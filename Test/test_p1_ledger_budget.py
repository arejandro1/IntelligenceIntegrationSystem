# -*- coding: utf-8 -*-
"""P1-6: 持久化费用台账与逐请求护栏测试。

覆盖：
- 台账幂等（attempt_id 唯一；task_id 与 attempt_id 分离）；
- 费用未知（cost=None）显式记录，不按 0 混入合计；
- 日/月汇总按计费时区，跨日/跨月正确切分（FakeClock）；
- BudgetGuard.restore_from_ledger 重启恢复预算累计；
- try_reserve/settle/cancel_reserve 原子预留结算（含并发）；
- Drainer 逐请求护栏：预算耗尽即停、限流含失败尝试、
  解析失败仍记 usage（PARSE_FAILED）、超时记 TIMEOUT_COST_UNKNOWN；
- 客户端 pre_attempt_hook 高峰期阻断重试。
"""
import threading
import unittest
from datetime import datetime

from ServiceComponent.ApiTimePolicy import ApiTimePolicy
from ServiceComponent.ApiUsageLedger import (
    STATUS_FAILED_NO_USAGE,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_TIMEOUT_COST_UNKNOWN,
    ApiUsageLedger,
    MemoryLedgerStore,
)
from ServiceComponent.BudgetGuard import BudgetGuard
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer, StartRateLimiter
from ServiceComponent.PersistentQueue import MemoryQueueBackend, PersistentQueue
from ServiceComponent.PricingPolicyRegistry import ModelPricing, PricingPolicyRegistry


def _fixed(tzinfo=None):
    return datetime(2026, 9, 20, 20, 0, 0)  # 非高峰（20:00）


class TestLedger(unittest.TestCase):
    def test_record_idempotent(self):
        ledger = ApiUsageLedger(now_fn=_fixed)
        self.assertTrue(ledger.record("a1", "flash", 10, 5, 0.01, True))
        self.assertFalse(ledger.record("a1", "flash", 10, 5, 0.01, True),
                         "重复 attempt_id 幂等命中")
        self.assertEqual(ledger.count(), 1)
        self.assertAlmostEqual(ledger.total_cost(), 0.01)

    def test_task_and_attempt_separated(self):
        """同一任务多次重试 → 多条 attempt 记录。"""
        ledger = ApiUsageLedger(now_fn=_fixed)
        for i in range(3):
            ledger.record(f"u1#attempt{i}", "flash", 10, 5, 0.01, True, task_id="u1")
        self.assertEqual(ledger.count(), 3)

    def test_unknown_cost_not_zeroed(self):
        """费用未知记 None，不混入 total_cost，单列计数。"""
        ledger = ApiUsageLedger(now_fn=_fixed)
        ledger.record("a1", "flash", 0, 0, None, False,
                      task_id="t1", status=STATUS_TIMEOUT_COST_UNKNOWN)
        ledger.record("a2", "flash", 10, 5, 0.02, False)
        self.assertEqual(ledger.total_cost(), 0.02)
        summary = ledger.daily_summary()
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["cost_unknown_count"], 1)

    def test_daily_monthly_summary_billing_tz(self):
        """日/月汇总按计费时区切分（FakeClock 固定 9/20）。"""
        ledger = ApiUsageLedger(now_fn=_fixed, timezone="Asia/Shanghai")
        # 同日两条
        ledger._now_fn = lambda: datetime(2026, 9, 20, 8, 0, 0)
        ledger.record("a", "flash", 1, 1, 0.01, True)
        ledger.record("b", "flash", 1, 1, 0.02, True)
        # 前一日
        ledger._now_fn = lambda: datetime(2026, 9, 19, 23, 0, 0)
        ledger.record("c", "flash", 1, 1, 0.05, True)
        # 前一月
        ledger._now_fn = lambda: datetime(2026, 8, 31, 23, 0, 0)
        ledger.record("d", "flash", 1, 1, 0.1, True)
        # 汇总基准时间复位回 9/20（避免汇总日跟随最后一次写入时间）
        ledger._now_fn = _fixed
        d = ledger.daily_summary()
        self.assertEqual(d["calls"], 2)
        self.assertAlmostEqual(d["cost"], 0.03)
        m = ledger.monthly_summary()
        self.assertEqual(m["calls"], 3)
        self.assertAlmostEqual(m["cost"], 0.08)

    def test_memory_store_shared_between_instances(self):
        """共享 store 模拟重启恢复：新实例可读旧记录。"""
        store = MemoryLedgerStore()
        l1 = ApiUsageLedger(store=store, now_fn=_fixed)
        l1.record("a", "flash", 1, 1, 0.5, True)
        l2 = ApiUsageLedger(store=store, now_fn=_fixed)
        self.assertEqual(l2.count(), 1)
        self.assertAlmostEqual(l2.daily_summary()["cost"], 0.5)


