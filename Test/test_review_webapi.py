# -*- coding: utf-8 -*-
"""Web 服务层评审修复的离线单元测试。

mock 掉 Mongo/Hub 依赖，使用 Flask test_client 验证评审修复项：
1. search_mode 白名单（非法值 400、大写变体不绕过公开钳制）
2. 空字符串 token 一律拒绝
3. /maintenance/ping?deep=1 深度健康检查结构
4. /rssfeed.xml 返回 200 且 Content-Type 为 RSS/XML
5. 登录限流键使用可信代理解析后的 IP
另覆盖 /collect 的 CONTRACT-1 code 契约与安全响应头。
"""
import datetime
import queue
import sys
import types
import unittest
import uuid
from unittest.mock import MagicMock

# 兼容 shim：IntelligenceHub.py（批次A）的 Error dataclass 使用
# field(default_factory=list)，而 attrs 的 field() 没有 default_factory 形参，
# 会在 import 期抛 TypeError。这里给 attr.field 包一层 default_factory→factory
# 映射，等批次A修复（改 factory=list 或改用 stdlib dataclasses）后可移除本段。
try:
    import attr as _real_attr

    _attr_compat = types.ModuleType('attr')
    _attr_compat.__dict__.update(vars(_real_attr))
    _real_attr_field = _real_attr.field

    def _field_compat(*args, **kwargs):
        if 'default_factory' in kwargs:
            kwargs['factory'] = kwargs.pop('default_factory')
        return _real_attr_field(*args, **kwargs)

    _attr_compat.field = _field_compat
    sys.modules['attr'] = _attr_compat
except ImportError:
    import dataclasses as _dataclasses

    _attr_stub = types.ModuleType('attr')
    _attr_stub.dataclass = _dataclasses.dataclass
    _attr_stub.field = _dataclasses.field
    sys.modules['attr'] = _attr_stub

from flask import Flask

from IntelligenceHub import IntelligenceHub
from IntelligenceHubWebService import (
    CSRF_SESSION_KEY,
    IntelligenceHubWebService,
    WebServiceAccessManager,
)
from ServiceComponent.RSSPublisher import RSSPublisher


def _build_service(config=None):
    """构造挂好路由的 Flask app 与全 mock 的 Hub（不触 Mongo/网络）。"""
    hub = MagicMock()
    hub._analysis_threads = []
    hub.original_queue = queue.Queue()
    hub.query_intelligence.return_value = ([], 0)
    hub.vector_search_intelligence.return_value = []
    hub.get_intelligence.return_value = []
    hub.submit_collected_data.return_value = True

    access_manager = WebServiceAccessManager(
        rpc_api_tokens=['rpc-token'],
        collector_tokens=['collector-token'],
        processor_tokens=[],
        user_manager=None,
        deny_on_empty_config=True,
    )
    service = IntelligenceHubWebService(
        intelligence_hub=hub,
        access_manager=access_manager,
        rss_publisher=RSSPublisher('http://localhost'),
        config=config,
    )
    app = Flask(__name__)
    app.secret_key = 'test-secret-key'
    service.register_routers(app)
    return app, hub, service


class TestSearchModeWhitelist(unittest.TestCase):
    """① search_mode 白名单与公开钳制的无条件执行。"""

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()

    def test_invalid_search_mode_returns_400(self):
        # 'Mongo' 等大写变体、空串、未知值一律 400，不能落到 else→mongo 分派
        for bad in ('Mongo', 'MONGO', '', 'sql', 'VECTOR_TEXT', 'mongo '):
            resp = self.client.get(f'/intelligences/query?search_mode={bad}')
            self.assertEqual(resp.status_code, 400, f'search_mode={bad!r} 应返回 400')
        self.hub.query_intelligence.assert_not_called()
        self.hub.vector_search_intelligence.assert_not_called()

    def test_public_clamps_apply_to_all_valid_modes(self):
        # mongo 模式：per_page 钳制到 mongo_max_per_page(20)，强制 30 天归档窗口
        resp = self.client.get('/intelligences/query?search_mode=mongo&per_page=100')
        self.assertEqual(resp.status_code, 200)
        _, kwargs = self.hub.query_intelligence.call_args
        self.assertEqual(kwargs['limit'], 20)
        start, end = kwargs['archive_period']
        self.assertIsNotNone(start.tzinfo)  # Web 层产出 aware datetime（CONTRACT-3/8）
        self.assertEqual((end - start).days, 30)
        # threshold_min=0 时不传 threshold（修复13：Hub 默认 None 后行为不受影响）
        self.assertNotIn('threshold', kwargs)

        # vector 模式同样被强制窗口与最小相似度阈值
        resp = self.client.get('/intelligences/query?search_mode=vector_text&keywords=x&per_page=50')
        self.assertEqual(resp.status_code, 200)
        _, vkwargs = self.hub.vector_search_intelligence.call_args
        v_start, v_end = vkwargs['archive_period']
        self.assertIsNotNone(v_start.tzinfo)
        self.assertEqual((v_end - v_start).days, 30)
        self.assertGreaterEqual(vkwargs['score_threshold'], 0.6)

        # 页码上限对所有模式生效（超出返回错误）
        resp = self.client.get('/intelligences/query?search_mode=mongo&page=99')
        self.assertEqual(resp.status_code, 429)
        resp = self.client.get('/intelligences/query?search_mode=vector_text&keywords=x&page=99')
        self.assertEqual(resp.status_code, 429)


