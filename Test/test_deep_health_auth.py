# -*- coding: utf-8 -*-
"""深度健康检查鉴权降级的离线单元测试（问题 2）。

覆盖：
1. 外部 IP 访问 /maintenance/ping?deep=1 → 静默降级为浅探活响应（无内部细节）；
2. 外部 IP 伪造 X-Forwarded-For: 127.0.0.1 不能绕过（直连时不可信）；
3. 本机回环 127.0.0.1 / ::1 的 deep 探活仍然可用（Launcher 不被误伤）；
4. 已登录 session + 外部 IP → deep 细节正常返回；
5. 外部 IP 浅探活（无 deep）保持 200（Docker HEALTHCHECK 兼容）。
"""
import queue
import sys
import types
import unittest
from unittest.mock import MagicMock

# 兼容 shim（同 test_review_webapi）
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

from IntelligenceHub import IntelligenceHub  # noqa: F401
from IntelligenceHubWebService import (
    IntelligenceHubWebService,
    WebServiceAccessManager,
)
from ServiceComponent.RSSPublisher import RSSPublisher

EXTERNAL_IP = '203.0.113.77'  # TEST-NET-3，仅测试用途


def _build_service():
    hub = MagicMock()
    hub._analysis_threads = []
    hub.original_queue = queue.Queue()
    hub.query_intelligence.return_value = ([], 0)

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
        config=None,
    )
    app = Flask(__name__)
    app.secret_key = 'test-secret-key'
    service.register_routers(app)
    return app, hub, service


class TestDeepHealthAuth(unittest.TestCase):

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()

    def test_external_ip_deep_degrades_to_shallow(self):
        # 外部 IP 带 deep=1：响应与浅探活完全一致，不泄露 checks 内部细节
        resp = self.client.get('/maintenance/ping?deep=1',
                               environ_base={'REMOTE_ADDR': EXTERNAL_IP})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertNotIn('checks', body)

    def test_external_ip_cannot_bypass_via_forwarded_header(self):
        # 外部直连时 X-Forwarded-For 不可信：伪造回环 IP 不能获取 deep 细节
        resp = self.client.get(
            '/maintenance/ping?deep=1',
            environ_base={'REMOTE_ADDR': EXTERNAL_IP},
            headers={'X-Forwarded-For': '127.0.0.1'})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('checks', resp.get_json())

    def test_localhost_deep_returns_details(self):
        # 本机回环（Launcher ServerManager 场景）deep 探活不受影响
        resp = self.client.get('/maintenance/ping?deep=1',
                               environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('checks', resp.get_json())

    def test_ipv6_loopback_deep_returns_details(self):
        resp = self.client.get('/maintenance/ping?deep=1',
                               environ_base={'REMOTE_ADDR': '::1'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('checks', resp.get_json())

    def test_logged_in_external_ip_deep_returns_details(self):
        # 已登录 session：即使来源为外部 IP 也允许 deep 细节
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True
        resp = self.client.get('/maintenance/ping?deep=1',
                               environ_base={'REMOTE_ADDR': EXTERNAL_IP})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('checks', resp.get_json())

    def test_shallow_ping_from_external_unchanged(self):
        # 外部 IP 浅探活保持 200（Docker HEALTHCHECK 依赖，不受本修复影响）
        resp = self.client.get('/maintenance/ping',
                               environ_base={'REMOTE_ADDR': EXTERNAL_IP})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
