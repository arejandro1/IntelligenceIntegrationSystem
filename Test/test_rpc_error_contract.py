# -*- coding: utf-8 -*-
"""RPC /api 错误处理契约的离线单元测试（问题 4）。

覆盖：
1. RPC 派发异常 → 结构化 JSON {'error': 'internal'} + HTTP 500，
   不回显内部异常文本（旧实现为空 body + 200 且 print 到 stdout）；
2. 成功路径响应格式不变（handle_flask_request 返回的序列化字符串原样透传）。

不调用真实付费 API；rpc_service.handle_flask_request 一律 Mock。
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


class TestRpcErrorContract(unittest.TestCase):

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()
        # /api 受 login_required 保护：测试前置登录态
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

    def test_dispatch_exception_returns_structured_500(self):
        # 派发异常：500 + {'error': 'internal'}，不回显内部异常文本
        self.service.rpc_service.handle_flask_request = MagicMock(
            side_effect=RuntimeError('boom-internal-detail'))
        resp = self.client.post('/api', json={'entry': 'query_intelligence'})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {'error': 'internal'})
        self.assertNotIn(b'boom-internal-detail', resp.data)
        self.assertNotIn(b'Traceback', resp.data)

    def test_success_path_response_unchanged(self):
        # 成功路径：handle_flask_request 返回的字符串原样作为响应体（200）
        payload = '{"entry": "query_intelligence", "msg_type": "rep"}'
        self.service.rpc_service.handle_flask_request = MagicMock(return_value=payload)
        resp = self.client.post('/api', json={'entry': 'query_intelligence'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), payload)

    def test_unauthenticated_rpc_rejected(self):
        # 未登录请求仍被 login_required 拦截（既有鉴权行为不变）
        client = self.app.test_client()
        resp = client.post('/api', json={'entry': 'query_intelligence'})
        self.assertIn(resp.status_code, (401, 302))


if __name__ == '__main__':
    unittest.main()