class TestCollectContract(unittest.TestCase):
    """② 空 token 拒绝与 /collect 的 CONTRACT-1 code 契约。"""

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()

    def test_empty_token_always_rejected(self):
        am = self.service.access_manager
        self.assertFalse(am.check_collector_token(''))
        self.assertFalse(am.check_rpc_api_token(''))
        self.assertFalse(am.check_processor_token(''))

    def test_empty_token_in_config_filtered(self):
        am = WebServiceAccessManager([''], [''], [''], None, deny_on_empty_config=True)
        self.assertEqual(am.collector_tokens, [])
        self.assertEqual(am.rpc_api_tokens, [])
        self.assertEqual(am.processor_tokens, [])
        self.assertFalse(am.check_collector_token(''))

    def test_collect_empty_token_returns_401(self):
        resp = self.client.post('/collect', json={
            'UUID': str(uuid.uuid4()), 'token': ''})
        self.assertEqual(resp.status_code, 401)
        self.hub.submit_collected_data.assert_not_called()

    def test_collect_contract_codes(self):
        # 正常入队：status ok
        resp = self.client.post('/collect', json={
            'UUID': str(uuid.uuid4()), 'token': 'collector-token'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'ok')

        # 重复数据：HTTP 200 + code duplicate
        self.hub.submit_collected_data.return_value = IntelligenceHub.Error(
            error_list=['Collected message duplicated xxx.'])
        resp = self.client.post('/collect', json={
            'UUID': str(uuid.uuid4()), 'token': 'collector-token'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['status'], 'error')
        self.assertEqual(body['code'], 'duplicate')

        # 队列满：HTTP 503 + code queue_full
        self.hub.submit_collected_data.return_value = IntelligenceHub.Error(
            error_list=['Queue full, service busy. Try again later.'])
        resp = self.client.post('/collect', json={
            'UUID': str(uuid.uuid4()), 'token': 'collector-token'})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()['code'], 'queue_full')

        # 校验失败（UUID 格式非法）：HTTP 200 + code validation
        resp = self.client.post('/collect', json={
            'UUID': 'not-a-uuid', 'token': 'collector-token'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['code'], 'validation')

        # token 校验先于 UUID 校验：非法 UUID + 非法 token → 401（防未认证探测）
        resp = self.client.post('/collect', json={
            'UUID': 'not-a-uuid', 'token': 'wrong-token'})
        self.assertEqual(resp.status_code, 401)


class TestMaintenancePing(unittest.TestCase):
    """③ /maintenance/ping?deep=1 深度健康检查结构。"""

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()

    def test_shallow_ping_unchanged(self):
        resp = self.client.get('/maintenance/ping')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'ok')

    def test_security_headers_present(self):
        resp = self.client.get('/maintenance/ping')
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.headers.get('X-Frame-Options'), 'DENY')
        self.assertEqual(resp.headers.get('Referrer-Policy'), 'no-referrer')

    def test_deep_ping_healthy(self):
        """legacy 模式：AI 线程存活 → 200。"""
        self.hub.consumer_mode = lambda: 'legacy_ai_worker'
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.hub._analysis_threads = [worker]
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertIn('checks', body)
        self.assertEqual(body['checks']['ai_workers']['alive'], 1)
        self.assertEqual(body['checks']['queues']['original_queue'], 0)

    def test_deep_ping_collect_only_healthy_without_workers(self):
        """P0-1 核心回归：collect_only 模式无 AI 线程是预期状态 → 200。

        原实现把 _analysis_threads 为空判为 503，导致只采集/DRY_RUN/
        Drainer 模式被 Launcher 误判为故障反复重启。
        """
        self.hub.consumer_mode = lambda: 'collect_only'
        self.hub._analysis_threads = []
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['checks']['mode'], 'collect_only')
        self.assertEqual(resp.get_json()['checks']['ai_workers']['expected'], 'none')

    def test_deep_ping_dry_run_healthy(self):
        """P0-1：DRY_RUN 模式无 AI 线程是预期状态 → 200。"""
        self.hub.consumer_mode = lambda: 'dry_run'
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 200)

    def test_deep_ping_drainer_alive_healthy(self):
        """P0-1：Drainer 模式检查 Drainer 线程（等待非高峰不判故障）。"""
        self.hub.consumer_mode = lambda: 'off_peak_drainer'
        self.hub.off_peak_drainer.is_running.return_value = True
        # 模拟等待非高峰：闸门拒绝但不影响健康
        self.hub.ai_call_gate_status = lambda: {
            'mode': 'off_peak_drainer', 'reason': 'off_peak_window',
            'api_enabled': True, 'api_dry_run': False}
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['checks']['off_peak_drainer']['alive'])
        self.assertEqual(body['checks']['ai_call_gate']['reason'], 'off_peak_window')

    def test_deep_ping_drainer_dead_unhealthy(self):
        """P0-1：Drainer 模式下 Drainer 线程死亡 → 503（该重启）。"""
        self.hub.consumer_mode = lambda: 'off_peak_drainer'
        self.hub.off_peak_drainer.is_running.return_value = False
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 503)

    def test_deep_ping_queue_backlog_unhealthy(self):
        self.hub.consumer_mode = lambda: 'collect_only'
        backlog = queue.Queue()
        for i in range(1600):
            backlog.put(i)
        self.hub.original_queue = backlog
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()['status'], 'unhealthy')

    def test_deep_ping_dead_worker_unhealthy(self):
        """legacy 模式：AI 线程死亡 → 503（线程故障必须能发现）。"""
        self.hub.consumer_mode = lambda: 'legacy_ai_worker'
        dead = MagicMock()
        dead.is_alive.return_value = False
        self.hub._analysis_threads = [dead]
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()['status'], 'unhealthy')

    def test_deep_ping_post_process_dead_unhealthy(self):
        """P0-1：后处理（归档）线程死亡任何模式都 → 503。"""
        self.hub.consumer_mode = lambda: 'collect_only'
        self.hub.post_process_thread.is_alive.return_value = False
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 503)

    def test_deep_ping_mongo_down_unhealthy(self):
        """P0-1：MongoDB 不可达 → 503（业务就绪不可用）。"""
        self.hub.consumer_mode = lambda: 'collect_only'
        def _raise(*args, **kwargs):
            raise RuntimeError('connection refused')
        self.hub.mongo_db_cache.client.admin.command.side_effect = _raise
        resp = self.client.get('/maintenance/ping?deep=1')
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json()['checks']['mongodb']['ok'])


