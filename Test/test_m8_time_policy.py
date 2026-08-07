# -*- coding: utf-8 -*-
"""M8: 峰谷时间策略测试（FakeClock 注入验证）"""
import unittest
from datetime import datetime, time
from ServiceComponent.ApiTimePolicy import ApiTimePolicy


class FakeClockPolicy(ApiTimePolicy):
    """可注入时钟的 ApiTimePolicy"""
    def __init__(self, fixed_now: datetime, **kwargs):
        super().__init__(**kwargs)
        self._fixed_now = fixed_now

    def now(self) -> datetime:
        return self._fixed_now


class TestPeakHours(unittest.TestCase):
    """高峰时段判定测试"""
    def test_morning_peak(self):
        p = FakeClockPolicy(datetime(2026, 8, 8, 10, 0, 0))
        self.assertTrue(p.is_peak())

    def test_afternoon_peak(self):
        p = FakeClockPolicy(datetime(2026, 8, 8, 15, 30, 0))
        self.assertTrue(p.is_peak())

    def test_lunch_off_peak(self):
        p = FakeClockPolicy(datetime(2026, 8, 8, 13, 0, 0))
        self.assertFalse(p.is_peak())

    def test_midnight_off_peak(self):
        p = FakeClockPolicy(datetime(2026, 8, 8, 2, 0, 0))
        self.assertFalse(p.is_peak())

    def test_evening_off_peak(self):
        p = FakeClockPolicy(datetime(2026, 8, 8, 20, 0, 0))
        self.assertFalse(p.is_peak())


class TestGuardWindows(unittest.TestCase):
    """Guard 窗口边界测试"""
    def test_allowed_midnight(self):
        # 00:01 在 [00:01, 08:50) 内
        p = FakeClockPolicy(datetime(2026, 8, 8, 0, 1, 0))
        self.assertTrue(p.allowed_to_start_request())

    def test_blocked_0855(self):
        # 08:55 不在任何允许窗口
        p = FakeClockPolicy(datetime(2026, 8, 8, 8, 55, 0))
        self.assertFalse(p.allowed_to_start_request())

    def test_allowed_1205(self):
        # 12:05 在 [12:01, 13:50) 内
        p = FakeClockPolicy(datetime(2026, 8, 8, 12, 5, 0))
        self.assertTrue(p.allowed_to_start_request())

    def test_blocked_1355(self):
        # 13:55 不在任何允许窗口
        p = FakeClockPolicy(datetime(2026, 8, 8, 13, 55, 0))
        self.assertFalse(p.allowed_to_start_request())

    def test_allowed_1805(self):
        # 18:05 在 [18:01, 23:59:59) 内（考虑 END_GUARD=10）
        p = FakeClockPolicy(datetime(2026, 8, 8, 18, 5, 0))
        self.assertTrue(p.allowed_to_start_request())

    def test_peak_blocked_during_request(self):
        # 高峰期间 allowed_to_start_request 必须 false
        p = FakeClockPolicy(datetime(2026, 8, 8, 10, 0, 0))
        self.assertFalse(p.allowed_to_start_request())

    def test_disabled_policy(self):
        p = FakeClockPolicy(datetime(2026, 8, 8, 2, 0, 0), enabled=False)
        self.assertFalse(p.allowed_to_start_request())

    def test_effective_from_future(self):
        from datetime import timedelta
        future = datetime(2026, 8, 8, 2, 0, 0) + timedelta(days=1)
        p = FakeClockPolicy(datetime(2026, 8, 8, 2, 0, 0), effective_from=future)
        self.assertFalse(p.allowed_to_start_request())


if __name__ == '__main__':
    unittest.main()