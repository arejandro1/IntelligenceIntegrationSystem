# -*- coding: utf-8 -*-
"""M3 修复测试：持久化队列（租约/幂等/并发认领/重启恢复）+ 消费者互斥。

不调用真实 API，使用内存后端和 FakeClock。
"""
import threading
import unittest
from datetime import datetime, timedelta

from ServiceComponent.PersistentQueue import (
    MemoryQueueBackend,
    PersistentQueue,
    STATUS_COMPLETED,
    STATUS_DEAD,
    STATUS_PENDING,
    STATUS_PROCESSING,
)
from ServiceComponent.ConsumerMutex import (
    ConsumerMutexError,
    validate_consumer_mutex,
    validate_startup_config,
)


class FakeClock:
    """可注入的假时钟，时间相关测试必须用它。"""
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class TestIdempotentEnqueue(unittest.TestCase):
    """幂等：同一 content_hash 重复入队不产生重复"""
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.q = PersistentQueue(MemoryQueueBackend(), now_fn=self.clock)

    def test_first_enqueue_inserts(self):
        ok = self.q.enqueue("hash-1", {"content": "x"})
        self.assertTrue(ok)
        self.assertEqual(self.q.count_pending(), 1)

    def test_duplicate_enqueue_no_op(self):
        self.q.enqueue("hash-1", {"content": "x"})
        ok = self.q.enqueue("hash-1", {"content": "x"})
        self.assertFalse(ok)  # 幂等命中
        self.assertEqual(self.q.count_pending(), 1)

    def test_different_hashes_both_inserted(self):
        self.q.enqueue("hash-1", {"content": "a"})
        self.q.enqueue("hash-2", {"content": "b"})
        self.assertEqual(self.q.count_pending(), 2)


class TestLeaseAndConcurrentClaim(unittest.TestCase):
    """租约机制 + 并发认领（同一项不得被两个 worker 同时认领）"""
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.q = PersistentQueue(MemoryQueueBackend(), lease_seconds=60, now_fn=self.clock)
        self.q.enqueue("h1", {"content": "task1"})

    def test_claim_marks_processing(self):
        item = self.q.claim(worker_id="w1")
        self.assertIsNotNone(item)
        self.assertEqual(item["status"], STATUS_PROCESSING)
        self.assertEqual(item["worker_id"], "w1")
        self.assertEqual(self.q.count_pending(), 0)
        self.assertEqual(self.q.count_processing(), 1)

    def test_concurrent_claim_only_one_wins(self):
        """两个 worker 同时认领同一队列，只有一个成功"""
        item1 = self.q.claim(worker_id="w1")
        item2 = self.q.claim(worker_id="w2")
        self.assertIsNotNone(item1)
        self.assertIsNone(item2)  # 第二个 worker 应拿不到

    def test_lease_expiry_releases_back_to_pending(self):
        """租约超时自动释放，项回到 PENDING"""
        self.q.claim(worker_id="w1")
        self.assertEqual(self.q.count_processing(), 1)
        # 推进时间超过租约
        self.clock.advance(seconds=61)
        reaped = self.q.reap_expired_leases()
        self.assertEqual(reaped, 1)
        self.assertEqual(self.q.count_pending(), 1)
        self.assertEqual(self.q.count_processing(), 0)

    def test_complete_only_by_owner(self):
        """只有认领者能标记完成"""
        self.q.claim(worker_id="w1")
        # 其他 worker 不能完成
        self.assertFalse(self.q.complete("h1", "w2"))
        self.assertEqual(self.q.count_processing(), 1)
        # owner 能完成
        self.assertTrue(self.q.complete("h1", "w1"))
        self.assertEqual(self.q.count_completed(), 1)


class TestRestartRecovery(unittest.TestCase):
    """重启恢复：未完成项在重启后重新可被认领"""
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.q = PersistentQueue(MemoryQueueBackend(), lease_seconds=60, now_fn=self.clock)

    def test_orphaned_processing_recovers(self):
        self.q.enqueue("h1", {"content": "task1"})
        # 模拟旧进程认领后崩溃：项卡在 PROCESSING
        self.q.claim(worker_id="dead-worker")
        self.assertEqual(self.q.count_processing(), 1)
        # 时间推进超过租约
        self.clock.advance(seconds=120)
        # 重启恢复
        recovered = self.q.recover_on_restart()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.q.count_pending(), 1)
        # 新 worker 能重新认领
        item = self.q.claim(worker_id="new-worker")
        self.assertIsNotNone(item)
        self.assertEqual(item["worker_id"], "new-worker")


