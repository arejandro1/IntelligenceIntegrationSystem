# -*- coding: utf-8 -*-
"""M8: 预算守卫测试——日/月预算 + 调用次数上限"""
import unittest
from ServiceComponent.BudgetGuard import BudgetGuard


class TestBudgetLimit(unittest.TestCase):
    def test_within_budget(self):
        b = BudgetGuard(daily_budget_cny=10.0)
        b.record(5.0)
        self.assertTrue(b.can_call())

    def test_exceed_daily_budget(self):
        b = BudgetGuard(daily_budget_cny=10.0)
        b.record(10.0)
        self.assertFalse(b.can_call())

    def test_daily_call_limit(self):
        b = BudgetGuard(daily_call_limit=3)
        for _ in range(3):
            b.record(0.01)
        self.assertFalse(b.can_call())

    def test_reset_daily(self):
        b = BudgetGuard(daily_budget_cny=10.0)
        b.record(15.0)
        self.assertFalse(b.can_call())
        b.reset_daily()
        self.assertTrue(b.can_call())
        self.assertEqual(b.daily_cost, 0.0)
        self.assertEqual(b.daily_calls, 0)

    def test_unset_limits_always_allow(self):
        b = BudgetGuard()
        self.assertTrue(b.can_call())
        b.record(99999.0)
        self.assertTrue(b.can_call())

    def test_combined_limits(self):
        b = BudgetGuard(daily_budget_cny=5.0, daily_call_limit=2)
        self.assertTrue(b.can_call())
        b.record(1.0)
        b.record(1.0)
        self.assertFalse(b.can_call())


if __name__ == '__main__':
    unittest.main()