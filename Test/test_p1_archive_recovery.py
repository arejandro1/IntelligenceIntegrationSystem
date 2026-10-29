# -*- coding: utf-8 -*-
"""P1-5: 分析完成但未归档的恢复断点测试。

覆盖：
- complete ≠ delivered：回调成功才标记 DELIVERED；
- 崩溃/回调异常/队列满 → 保持未交付，重放不重复调用 AI；
- 历史记录兼容（delivery_status 缺失视为未交付，交付一次后不再重复）；
- 重启重放（recover + replay_undelivered）；
- 租约过期回收（数据库暂不可用后的恢复）；
- DEAD 终态可受控重试（requeue_dead）。
"""
import unittest
from datetime import datetime, timedelta

from ServiceComponent.ApiTimePolicy import ApiTimePolicy
from ServiceComponent.ApiUsageLedger import ApiUsageLedger
from ServiceComponent.BudgetGuard import BudgetGuard
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer
from ServiceComponent.PersistentQueue import (
    STATUS_COMPLETED,
    STATUS_DEAD,
    STATUS_PENDING,
    MemoryQueueBackend,
    PersistentQueue,
)
from ServiceComponent.PricingPolicyRegistry import ModelPricing, PricingPolicyRegistry


def _make_router(api_enabled=True, api_dry_run=True):
    pricing = PricingPolicyRegistry(
        flash_model="flash", pro_model="pro",
        flash_pricing=ModelPricing(model_name="flash", is_flash=True),
        pro_pricing=ModelPricing(model_name="pro", is_flash=False))
    return DeepSeekModelRouter(ApiTimePolicy(), pricing, BudgetGuard(),
                               ApiUsageLedger(),
                               api_enabled=api_enabled, api_dry_run=api_dry_run)


def _make_drainer(router=None, on_complete=None, invoke_handler=None):
    router = router or _make_router()
    queue = PersistentQueue(MemoryQueueBackend())
    return OffPeakQueueDrainer(
        router=router,
        mock_client=MockDeepSeekClient(),
        drain_interval_seconds=0,
        max_per_minute=100,
        persistent_queue=queue,
        invoke_handler=invoke_handler,
        on_complete=on_complete), queue


def _business_result(uuid):
    return {"model": "flash", "is_dry_run": True,
            "business": {"UUID": uuid, "EVENT_TITLE": "t"}}


class TestDeliveryStatus(unittest.TestCase):
    def test_complete_then_deliver_marks_delivered(self):
        """complete 后交付成功才 DELIVERED。"""
        delivered = []

        def on_complete(item_id, result):
            delivered.append(item_id)
            return True

        d, q = _make_drainer(on_complete=on_complete)
        q.enqueue("hash-1", {"content": "abc", "original_uuid": "u1"})
        item = q.claim(worker_id=d.worker_id)
        result = _business_result("u1")
        q.complete(item["item_id"], d.worker_id, result=result)
        # 尚未交付：仍在未交付列表
        self.assertEqual(len(q.list_completed_undelivered()), 1)
        self.assertTrue(d._deliver_result(item, result))
        self.assertEqual(q.list_completed_undelivered(), [])
        self.assertEqual(delivered, [item["item_id"]])

    def test_callback_exception_keeps_undelivered(self):
        """回调异常 → 未交付保留（等待重放），不丢结果。"""
        def on_complete(item_id, result):
            raise RuntimeError("archive db down")

        d, q = _make_drainer(on_complete=on_complete)
        q.enqueue("hash-2", {"content": "abc", "original_uuid": "u2"})
        item = q.claim(worker_id=d.worker_id)
        q.complete(item["item_id"], d.worker_id, result=_business_result("u2"))
        self.assertFalse(d._deliver_result(item, _business_result("u2")))
        self.assertEqual(len(q.list_completed_undelivered()), 1)

    def test_callback_reject_keeps_undelivered(self):
        """回调返回 False（如归档队列满）→ 未交付保留。"""
        d, q = _make_drainer(on_complete=lambda i, r: False)
        q.enqueue("hash-3", {"content": "abc", "original_uuid": "u3"})
        item = q.claim(worker_id=d.worker_id)
        q.complete(item["item_id"], d.worker_id, result=_business_result("u3"))
        self.assertFalse(d._deliver_result(item, _business_result("u3")))
        self.assertEqual(len(q.list_completed_undelivered()), 1)