class TestRetryAndDead(unittest.TestCase):
    """失败重试与超限进 DEAD"""
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.q = PersistentQueue(MemoryQueueBackend(), max_retries=2, now_fn=self.clock)
        self.q.enqueue("h1", {"content": "x"})

    def test_fail_returns_to_pending_within_retry_limit(self):
        self.q.claim(worker_id="w1")
        ok = self.q.fail("h1", "w1", reason="timeout")
        self.assertTrue(ok)
        self.assertEqual(self.q.count_pending(), 1)

    def test_fail_exceeding_retries_goes_dead(self):
        # 失败 3 次（max_retries=2，第 3 次超限）
        for i in range(3):
            self.q.claim(worker_id=f"w{i}")
            self.q.fail("h1", f"w{i}", reason="err")
            self.clock.advance(seconds=61)
            self.q.reap_expired_leases()
        self.assertEqual(self.q.count_dead(), 1)
        self.assertEqual(self.q.count_pending(), 0)


class TestConsumerMutex(unittest.TestCase):
    """新旧 AI 消费者互斥校验"""
    def test_both_true_must_fail(self):
        with self.assertRaises(ConsumerMutexError):
            validate_consumer_mutex(use_legacy_ai_worker=True, use_off_peak_drainer=True)

    def test_both_false_is_legal_collect_only(self):
        validate_consumer_mutex(use_legacy_ai_worker=False, use_off_peak_drainer=False)

    def test_only_legacy_is_legal(self):
        validate_consumer_mutex(use_legacy_ai_worker=True, use_off_peak_drainer=False)

    def test_only_drainer_is_legal(self):
        validate_consumer_mutex(use_legacy_ai_worker=False, use_off_peak_drainer=True)

    def test_startup_config_rejects_both_true(self):
        cfg = {"use_legacy_ai_worker": True, "use_off_peak_drainer": True}
        with self.assertRaises(ConsumerMutexError):
            validate_startup_config(cfg)

    def test_startup_config_accepts_collect_only(self):
        cfg = {"use_legacy_ai_worker": False, "use_off_peak_drainer": False}
        validate_startup_config(cfg)  # 不抛

    def test_m1_m2_default_config_passes(self):
        """M1/M2 默认配置（两者都 false）必须通过"""
        cfg = {
            "api_enabled": False,
            "api_dry_run": True,
            "use_legacy_ai_worker": False,
            "use_off_peak_drainer": False,
        }
        validate_startup_config(cfg)

    def test_m3_enabled_config_passes(self):
        """M3 人工启用后（仅 drainer）必须通过"""
        cfg = {
            "api_enabled": True,
            "api_dry_run": False,
            "use_legacy_ai_worker": False,
            "use_off_peak_drainer": True,
        }
        validate_startup_config(cfg)


class TestDrainerUsesPersistentQueue(unittest.TestCase):
    """验证 OffPeakQueueDrainer 使用 PersistentQueue（而非内存 queue.Queue）"""
    def test_drainer_queue_is_persistent(self):
        from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
        from ServiceComponent.ApiTimePolicy import ApiTimePolicy
        from ServiceComponent.PricingPolicyRegistry import PricingPolicyRegistry
        from ServiceComponent.BudgetGuard import BudgetGuard
        from ServiceComponent.ApiUsageLedger import ApiUsageLedger
        from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
        from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer
        from ServiceComponent.PersistentQueue import PersistentQueue as PQ

        router = DeepSeekModelRouter(
            ApiTimePolicy(), PricingPolicyRegistry("flash", "pro"),
            BudgetGuard(), ApiUsageLedger(),
            api_enabled=False, api_dry_run=True,
        )
        mock = MockDeepSeekClient(model_name="flash")
        drainer = OffPeakQueueDrainer(router, mock)
        self.assertIsInstance(drainer.queue, PQ)

    def test_drainer_enqueue_idempotent(self):
        from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
        from ServiceComponent.ApiTimePolicy import ApiTimePolicy
        from ServiceComponent.PricingPolicyRegistry import PricingPolicyRegistry
        from ServiceComponent.BudgetGuard import BudgetGuard
        from ServiceComponent.ApiUsageLedger import ApiUsageLedger
        from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
        from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer

        router = DeepSeekModelRouter(
            ApiTimePolicy(), PricingPolicyRegistry("flash", "pro"),
            BudgetGuard(), ApiUsageLedger(),
            api_enabled=False, api_dry_run=True,
        )
        mock = MockDeepSeekClient(model_name="flash")
        drainer = OffPeakQueueDrainer(router, mock)
        ok1 = drainer.enqueue({"content_hash": "h1", "content": "x", "original_uuid": "u1"})
        ok2 = drainer.enqueue({"content_hash": "h1", "content": "x", "original_uuid": "u1"})
        # 批次三修订：enqueue 返回三态字符串（new/duplicate），不再返回 bool
        from ServiceComponent.PersistentQueue import ENQUEUE_DUPLICATE, ENQUEUE_NEW
        self.assertEqual(ok1, ENQUEUE_NEW)
        self.assertEqual(ok2, ENQUEUE_DUPLICATE)  # 幂等：确定重复
        self.assertEqual(drainer.queue_size(), 1)


if __name__ == '__main__':
    unittest.main()