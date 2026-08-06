# -*- coding: utf-8 -*-
"""
PricingPolicyRegistry 计费政策注册表（M3）
维护各模型的基准单价，计算调用费用。价格、政策版本、生效日期必须配置化。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


@dataclass
class ModelPricing:
    model_name: str
    input_price_cny_per_1k: float = 0.001
    output_price_cny_per_1k: float = 0.002
    is_flash: bool = False

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return round((input_tokens / 1000) * self.input_price_cny_per_1k + (output_tokens / 1000) * self.output_price_cny_per_1k, 6)


class PricingPolicyRegistry:
    def __init__(self, flash_model: str = "", pro_model: str = "",
                 flash_pricing: Optional[ModelPricing] = None,
                 pro_pricing: Optional[ModelPricing] = None,
                 peak_multiplier: float = 2.0, off_peak_multiplier: float = 1.0,
                 policy_version: str = "", effective_from: Optional[datetime] = None):
        self.flash_model = flash_model
        self.pro_model = pro_model
        self.flash_pricing = flash_pricing or ModelPricing(model_name=flash_model, is_flash=True)
        self.pro_pricing = pro_pricing or ModelPricing(model_name=pro_model, is_flash=False)
        self.peak_multiplier = peak_multiplier
        self.off_peak_multiplier = off_peak_multiplier
        self.policy_version = policy_version
        self.effective_from = effective_from

    def is_effective(self) -> bool:
        return self.effective_from is not None and datetime.now() >= self.effective_from

    def get_pricing(self, model_name: str) -> Optional[ModelPricing]:
        if model_name == self.flash_model: return self.flash_pricing
        if model_name == self.pro_model: return self.pro_pricing
        return None

    def calculate_cost(self, model_name: str, input_tokens: int, output_tokens: int, is_peak: bool) -> float:
        pricing = self.get_pricing(model_name)
        if not pricing: return 0.0
        base = pricing.calculate_cost(input_tokens, output_tokens)
        multiplier = self.peak_multiplier if is_peak else self.off_peak_multiplier
        return round(base * multiplier, 6)