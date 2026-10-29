# -*- coding: utf-8 -*-
"""M8: 预算守卫测试——日/月预算 + 调用次数上限 + 预警阈值"""
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


class TestBudgetWarn(unittest.TestCase):
    """预警阈值测试：0.2 预警 / 0.3 停止 场景"""
    def test_warn_does_not_stop(self):
        warns = []
        b = BudgetGuard(daily_budget_cny=0.3, daily_warn_cny=0.2,
                        warn_callback=lambda kind, cost: warns.append((kind, cost)))
        b.record(0.2)  # 触发预警
        self.assertTrue(b.is_warned())
        self.assertTrue(b.can_call())  # 预警不停止
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0][0], "daily_warn")

    def test_warn_fires_only_once(self):
        warns = []
        b = BudgetGuard(daily_budget_cny=0.3, daily_warn_cny=0.2,
                        warn_callback=lambda kind, cost: warns.append(kind))
        b.record(0.1)
        b.record(0.1)  # 累计 0.2 触发
        b.record(0.02)  # 累计 0.22，不应再次触发
        self.assertEqual(len(warns), 1)

    def test_stop_at_hard_budget(self):
        b = BudgetGuard(daily_budget_cny=0.3, daily_warn_cny=0.2)
        b.record(0.2)  # 预警
        self.assertTrue(b.can_call())
        b.record(0.1)  # 累计 0.3 停止
        self.assertFalse(b.can_call())

    def test_warn_reset_with_daily(self):
        b = BudgetGuard(daily_budget_cny=0.3, daily_warn_cny=0.2)
        b.record(0.25)
        self.assertTrue(b.is_warned())
        b.reset_daily()
        self.assertFalse(b.is_warned())


class TestDeepSeekClientMocked(unittest.TestCase):
    """DeepSeekClient 重试/退避逻辑测试（mock httpx，不调真实 API）"""
    @staticmethod
    def _patch_sleep_disabled():
        import ServiceComponent.DeepSeekClient as dsc
        orig = dsc.time.sleep
        dsc.time.sleep = lambda x: None
        return orig

    @staticmethod
    def _restore_sleep(orig):
        import ServiceComponent.DeepSeekClient as dsc
        dsc.time.sleep = orig

    @staticmethod
    def _make_client_with_fake(fake_post, max_retries=3):
        """构造一个注入了 fake httpx 的 DeepSeekClient。

        通过 monkeypatch sys.modules 让 `import httpx` 返回 fake 对象，
        从而完整走真实代码路径（包括重试/退避）。
        """
        import sys
        from ServiceComponent.DeepSeekClient import DeepSeekClient

        class _FakeHttpxModule:
            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                return fake_post(url, json, headers, timeout)

        client = DeepSeekClient(api_key="k", max_retries=max_retries)
        # 临时把 fake 注入 sys.modules，使 client.chat_completion 内 `import httpx` 命中
        prev = sys.modules.get("httpx")
        sys.modules["httpx"] = _FakeHttpxModule  # type: ignore
        return client, prev

    @staticmethod
    def _restore_httpx(prev):
        import sys
        if prev is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = prev

    def test_success(self):
        calls = {"n": 0}
        def fake_post(url, json, headers, timeout):
            calls["n"] += 1
            class R:
                status_code = 200
                def json(self_inner):
                    return {"id": "x", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
            return R()
        client, prev = self._make_client_with_fake(fake_post)
        try:
            resp = client.chat_completion(messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(calls["n"], 1)
            self.assertEqual(resp["usage"]["prompt_tokens"], 5)
        finally:
            self._restore_httpx(prev)

    def test_non_retryable_400_raises(self):
        def fake_post(url, json, headers, timeout):
            class R:
                status_code = 400
                def json(self_inner):
                    return {"error": "bad"}
            return R()
        client, prev = self._make_client_with_fake(fake_post)
        orig_sleep = self._patch_sleep_disabled()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                client.chat_completion(messages=[{"role": "user", "content": "hi"}])
            self.assertIn("不可重试", str(ctx.exception))
        finally:
            self._restore_sleep(orig_sleep)
            self._restore_httpx(prev)

    def test_5xx_retries_then_success(self):
        calls = {"n": 0}
        def fake_post(url, json, headers, timeout):
            calls["n"] += 1
            class R:
                status_code = 503 if calls["n"] < 3 else 200
                def json(self_inner):
                    return {"id": "x", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            return R()
        client, prev = self._make_client_with_fake(fake_post)
        orig_sleep = self._patch_sleep_disabled()
        try:
            resp = client.chat_completion(messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(calls["n"], 3)  # 2 次 503 + 1 次 200
            self.assertEqual(resp["usage"]["prompt_tokens"], 1)
        finally:
            self._restore_sleep(orig_sleep)
            self._restore_httpx(prev)

    def test_missing_api_key_rejected(self):
        from ServiceComponent.DeepSeekClient import DeepSeekClient
        with self.assertRaises(ValueError):
            DeepSeekClient(api_key="")


if __name__ == '__main__':
    unittest.main()
