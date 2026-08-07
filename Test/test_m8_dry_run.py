# -*- coding: utf-8 -*-
"""M8: DRY_RUN模式验证 + 高峰零调用测试"""
import unittest
from datetime import datetime
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
from ServiceComponent.ApiTimePolicy import ApiTimePolicy
from ServiceComponent.PricingPolicyRegistry import PricingPolicyRegistry
from ServiceComponent.BudgetGuard import BudgetGuard
from ServiceComponent.ApiUsageLedger import ApiUsageLedger
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter


class TestDryRunMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockDeepSeekClient(model_name="deepseek-flash")

    def test_mock_returns_response(self):
        resp = self.mock.chat_completion(messages=[{"role":"user","content":"分析当前局势"}])
        self.assertIn('choices', resp)
        self.assertIn('usage', resp)
        self.assertGreater(resp['usage']['prompt_tokens'], 0)

    def test_mock_call_count_increments(self):
        old = self.mock.call_count
        self.mock.chat_completion(messages=[{"role":"user","content":"test"}])
        self.assertEqual(self.mock.call_count, old + 1)

    def test_mock_id_prefix(self):
        resp = self.mock.chat_completion(messages=[{"role":"user","content":"Hello"}])
        self.assertTrue(resp['id'].startswith('mock-'))

    def test_router_blocked_when_api_disabled(self):
        """api_enabled=false时router必须禁止调用"""
        time_policy = ApiTimePolicy()
        pricing = PricingPolicyRegistry(flash_model="flash", pro_model="pro")
        budget = BudgetGuard(daily_budget_cny=10.0)
        ledger = ApiUsageLedger()
        router = DeepSeekModelRouter(time_policy, pricing, budget, ledger,
                                     api_enabled=False, api_dry_run=True)
        self.assertFalse(router.can_call())

    def test_ledger_records_dry_run(self):
        """ApiUsageLedger必须记录DRY_RUN费用"""
        ledger = ApiUsageLedger()
        ledger.record("attempt-1", "flash", 100, 50, 0.15, True)
        self.assertEqual(ledger.count(), 1)
        self.assertAlmostEqual(ledger.total_cost(), 0.15, places=6)


class TestPeakZeroCalls(unittest.TestCase):
    """高峰时段真实调用=0"""
    def test_peak_time_api_blocked(self):
        # 用可注入时间的策略验证高峰禁止
        from test_m8_time_policy import FakeClockPolicy
        peak_policy = FakeClockPolicy(datetime(2026, 8, 8, 10, 0, 0))
        self.assertFalse(peak_policy.allowed_to_start_request())


if __name__ == '__main__':
    unittest.main()