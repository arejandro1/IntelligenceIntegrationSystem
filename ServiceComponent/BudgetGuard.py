# -*- coding: utf-8 -*-
"""
BudgetGuard 预算守卫（M3）
职责：记录每次 API 调用的费用，限制日/月预算和调用次数上限。
"""
from datetime import datetime, timedelta
from typing import Optional


class BudgetGuard:
    """预算与调用次数限制守卫。"""

    def __init__(self,
                 daily_budget_cny: float = 0.0,
                 monthly_budget_cny: float = 0.0,
                 daily_call_limit: int = 0,
                 monthly_call_limit: int = 0):
        self.daily_budget = daily_budget_cny
        self.monthly_budget = monthly_budget_cny
        self.daily_limit = daily_call_limit
        self.monthly_limit = monthly_call_limit
        self._daily_cost: float = 0.0
        self._monthly_cost: float = 0.0
        self._daily_calls: int = 0
        self._monthly_calls: int = 0

    def record(self, cost_cny: float) -> None:
        """记录一次调用费用"""
        self._daily_cost += cost_cny
        self._monthly_cost += cost_cny
        self._daily_calls += 1
        self._monthly_calls += 1

    def can_call(self) -> bool:
        """检查是否允许继续调用"""
        if self.daily_budget > 0 and self._daily_cost >= self.daily_budget:
            return False
        if self.monthly_budget > 0 and self._monthly_cost >= self.monthly_budget:
            return False
        if self.daily_limit > 0 and self._daily_calls >= self.daily_limit:
            return False
        if self.monthly_limit > 0 and self._monthly_calls >= self.monthly_limit:
            return False
        return True

    def reset_daily(self) -> None:
        self._daily_cost = 0.0
        self._daily_calls = 0

    @property
    def daily_cost(self) -> float:
        return self._daily_cost

    @property
    def daily_calls(self) -> int:
        return self._daily_calls