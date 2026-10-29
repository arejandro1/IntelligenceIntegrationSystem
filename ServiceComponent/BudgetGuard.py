# -*- coding: utf-8 -*-
"""
BudgetGuard 预算守卫（M3）
职责：记录每次 API 调用的费用，限制日/月预算和调用次数上限。
支持预警阈值（warn_threshold）：到达阈值发出警告但不停止；
stop 阈值（daily/monthly_budget）到达后停止调用。
"""
import threading
from datetime import datetime, timedelta, date
from typing import Callable, Optional
from zoneinfo import ZoneInfo

# 计费时区（默认 Asia/Shanghai，与 .clinerules/10-architecture.md 一致）
DEFAULT_BILLING_TIMEZONE = "Asia/Shanghai"


class BudgetGuard:
    """预算与调用次数限制守卫。

    daily_budget_cny / monthly_budget_cny：到达即停止（hard stop）
    daily_warn_cny / monthly_warn_cny：到达仅告警（soft warn），不影响 can_call

    P1-6 修复：
    - now_fn 可注入（FakeClock 测试要求，30-testing）；
    - restore_from_ledger：启动时从持久化台账恢复当日/当月累计；
    - try_reserve / settle：并发下预算检查+额度预留的原子操作
      （进程内锁；本项目 AI 消费并发=1，进程内原子已满足约束）；
    - 预留差额在 settle 时修正，调用结束按实际 usage 结算。
    """

    def __init__(self,
                 daily_budget_cny: float = 0.0,
                 monthly_budget_cny: float = 0.0,
                 daily_call_limit: int = 0,
                 monthly_call_limit: int = 0,
                 daily_warn_cny: float = 0.0,
                 monthly_warn_cny: float = 0.0,
                 warn_callback: Optional[Callable[[str, float], None]] = None,
                 timezone: str = DEFAULT_BILLING_TIMEZONE,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.daily_budget = daily_budget_cny
        self.monthly_budget = monthly_budget_cny
        self.daily_limit = daily_call_limit
        self.monthly_limit = monthly_call_limit
        # 预警阈值（0 表示不预警）
        self.daily_warn = daily_warn_cny
        self.monthly_warn = monthly_warn_cny
        self._warn_callback = warn_callback
        self._daily_cost: float = 0.0
        self._monthly_cost: float = 0.0
        self._daily_calls: int = 0
        self._monthly_calls: int = 0
        # 预警是否已触发（避免每次调用重复告警）
        self._daily_warn_fired: bool = False
        self._monthly_warn_fired: bool = False
        self._lock = threading.Lock()
        # 批次二：活跃预留计数——try_reserve +1，settle/cancel_reserve -1。
        # 用于逐尝试护栏判断"当前请求已取得调用名额"：
        # 已预留的请求在发送前不得被预算检查误拒（名额已扣，can_call 为假）。
        self._pending_reserves: int = 0
        # 计费时区（默认 Asia/Shanghai）：跨日/跨月判断必须基于计费时区，
        # 避免部署机 UTC 时区导致日预算在错误时间点重置。
        self._tz = ZoneInfo(timezone)
        # P1-6：可注入时钟（FakeClock）；默认按计费时区取当前时间
        self._now_fn = now_fn or (lambda: datetime.now(self._tz))
        # 惰性重置跟踪：跨日/跨月自动滚动重置，不依赖外部调用 reset_daily/reset_monthly
        now_local = self._now_local()
        self._current_day: date = now_local.date()
        self._current_month: str = self._current_day.strftime("%Y-%m")

    def _now_local(self) -> datetime:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        return now.astimezone(self._tz)

    def _lazy_rollover_if_needed(self) -> None:
        """跨日/跨月时惰性重置日/月累计，避免进程跨零点后永久停摆。"""
        today = self._now_local().date()
        if today != self._current_day:
            self.reset_daily()
            self._current_day = today
        current_month = today.strftime("%Y-%m")
        if current_month != self._current_month:
            self._monthly_cost = 0.0
            self._monthly_calls = 0
            self._monthly_warn_fired = False
            self._current_month = current_month

    def restore_from_ledger(self, ledger) -> None:
        """P1-6：从持久化台账恢复当日/当月累计（启动时调用一次）。

        ledger 需提供 daily_summary()/monthly_summary()（ApiUsageLedger 已实现）。
        恢复发生在进程启动时，之后的新增记录仍走 record/settle 正常累计。
        """
        try:
            day_sum = ledger.daily_summary()
            month_sum = ledger.monthly_summary()
        except Exception as e:
            # 恢复失败必须显式告警：预算状态从零开始意味着可能超支
            import logging
            logging.getLogger(__name__).error(
                "预算累计从台账恢复失败，日/月累计将按 0 起算（存在超支风险）：%s", e)
            return
        with self._lock:
            self._lazy_rollover_if_needed()
            self._daily_cost = float(day_sum.get("cost", 0.0))
            self._daily_calls = int(day_sum.get("calls", 0))
            self._monthly_cost = float(month_sum.get("cost", 0.0))
            self._monthly_calls = int(month_sum.get("calls", 0))
            import logging
            logging.getLogger(__name__).info(
                "预算累计已从台账恢复：当日 %.6f 元/%d 次，当月 %.6f 元/%d 次。",
                self._daily_cost, self._daily_calls,
                self._monthly_cost, self._monthly_calls)

    def reset_monthly(self) -> None:
        """重置月度累计（外部可选调用，惰性滚动会自行处理）。"""
        self._monthly_cost = 0.0
        self._monthly_calls = 0
        self._monthly_warn_fired = False

    def record(self, cost_cny: float) -> None:
        """记录一次调用费用，并在跨越预警阈值时触发回调。"""
        with self._lock:
            self._lazy_rollover_if_needed()
            self._daily_cost += cost_cny
            self._monthly_cost += cost_cny
            self._daily_calls += 1
            self._monthly_calls += 1
            self._fire_warnings_locked()

    def _fire_warnings_locked(self) -> None:
        """预警触发（须持锁调用；仅告警一次）。"""
        if (self.daily_warn > 0 and not self._daily_warn_fired
                and self._daily_cost >= self.daily_warn):
            self._daily_warn_fired = True
            if self._warn_callback:
                self._warn_callback("daily_warn", self._daily_cost)
        if (self.monthly_warn > 0 and not self._monthly_warn_fired
                and self._monthly_cost >= self.monthly_warn):
            self._monthly_warn_fired = True
            if self._warn_callback:
                self._warn_callback("monthly_warn", self._monthly_cost)

    # ---------- P1-6：原子预留 / 结算 ----------
    def try_reserve(self, cost_estimate: float, calls: int = 1) -> bool:
        """原子地"检查预算 + 预留额度"。

        在同一把锁内完成 can_call 判定与额度/次数预扣，
        避免并发下多个请求同时通过检查导致超支。
        调用结束后必须调用 settle(estimated, actual) 修正差额；
        若调用未发生（请求被拒绝），调用 cancel_reserve(estimated) 回退。

        返回 False 表示预算/次数不允许本次调用（未做任何扣减）。
        """
        est = float(cost_estimate or 0.0)
        with self._lock:
            self._lazy_rollover_if_needed()
            if not self._can_call_locked():
                return False
            # 预估费用也会占用预算（宁可少调不可超支）
            if (self.daily_budget > 0 and self._daily_cost + est > self.daily_budget) \
                    or (self.monthly_budget > 0 and self._monthly_cost + est > self.monthly_budget):
                return False
            self._daily_cost += est
            self._monthly_cost += est
            self._daily_calls += calls
            self._monthly_calls += calls
            self._pending_reserves += 1
            self._fire_warnings_locked()
            return True

    def settle(self, estimated: float, actual: float, calls: int = 1) -> None:
        """按实际 usage 结算：冲回预估、计入实际费用。

        与 try_reserve 配对使用；actual 为 None（费用未知）时保留预估值
        （按最坏情况计，不按 0 静默放行）。
        """
        est = float(estimated or 0.0)
        act = est if actual is None else float(actual or 0.0)
        with self._lock:
            self._lazy_rollover_if_needed()
            self._daily_cost += act - est
            self._monthly_cost += act - est
            self._pending_reserves = max(0, self._pending_reserves - 1)
            self._fire_warnings_locked()

    def cancel_reserve(self, estimated: float, calls: int = 1) -> None:
        """请求最终未发出（如时间窗口关闭）时回退预留。

        批次二注意：只有"确定未发送"才允许退款；已发送但结果未知的
        请求必须改用 settle(estimated, None) 保留占用。
        """
        est = float(estimated or 0.0)
        with self._lock:
            self._lazy_rollover_if_needed()
            self._daily_cost -= est
            self._monthly_cost -= est
            self._daily_calls -= calls
            self._monthly_calls -= calls
            self._pending_reserves = max(0, self._pending_reserves - 1)

    # ---------- 批次二：活跃预留 ----------
    def has_active_reserve(self) -> bool:
        """是否存在进行中的预算预留（已取得调用名额、尚未结算）。

        逐尝试护栏在发送前检查预算时使用：有活跃预留说明本次调用
        名额已在任务开始时原子取得，不得再次按"预算耗尽"拒绝。
        """
        with self._lock:
            return self._pending_reserves > 0

    def _can_call_locked(self) -> bool:
        """预算判定（须持锁调用）。"""
        if self.daily_budget > 0 and self._daily_cost >= self.daily_budget:
            return False
        if self.monthly_budget > 0 and self._monthly_cost >= self.monthly_budget:
            return False
        if self.daily_limit > 0 and self._daily_calls >= self.daily_limit:
            return False
        if self.monthly_limit > 0 and self._monthly_calls >= self.monthly_limit:
            return False
        return True

    def can_call(self) -> bool:
        """检查是否允许继续调用（到达 stop 阈值返回 False）"""
        with self._lock:
            self._lazy_rollover_if_needed()
            return self._can_call_locked()

    def is_warned(self) -> bool:
        """是否已触发日预警阈值（便于上层展示状态）"""
        self._lazy_rollover_if_needed()
        return self._daily_warn_fired

    def reset_daily(self) -> None:
        self._daily_cost = 0.0
        self._daily_calls = 0
        self._daily_warn_fired = False

    @property
    def daily_cost(self) -> float:
        self._lazy_rollover_if_needed()
        return self._daily_cost

    @property
    def monthly_cost(self) -> float:
        self._lazy_rollover_if_needed()
        return self._monthly_cost

    @property
    def daily_calls(self) -> int:
        self._lazy_rollover_if_needed()
        return self._daily_calls

    @property
    def monthly_calls(self) -> int:
        self._lazy_rollover_if_needed()
        return self._monthly_calls