class TestReplay(unittest.TestCase):
    def test_replay_redelivers_without_ai_recall(self):
        """重放只重交付，不重复调用 AI。"""
        calls = {"invoke": 0}
        delivered = []

        def invoke(task):
            calls["invoke"] += 1
            resp = MockDeepSeekClient().chat_completion(
                messages=[{"role": "user", "content": task["content"]}])
            return resp, {"UUID": task.get("original_uuid"), "EVENT_TITLE": "t"}

        def on_complete(item_id, result):
            delivered.append(item_id)
            return True

        d, q = _make_drainer(on_complete=on_complete, invoke_handler=invoke)
        q.enqueue("hash-4", {"content": "abc", "original_uuid": "u4"})
        # 第一次处理：AI 调用一次，complete 但模拟崩溃（未交付）
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        q.complete(item["item_id"], d.worker_id, result=result)
        self.assertEqual(calls["invoke"], 1)

        # “重启”：新 drainer 实例共享同一队列，重放交付
        d2, _ = _make_drainer(on_complete=on_complete, invoke_handler=invoke)
        d2.queue = q
        delivered.clear()
        d2.replay_undelivered()
        self.assertEqual(delivered, [item["item_id"]])
        # 重放绝不重新调用 AI
        self.assertEqual(calls["invoke"], 1, "重放不得重复调用 AI")

    def test_replay_failure_keeps_for_next_round(self):
        """重放交付失败（归档库暂不可用）→ 保留，下一轮再试。"""
        attempts = {"n": 0}

        def flaky_on_complete(item_id, result):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("db unavailable")
            return True

        d, q = _make_drainer(on_complete=flaky_on_complete)
        q.enqueue("hash-5", {"content": "abc", "original_uuid": "u5"})
        item = q.claim(worker_id=d.worker_id)
        q.complete(item["item_id"], d.worker_id,
                   result=_business_result("u5"))
        self.assertEqual(d.replay_undelivered(), 0, "首轮失败不计成功")
        self.assertEqual(len(q.list_completed_undelivered()), 1)
        self.assertEqual(d.replay_undelivered(), 1, "第二轮成功交付")
        self.assertEqual(q.list_completed_undelivered(), [])

    def test_historical_records_compat(self):
        """历史队列记录（无 delivery_status）视为未交付，交付一次即止。"""
        delivered = []

        def on_complete(item_id, result):
            delivered.append(item_id)
            return True

        d, q = _make_drainer(on_complete=on_complete)
        # 手工构造历史文档：无 delivery_status 字段
        q.backend._items["old-1"] = {
            "_id": "old-1", "item_id": "old-1", "content_hash": "old-1",
            "payload": {"content": "x"}, "status": STATUS_COMPLETED,
            "result": _business_result("u-old"),
            "enqueued_at": "2026-01-01T00:00:00",
        }
        self.assertEqual(len(q.list_completed_undelivered()), 1)
        d.replay_undelivered()
        self.assertEqual(delivered, ["old-1"])
        self.assertEqual(q.list_completed_undelivered(), [], "交付后不再重复")

    def test_history_without_business_skipped(self):
        """无 business 结果的历史记录跳过（无可交付数据）。"""
        d, q = _make_drainer()
        q.backend._items["old-2"] = {
            "_id": "old-2", "item_id": "old-2", "content_hash": "old-2",
            "payload": {}, "status": STATUS_COMPLETED,
            "result": {"model": "flash"},  # 无 business
            "enqueued_at": "2026-01-01T00:00:00",
        }
        self.assertEqual(q.list_completed_undelivered(), [])


class TestLeaseAndTerminalState(unittest.TestCase):
    def test_expired_lease_requeued(self):
        """租约过期回收：崩溃 worker 的处理中项回到 PENDING。"""
        d, q = _make_drainer()
        q.enqueue("hash-6", {"content": "abc", "original_uuid": "u6"})
        item = q.claim(worker_id="crashed-worker")
        self.assertEqual(q.count_pending(), 0)
        # 租约置为过去
        q.backend._items[item["item_id"]]["lease_until"] = (
            datetime.now() - timedelta(seconds=9999)).isoformat()
        reaped = q.reap_expired_leases()
        self.assertEqual(reaped, 1)
        self.assertEqual(q.count_pending(), 1)

    def test_lost_lease_cannot_complete(self):
        """失去租约（worker 不匹配）后不能再 complete（防双写）。"""
        d, q = _make_drainer()
        q.enqueue("hash-7", {"content": "abc", "original_uuid": "u7"})
        item = q.claim(worker_id=d.worker_id)
        # 模拟租约被回收/转移
        q.backend._items[item["item_id"]]["worker_id"] = None
        self.assertFalse(q.complete(item["item_id"], d.worker_id, result={}))

    def test_dead_terminal_and_requeue(self):
        """不可恢复任务 → DEAD 终态；可受控重试回 PENDING。"""
        d, q = _make_drainer()
        q.enqueue("hash-8", {"content": "abc", "original_uuid": "u8"})
        for _ in range(4):  # PersistentQueue max_retries=3 → 第 4 次 fail 进 DEAD
            claimed = q.claim(worker_id=d.worker_id)
            if claimed is None:
                break
            q.fail(claimed["item_id"], d.worker_id, "boom")
        self.assertEqual(q.count_dead(), 1)
        # 受控重试：DEAD → PENDING，retries 清零
        self.assertTrue(q.requeue_dead("hash-8"))
        self.assertEqual(q.count_pending(), 1)


if __name__ == "__main__":
    unittest.main()