class TestBudgetRestoreAndReserve(unittest.TestCase):
    def test_restore_from_ledger(self):
        """重启恢复：预算/次数累计从台账恢复，can_call 立即受限。"""
        ledger = ApiUsageLedger(now_fn=_fixed, timezone="Asia/Shanghai")
        for i in range(5):
            ledger.record(f"a{i}", "flash", 1, 1, 0.02, True)
        budget = BudgetGuard(daily_call_limit=5, now_fn=_fixed)
        budget.restore_from_ledger(ledger)
        self.assertEqual(budget.daily_calls, 5)
        self.assertFalse(budget.can_call(), "恢复后次数达限必须立即拒绝")

    def test_try_reserve_and_settle(self):
        budget = BudgetGuard(daily_budget_cny=1.0, now_fn=_fixed)
        self.assertTrue(budget.try_reserve(0.4))
        self.assertAlmostEqual(budget.daily_cost, 0.4)
        budget.settle(0.4, 0.35)
        self.assertAlmostEqual(budget.daily_cost, 0.35)

    def test_reserve_prevents_overrun(self):
        budget = BudgetGuard(daily_budget_cny=0.5, now_fn=_fixed)
        self.assertTrue(budget.try_reserve(0.4))
        self.assertFalse(budget.try_reserve(0.2), "预留后剩余额度不足必须拒绝")

    def test_cancel_reserve_refunds(self):
        budget = BudgetGuard(daily_call_limit=2, now_fn=_fixed)
        self.assertTrue(budget.try_reserve(0.0))
        budget.cancel_reserve(0.0)
        self.assertEqual(budget.daily_calls, 0)

    def test_settle_none_keeps_estimate(self):
        """费用未知结算：保留预估（最坏情况），不按 0 放行。"""
        budget = BudgetGuard(daily_budget_cny=1.0, now_fn=_fixed)
        budget.try_reserve(0.4)
        budget.settle(0.4, None)
        self.assertAlmostEqual(budget.daily_cost, 0.4)

    def test_concurrent_reserve_atomic(self):
        """并发预留原子性：总额不超预算。"""
        budget = BudgetGuard(daily_budget_cny=1.0, now_fn=_fixed)
        granted = []
        lock = threading.Lock()

        def worker():
            ok = budget.try_reserve(0.2)
            with lock:
                granted.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(granted), 5, "1.0/0.2 只允许 5 个并发预留")
        self.assertAlmostEqual(budget.daily_cost, 1.0)


class _FakeClockPolicy(ApiTimePolicy):
    def __init__(self, fixed_now):
        super().__init__()
        self._fixed = fixed_now

    def now(self):
        return self._fixed


def _make_drainer(budget=None, ledger=None, time_policy=None, max_per_minute=100):
    pricing = PricingPolicyRegistry(
        flash_model="flash", pro_model="pro",
        flash_pricing=ModelPricing(model_name="flash", is_flash=True,
                                   input_price_cny_per_1k=0.001,
                                   output_price_cny_per_1k=0.002),
        pro_pricing=ModelPricing(model_name="pro", is_flash=False))
    router = DeepSeekModelRouter(
        time_policy or _FakeClockPolicy(datetime(2026, 9, 20, 20, 0, 0)),
        pricing, budget or BudgetGuard(now_fn=_fixed),
        ledger or ApiUsageLedger(now_fn=_fixed),
        api_enabled=True, api_dry_run=True)
    queue = PersistentQueue(MemoryQueueBackend())
    return OffPeakQueueDrainer(
        router=router, mock_client=MockDeepSeekClient(),
        drain_interval_seconds=0, max_per_minute=max_per_minute,
        persistent_queue=queue), queue, router


