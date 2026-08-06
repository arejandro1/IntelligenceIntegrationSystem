# -*- coding: utf-8 -*-
"""
ApiTimePolicy 峰谷时间策略（M3）
职责：判断当前时间是否允许调用 DeepSeek API。
高峰期禁止 Flash/Pro/API重试/付费健康检查。
时间窗口、Guard、时区必须配置化。
"""
from datetime import datetime, time
from typing import List, Tuple, Optional
from zoneinfo import ZoneInfo


class ApiTimePolicy:
    """DeepSeek 峰谷时间策略，fail-closed 设计。"""

    def __init__(self,
                 timezone: str = "Asia/Shanghai",
                 peak_windows: Optional[List[Tuple[str, str]]] = None,
                 off_peak_start_guard_minutes: int = 1,
                 off_peak_end_guard_minutes: int = 10,
                 policy_version: str = "",
                 effective_from: Optional[datetime] = None,
                 enabled: bool = True):
        self.timezone = ZoneInfo(timezone)
        self.start_guard = off_peak_start_guard_minutes
        self.end_guard = off_peak_end_guard_minutes
        self.policy_version = policy_version
        self.effective_from = effective_from
        self.enabled = enabled

        # 默认高峰窗口（配置化覆盖）
        self.peak_windows: List[Tuple[time, time]] = []
        if peak_windows is None:
            peak_windows = [
                ("09:00", "12:00"),
                ("14:00", "18:00"),
            ]
        for start_str, end_str in peak_windows:
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            self.peak_windows.append((time(sh, sm), time(eh, em)))

    def now(self) -> datetime:
        """获取当前时间（北京时间），可注入 FakeClock 用于测试"""
        return datetime.now(self.timezone)

    def is_peak(self, at: Optional[datetime] = None) -> bool:
        """判断指定时间是否在高峰时段内"""
        t = (at or self.now()).timetz()
        for start, end in self.peak_windows:
            if start <= t < end:
                return True
        return False

    def allowed_to_start_request(self, at: Optional[datetime] = None) -> bool:
        """
        判断当前时刻是否允许启动新的 API 请求。
        考虑 Guard 窗口：
        - 非高峰开始后 start_guard 分钟才允许
        - 非高峰结束前 end_guard 分钟停止新请求
        """
        if not self.enabled:
            return False

        if self.effective_from is not None and self.now() < self.effective_from:
            return False

        now_dt = at or self.now()
        t = now_dt.time()

        # 定义非高峰窗口
        off_peak_windows = [
            (time(0, 0), time(9, 0)),
            (time(12, 0), time(14, 0)),
            (time(18, 0), time(23, 59, 59)),
        ]

        for start, end in off_peak_windows:
            guard_start = self._add_minutes(start, self.start_guard)
            guard_end = self._add_minutes(end, -self.end_guard)
            if guard_start <= t < guard_end:
                return True

        return False

    @staticmethod
    def _add_minutes(t: time, minutes: int) -> time:
        """时间加减分钟（仅限同一天内）"""
        total_minutes = t.hour * 60 + t.minute + minutes
        total_minutes = max(0, min(total_minutes, 1439))
        h, m = divmod(total_minutes, 60)
        return time(h, m)