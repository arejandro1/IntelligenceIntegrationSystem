# -*- coding: utf-8 -*-
"""
DeepSeekModelRouter 分级路由引擎（M3 / P1-6 增强）
管理 Flash/Pro 模型路由、时间策略校验、预算守卫、费用台账、MOCK/DRY_RUN模式。

P1-6 新增：
- estimate_cost：按内容长度预估费用（供预算原子预留）；
- make_attempt_guard：返回逐尝试（含每次重试）护栏钩子——
  时间窗口、预算、停止信号任一不满足时抛 ApiCallBlocked，
  注入 DeepSeekClient.pre_attempt_hook 后，每次真实 HTTP 尝试前都会执行。
"""
import logging
import math
import re
from typing import Callable, Dict, List, Optional

from ServiceComponent.ApiTimePolicy import ApiTimePolicy
from ServiceComponent.PricingPolicyRegistry import PricingPolicyRegistry
from ServiceComponent.BudgetGuard import BudgetGuard
from ServiceComponent.ApiUsageLedger import ApiUsageLedger
from ServiceComponent.DeepSeekClient import ApiAttemptBlocked

logger = logging.getLogger(__name__)

# 批次二：保守 token 估算系数。
# - CJK 字符：DeepSeek 系 tokenizer 约 0.6~0.7 token/汉字，按 1.5 计留足余量；
# - 其他字符：英文约 4 字符/token，按 3.5 计留余量；
# - 每条消息固定 +16 token 开销（role/name 等结构字段）。
# 注意：这是"保守上界预估"（宁可高估导致少调用），不是数学意义上的硬上限——
# tokenizer 算法可能变化，超保守系数已覆盖常见偏差。
_CJK_CHAR_RE = re.compile(r'[\u2E80-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF]')
_CJK_TOKENS_PER_CHAR = 1.5
_OTHER_CHARS_PER_TOKEN = 3.5
_MESSAGE_OVERHEAD_TOKENS = 16


def estimate_tokens_for_text(text: str) -> int:
    """按保守上界估算一段文本的 token 数（预算预留用，宁可高估）。"""
    if not text:
        return 0
    text = str(text)
    cjk = len(_CJK_CHAR_RE.findall(text))
    other = len(text) - cjk
    return math.ceil(cjk * _CJK_TOKENS_PER_CHAR + other / _OTHER_CHARS_PER_TOKEN)


def estimate_tokens_for_messages(messages: List[Dict[str, str]]) -> int:
    """估算最终实际发送 messages 的输入 token 总数（含 prompt 与正文）。"""
    total = 0
    for m in messages or []:
        total += estimate_tokens_for_text(str((m or {}).get('content', '') or ''))
        total += _MESSAGE_OVERHEAD_TOKENS
    return total