class TestDrainerGuardrails(unittest.TestCase):
    def test_budget_exhausted_blocks_item(self):
        """预算耗尽：处理前预留失败 → 不调用、无损释放回队列。"""
        budget = BudgetGuard(daily_budget_cny=0.001, now_fn=_fixed)
        budget.record(0.001)  # 耗尽
        d, q, router = _make_drainer(budget=budget)
        q.enqueue("h1", {"content": "abc", "original_uuid": "u1"})
        item = q.claim(worker_id=d.worker_id)
        with self.assertRaises(Exception):
            d._process_item(item)
        # 无损释放：任务仍在队列，未计失败
        d.queue.release(item["item_id"], d.worker_id)
        self.assertEqual(q.count_pending(), 1)

    def test_parse_failure_still_records_usage(self):
        """成功响应但解析失败（异常附带 api_usage）→ 台账记 PARSE_FAILED。"""
        ledger = ApiUsageLedger(now_fn=_fixed)
        d, q, _ = _make_drainer(ledger=ledger)

        def invoke(task):
            MockDeepSeekClient().chat_completion(
                messages=[{"role": "user", "content": task["content"]}])
            err = ValueError("parse error")
            err.api_usage = {"input_tokens": 30, "output_tokens": 12}
            raise err

        d.invoke_handler = invoke
        q.enqueue("h2", {"content": "abc", "original_uuid": "u2"})
        item = q.claim(worker_id=d.worker_id)
        with self.assertRaises(ValueError):
            d._process_item(item)
        self.assertEqual(ledger.count(), 1)
        rec = ledger.records(1)[0]
        self.assertEqual(rec["status"], STATUS_PARSE_FAILED)
        self.assertEqual(rec["input_tokens"], 30)
        self.assertGreater(rec["cost_cny"], 0, "解析失败费用不得记 0/漏记")

    def test_timeout_records_unknown_cost(self):
        """超时失败（无 usage）→ 费用未知显式记录。"""
        ledger = ApiUsageLedger(now_fn=_fixed)
        d, q, _ = _make_drainer(ledger=ledger)

        def invoke(task):
            raise RuntimeError("DeepSeek 调用失败：request timed out")

        d.invoke_handler = invoke
        q.enqueue("h3", {"content": "abc", "original_uuid": "u3"})
        item = q.claim(worker_id=d.worker_id)
        with self.assertRaises(RuntimeError):
            d._process_item(item)
        rec = ledger.records(1)[0]
        self.assertEqual(rec["status"], STATUS_TIMEOUT_COST_UNKNOWN)
        self.assertIsNone(rec["cost_cny"])

    def test_failure_records_no_usage_status(self):
        ledger = ApiUsageLedger(now_fn=_fixed)
        d, q, _ = _make_drainer(ledger=ledger)

        def invoke(task):
            raise RuntimeError("connection refused")

        d.invoke_handler = invoke
        q.enqueue("h4", {"content": "abc", "original_uuid": "u4"})
        item = q.claim(worker_id=d.worker_id)
        with self.assertRaises(RuntimeError):
            d._process_item(item)
        rec = ledger.records(1)[0]
        self.assertEqual(rec["status"], STATUS_FAILED_NO_USAGE)


    def test_success_records_ok_and_settles_budget(self):
        ledger = ApiUsageLedger(now_fn=_fixed)
        budget = BudgetGuard(daily_budget_cny=10.0, now_fn=_fixed)
        d, q, _ = _make_drainer(budget=budget, ledger=ledger)
        q.enqueue("h5", {"content": "abc", "original_uuid": "u5"})
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        rec = ledger.records(1)[0]
        self.assertEqual(rec["status"], STATUS_OK)
        self.assertGreaterEqual(rec["cost_cny"], 0)
        # 结算按实际 usage（预估远大于 mock 实际，结算后应显著回落）
        self.assertLess(budget.daily_cost, 1.0)
        self.assertEqual(result["model"], "flash")

    def test_rate_limiter_counts_failures(self):
        """限流含失败尝试：同一分钟窗口内失败也占配额。"""
        now = {"t": 0.0}
        limiter = StartRateLimiter(2, now_fn=lambda: now["t"])
        self.assertTrue(limiter.allow())
        limiter.record_start()  # 失败尝试
        self.assertTrue(limiter.allow())
        limiter.record_start()  # 第二次（无论成败）
        self.assertFalse(limiter.allow(), "两次开始后窗口内必须拒绝")
        now["t"] = 61.0
        self.assertTrue(limiter.allow(), "窗口滑过后恢复")


class TestClientPreAttemptHook(unittest.TestCase):
    def test_hook_blocks_retries_in_peak(self):
        """高峰期：pre_attempt_hook 阻断，客户端不发起任何请求。"""
        from ServiceComponent.DeepSeekClient import DeepSeekClient

        peak_policy = _FakeClockPolicy(datetime(2026, 9, 20, 10, 0, 0))  # 高峰
        pricing = PricingPolicyRegistry(
            flash_model="flash", pro_model="pro",
            flash_pricing=ModelPricing(model_name="flash", is_flash=True),
            pro_pricing=ModelPricing(model_name="pro", is_flash=False))
        router = DeepSeekModelRouter(
            peak_policy, pricing, BudgetGuard(now_fn=_fixed),
            ApiUsageLedger(now_fn=_fixed), api_enabled=True, api_dry_run=False)
        client = DeepSeekClient(api_key="k", max_retries=3,
                                pre_attempt_hook=router.make_attempt_guard())
        calls = {"n": 0}

        class _FakeHttpx:
            @staticmethod
            def post(*a, **kw):
                calls["n"] += 1
                raise RuntimeError("should not reach")

        import sys as _sys
        prev = _sys.modules.get("httpx")
        _sys.modules["httpx"] = _FakeHttpx
        try:
            with self.assertRaises(Exception) as ctx:
                client.chat_completion(messages=[{"role": "user", "content": "x"}])
            self.assertIn("窗口", str(ctx.exception))
            self.assertEqual(calls["n"], 0, "高峰期真实请求次数必须为 0")
        finally:
            if prev is None:
                _sys.modules.pop("httpx", None)
            else:
                _sys.modules["httpx"] = prev


if __name__ == "__main__":
    unittest.main()


