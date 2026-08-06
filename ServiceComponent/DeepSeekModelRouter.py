# -*- coding: utf-8 -*-
"""
DeepSeekModelRouter 分级路由引擎（M3）
管理 Flash/Pro 模型路由、时间策略校验、预算守卫、费用台账、MOCK/DRY_RUN模式。
"""
from typing import Dict, Optional
from ServiceComponent.ApiTimePolicy import ApiTimePolicy
from ServiceComponent.PricingPolicyRegistry import PricingPolicyRegistry
from ServiceComponent.BudgetGuard import BudgetGuard
from ServiceComponent.ApiUsageLedger import ApiUsageLedger


class DeepSeekModelRouter:
    """每次 API 调用前检查：时间 → 预算 → 模型路由 → 调用 → 费用记录"""

    def __init__(self, time_policy: ApiTimePolicy, pricing: PricingPolicyRegistry,
                 budget: BudgetGuard, ledger: ApiUsageLedger,
                 api_enabled: bool = False, api_dry_run: bool = True):
        self.time_policy = time_policy
        self.pricing = pricing
        self.budget = budget
        self.ledger = ledger
        self.api_enabled = api_enabled
        self.api_dry_run = api_dry_run

    def can_call(self) -> bool:
        """综合校验：api_enabled + 时间 + 预算"""
        if not self.api_enabled:
            return False
        if not self.time_policy.allowed_to_start_request():
            return False
        if not self.budget.can_call():
            return False
        return True

    def route(self, is_priority: bool = False) -> str:
        """根据优先级选择模型"""
        return self.pricing.pro_model if is_priority else self.pricing.flash_model

    def compute_cost(self, model: str, input_tokens: int, output_tokens: int, at_time=None) -> float:
        is_peak = self.time_policy.is_peak(at_time)
        return self.pricing.calculate_cost(model, input_tokens, output_tokens, is_peak)