class ApiCallBlocked(ApiAttemptBlocked):
    """护栏（时间窗口/预算/停止信号）阻止本次 API 尝试。

    继承 DeepSeekClient.ApiAttemptBlocked：客户端重试循环识别该类型后
    立即终止重试（而非当作可重试错误退避重试）。
    """


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
        """综合校验：api_enabled + 时间 + 预算 + 台账可用性"""
        if not self.api_enabled:
            return False
        # 批次二：台账降级（读写故障）期间暂停新增付费请求
        if getattr(self.ledger, 'is_degraded', lambda: False)():
            return False
        if not self.time_policy.allowed_to_start_request():
            return False
        if not self.budget.can_call():
            return False
        return True

    def block_reason(self) -> Optional[str]:
        """当前禁止调用的原因（None=允许）。仅读取状态，不触发任何 API 调用。"""
        if not self.api_enabled:
            return 'api_disabled'
        if self.api_dry_run:
            return 'dry_run'
        if getattr(self.ledger, 'is_degraded', lambda: False)():
            return 'ledger_degraded'
        if not self.time_policy.allowed_to_start_request():
            return 'off_peak_window'
        if not self.budget.can_call():
            return 'budget_exhausted'
        return None

    def route(self, is_priority: bool = False) -> str:
        """根据优先级选择模型"""
        return self.pricing.pro_model if is_priority else self.pricing.flash_model

    def estimate_cost(self, model: str, content: str,
                      max_tokens: Optional[int] = None) -> float:
        """按内容长度预估一次调用费用（预算预留用，宁可高估）。

        批次二说明：本方法只按单一 content 文本估算；生产链路的真实
        请求消息还包含完整 prompt（很长）与正文，必须改用
        estimate_cost_for_messages（从最终实际发送的消息估算），
        否则输入费用会被严重低估（甚至把正文当空字符串）。
        """
        est_input_tokens = estimate_tokens_for_text(content)
        est_output_tokens = int(max_tokens or 2048)
        return self.pricing.calculate_cost(
            model, est_input_tokens, est_output_tokens,
            is_peak=self.time_policy.is_peak())

    def estimate_cost_for_messages(self, model: str,
                                   messages: List[Dict[str, str]],
                                   max_tokens: int) -> float:
        """批次二：从最终实际发送的消息估算调用费用（含 prompt 与正文）。

        - 输入：estimate_tokens_for_messages(messages)——覆盖 system prompt
          （V25 分析提示词）与 user 正文，任何一部分缺失都会低估费用；
        - 输出：按 max_tokens 上限全额估算——该值必须与实际请求使用的
          max_tokens 一致（调用方负责传入同一来源的值）。
        """
        est_input_tokens = estimate_tokens_for_messages(messages)
        est_output_tokens = int(max_tokens or 2048)
        return self.pricing.calculate_cost(
            model, est_input_tokens, est_output_tokens,
            is_peak=self.time_policy.is_peak())

    def compute_cost(self, model: str, input_tokens: int, output_tokens: int, at_time=None) -> float:
        is_peak = self.time_policy.is_peak(at_time)
        return self.pricing.calculate_cost(model, input_tokens, output_tokens, is_peak)

    def has_price(self, model: str) -> bool:
        """模型是否配置了价格（未配置时费用只能记为未知/0 并显式告警）。"""
        return self.pricing.get_pricing(model) is not None

    def make_attempt_guard(self, stop_event=None, rate_limiter=None) -> Callable[[], None]:
        """构造逐尝试护栏钩子（注入 DeepSeekClient.pre_attempt_hook）。

        每次 HTTP 尝试（含重试）前执行：
        - stop_event 已置位 → 阻止（进程正在退出）；
        - rate_limiter（批次二）不允许 → 阻止——每次实际网络尝试（含
          客户端内部重试）都独立限流，失败尝试同样占用配额；
        - 时间窗口关闭（高峰/Guard 边界）→ 阻止（规则：高峰期重试也为 0）；
        - 预算耗尽 → 阻止。批次二修复：存在活跃预留（本次调用名额已在
          任务开始时原子取得）时跳过预算检查——已取得的调用名额不得
          在发送前再次被当作"额度耗尽"拒绝。
        阻止时抛 ApiCallBlocked，由客户端立即终止重试循环。
        """
        def guard() -> None:
            if stop_event is not None and stop_event.is_set():
                raise ApiCallBlocked("停止信号已置位，取消本次 API 尝试。")
            if not self.api_enabled or not self.time_policy.allowed_to_start_request():
                raise ApiCallBlocked("当前不在允许调用窗口（api_enabled/峰谷策略）。")
            if rate_limiter is not None:
                if not rate_limiter.allow():
                    raise ApiCallBlocked("每分钟最大开始次数已达上限（含失败尝试）。")
                rate_limiter.record_start()
            # 批次二：有活跃预留时跳过预算检查（名额已扣，can_call 会误拒）；
            # 无预留（如直接调用客户端的旁路场景）退回常规预算判定。
            if not self.budget.has_active_reserve() and not self.budget.can_call():
                raise ApiCallBlocked("预算或调用次数已耗尽。")
        return guard