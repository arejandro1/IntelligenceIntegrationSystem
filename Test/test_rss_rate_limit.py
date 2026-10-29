# -*- coding: utf-8 -*-
"""RSS 公开端点限流与 threshold 钳制的离线单元测试。

覆盖：
1. 未登录用户超限 → 429 + 中文提示，且不触发 MongoDB 查询（限流在查询前）；
2. 未登录用户未超限 → 200（成功路径响应格式不变）；
3. 已登录用户完全绕过限流（不调用限流器）；
4. 未登录 threshold 下限钳制为 4（问题 9）；已登录用户不受限。

限流器一律 Mock，不触真实 MongoDB / 网络。
"""
import queue
import sys
import types
import unittest
from unittest.mock import MagicMock

# 兼容 shim（同 test_review_webapi）：IntelligenceHub 的 Error dataclass 使用
# field(default_factory=list)，attr 的 field() 没有该形参，import 期会抛 TypeError。
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

from IntelligenceHub import IntelligenceHub  # noqa: F401  触发模块导入链
from IntelligenceHubWebService import (
    IntelligenceHubWebService,
    WebServiceAccessManager,
)
from ServiceComponent.RSSPublisher import RSSPublisher


def _build_service():
    """构造挂好路由的 Flask app 与全 mock 的 Hub（不触 Mongo/网络）。"""
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
    # Mock 公开 Mongo 限流器：本测试只验证限流接线与钳制逻辑，不验证限流算法本身
    service._public_mongo_rate_limiter = MagicMock()
    app = Flask(__name__)
    app.secret_key = 'test-secret-key'
    service.register_routers(app)
    return app, hub, service


class TestRSSRateLimit(unittest.TestCase):
    """/rssfeed.xml 未登录限流（问题 1）。"""

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.client = self.app.test_client()
        self.limiter = self.service._public_mongo_rate_limiter

    def test_anonymous_rate_limited_returns_429_without_mongo_query(self):
        # 未登录 + 超限：返回 429 与中文提示，且 Mongo 查询一次都不能发生
        self.limiter.is_allowed.return_value = False
        resp = self.client.get('/rssfeed.xml?count=200')
        self.assertEqual(resp.status_code, 429)
        self.assertIn('请求过于频繁', resp.get_json()['error'])
        self.hub.query_intelligence.assert_not_called()
        self.limiter.is_allowed.assert_called_once()

    def test_anonymous_allowed_keeps_200_success_path(self):
        # 未登录 + 未超限：成功路径不变（200 + RSS XML）
        self.limiter.is_allowed.return_value = True
        resp = self.client.get('/rssfeed.xml')
        self.assertEqual(resp.status_code, 200)
        self.hub.query_intelligence.assert_called_once()

    def test_logged_in_bypasses_rate_limit(self):
        # 已登录：完全不限流（限流器不应被调用）
        self.limiter.is_allowed.return_value = False
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True
        resp = self.client.get('/rssfeed.xml?count=200')
        self.assertEqual(resp.status_code, 200)
        self.limiter.is_allowed.assert_not_called()


class TestRSSThresholdFloor(unittest.TestCase):
    """/rssfeed.xml 公开 threshold 下限钳制（问题 9）。"""

    def setUp(self):
        self.app, self.hub, self.service = _build_service()
        self.service._public_mongo_rate_limiter.is_allowed.return_value = True
        self.client = self.app.test_client()

    def test_anonymous_threshold_floored_at_4(self):
        # 未登录 threshold=2 应被钳制为 4，防止低阈值批量拉取低分数据
        self.client.get('/rssfeed.xml?threshold=2')
        _, kwargs = self.hub.query_intelligence.call_args
        self.assertEqual(kwargs['threshold'], 4)

    def test_anonymous_threshold_above_floor_unchanged(self):
        # 未登录 threshold=6（高于下限）不受影响
        self.client.get('/rssfeed.xml?threshold=6')
        _, kwargs = self.hub.query_intelligence.call_args
        self.assertEqual(kwargs['threshold'], 6)

    def test_logged_in_threshold_not_floored(self):
        # 已登录 threshold=2 原样透传
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True
        self.client.get('/rssfeed.xml?threshold=2')
        _, kwargs = self.hub.query_intelligence.call_args
        self.assertEqual(kwargs['threshold'], 2)


if __name__ == '__main__':
    unittest.main()
