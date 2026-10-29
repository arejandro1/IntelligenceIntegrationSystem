# -*- coding: utf-8 -*-
"""P0-1/P2-9: Hub 运行模式、AI 闸门状态与向量四态测试（最小实例）。

探活语义的端点级测试见 Test/test_review_webapi.py；此处覆盖 Hub 侧
只读辅助方法（不触发任何 API 调用）。
"""
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from IntelligenceHub import IntelligenceHub
    HUB_IMPORTABLE = True
except Exception:
    HUB_IMPORTABLE = False


def _minimal_hub(**attrs):
    hub = IntelligenceHub.__new__(IntelligenceHub)
    hub.api_enabled = attrs.get("api_enabled", False)
    hub.api_dry_run = attrs.get("api_dry_run", True)
    hub.use_legacy_ai_worker = attrs.get("use_legacy_ai_worker", True)
    hub.use_off_peak_drainer = attrs.get("use_off_peak_drainer", False)
    hub.model_router = attrs.get("model_router")
    hub.off_peak_drainer = attrs.get("off_peak_drainer")
    hub.vector_db_client = attrs.get("vector_db_client")
    hub.vector_db_engine_summary = attrs.get("vector_engine_summary")
    hub.vector_db_engine_full_text = attrs.get("vector_engine_full_text")
    hub.last_vector_error = attrs.get("last_vector_error")
    hub.lock = threading.Lock()
    return hub


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestConsumerMode(unittest.TestCase):
    def test_collect_only(self):
        hub = _minimal_hub(api_enabled=False, api_dry_run=True)
        self.assertEqual(hub.consumer_mode(), "collect_only")

    def test_dry_run(self):
        hub = _minimal_hub(api_enabled=True, api_dry_run=True)
        self.assertEqual(hub.consumer_mode(), "dry_run")

    def test_legacy_worker(self):
        hub = _minimal_hub(api_enabled=True, api_dry_run=False,
                           use_off_peak_drainer=False)
        self.assertEqual(hub.consumer_mode(), "legacy_ai_worker")

    def test_drainer(self):
        hub = _minimal_hub(api_enabled=True, api_dry_run=False,
                           use_off_peak_drainer=True,
                           off_peak_drainer=MagicMock())
        self.assertEqual(hub.consumer_mode(), "off_peak_drainer")

    def test_drainer_flag_without_instance_falls_back(self):
        """use_off_peak_drainer=True 但未注入实例 → 无消费者运行。

        startup() 在该组合下不启动旧 Worker（互斥），Drainer 实例又
        缺失，实际没有任何 AI 消费者 → collect_only（不误报 legacy）。
        """
        hub = _minimal_hub(api_enabled=True, api_dry_run=False,
                           use_off_peak_drainer=True, off_peak_drainer=None)
        self.assertEqual(hub.consumer_mode(), "collect_only")


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestAiCallGate(unittest.TestCase):
    def _router(self, api_enabled=True, dry_run=False, time_ok=True, budget_ok=True):
        router = MagicMock()
        router.api_enabled = api_enabled
        router.api_dry_run = dry_run
        router.time_policy = MagicMock()
        router.time_policy.allowed_to_start_request.return_value = time_ok
        router.budget_guard = MagicMock()
        router.budget_guard.can_call.return_value = budget_ok
        return router

    def test_api_disabled_reason(self):
        hub = _minimal_hub(api_enabled=False, model_router=self._router(api_enabled=False))
        self.assertEqual(hub.ai_call_gate_status()["reason"], "api_disabled")

    def test_dry_run_reason(self):
        hub = _minimal_hub(api_enabled=True, model_router=self._router(dry_run=True))
        self.assertEqual(hub.ai_call_gate_status()["reason"], "dry_run")

    def test_off_peak_window_reason(self):
        """等待非高峰：预期状态（信息性），非故障。"""
        hub = _minimal_hub(api_enabled=True,
                           model_router=self._router(time_ok=False))
        self.assertEqual(hub.ai_call_gate_status()["reason"], "off_peak_window")

    def test_budget_exhausted_reason(self):
        hub = _minimal_hub(api_enabled=True,
                           model_router=self._router(budget_ok=False))
        self.assertEqual(hub.ai_call_gate_status()["reason"], "budget_exhausted")

    def test_allowed(self):
        hub = _minimal_hub(api_enabled=True, model_router=self._router())
        self.assertEqual(hub.ai_call_gate_status()["reason"], "allowed")

    def test_no_router(self):
        hub = _minimal_hub(api_enabled=False, model_router=None)
        self.assertEqual(hub.ai_call_gate_status()["reason"], "api_disabled")


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestVectorStatus(unittest.TestCase):
    def test_disabled_when_no_client(self):
        hub = _minimal_hub()
        self.assertEqual(hub.vector_status, "disabled")

    def test_initializing_when_engines_absent(self):
        """启用但引擎未建 → initializing（区别于故障与无结果）。"""
        hub = _minimal_hub(vector_db_client=MagicMock(),
                           vector_engine_summary=None,
                           vector_engine_full_text=None)
        self.assertEqual(hub.vector_status, "initializing")

    def test_degraded_on_error(self):
        hub = _minimal_hub(vector_db_client=MagicMock(),
                           vector_engine_summary=MagicMock(),
                           vector_engine_full_text=MagicMock(),
                           last_vector_error="summary: connection refused")
        self.assertEqual(hub.vector_status, "degraded")

    def test_ok_when_ready(self):
        hub = _minimal_hub(vector_db_client=MagicMock(),
                           vector_engine_summary=MagicMock(),
                           vector_engine_full_text=MagicMock(),
                           last_vector_error=None)
        self.assertEqual(hub.vector_status, "ok")


if __name__ == "__main__":
    unittest.main()