class TestRssFeed(unittest.TestCase):
    """④ /rssfeed.xml 返回 200 且 Content-Type 为 RSS/XML。"""

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()

    def test_rssfeed_returns_rss_xml(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.hub.query_intelligence.return_value = ([{
            'UUID': '11111111-2222-4333-8444-555555555555',
            'EVENT_TITLE': None,  # EVENT_TITLE 为 None 时 title 应回退 EVENT_BRIEF
            'EVENT_BRIEF': '公开摘要内容',
            'EVENT_TEXT': '全文不应出现在 RSS 中',
            'APPENDIX': {'__TIME_PUB__': now},
        }], 1)
        resp = self.client.get('/rssfeed.xml')
        self.assertEqual(resp.status_code, 200)
        content_type = resp.headers.get('Content-Type', '').lower()
        self.assertIn('rss', content_type)
        self.assertIn('xml', content_type)
        body = resp.get_data(as_text=True)
        self.assertIn('公开摘要内容', body)
        self.assertNotIn('全文不应出现在 RSS 中', body)


class TestLoginClientIp(unittest.TestCase):
    """⑤ 登录限流键使用可信代理解析后的 IP，username 走 flash（CONTRACT-6）。"""

    def setUp(self):
        config = {'intelligence_hub_web_service.trusted_proxies': ['127.0.0.1']}
        self.app, self.hub, self.service = _build_service(config=config)
        self.client = self.app.test_client()

    def _post_login(self, remote_addr, xff=None):
        with self.client.session_transaction() as sess:
            sess[CSRF_SESSION_KEY] = 'csrf-test'
        headers = {'X-Forwarded-For': xff} if xff else {}
        return self.client.post(
            '/login',
            data={'username': 'alice', 'password': 'wrong', 'csrf_token': 'csrf-test'},
            headers=headers,
            environ_overrides={'REMOTE_ADDR': remote_addr},
        )

    def test_rate_limit_key_uses_trusted_proxy_resolved_ip(self):
        # 直连对端在可信白名单内：采信 XFF
        resp = self._post_login('127.0.0.1', '9.9.9.9')
        self.assertEqual(resp.status_code, 302)
        # CONTRACT-6：登录失败重定向不再把 username 放 URL query
        self.assertNotIn('username=', resp.headers.get('Location', ''))
        keys = list(self.service._login_rate_limiter._records.keys())
        self.assertTrue(any(k.startswith('9.9.9.9:') for k in keys),
                        f'限流键应使用代理解析后的 IP，实际: {keys}')
        # CONTRACT-6：username 通过 flash（category 'username'）传递
        with self.client.session_transaction() as sess:
            flashes = sess.get('_flashes', [])
        self.assertIn(('username', 'alice'), flashes)

    def test_xff_ignored_when_peer_not_trusted(self):
        # 直连对端不在白名单内：忽略可伪造的 XFF，使用 remote_addr
        resp = self._post_login('10.1.2.3', '9.9.9.9')
        self.assertEqual(resp.status_code, 302)
        keys = list(self.service._login_rate_limiter._records.keys())
        self.assertTrue(any(k.startswith('10.1.2.3:') for k in keys),
                        f'限流键应使用 remote_addr，实际: {keys}')
        self.assertFalse(any(k.startswith('9.9.9.9:') for k in keys))


if __name__ == '__main__':
    unittest.main()
