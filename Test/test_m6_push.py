# -*- coding: utf-8 -*-
"""M6/P1-7: 推送服务测试——真实发送语义、持久化队列、重试与禁用状态。

不连接真实推送渠道；sender 全部注入 fake。
覆盖：
- 未配置渠道：明确禁用，不伪报成功；
- submit 幂等（重复入队不重复）；
- 发送成功才确认（COMPLETED）；
- 发送失败有限重试后交还队列/转 DEAD；
- 崩溃恢复（租约回收后重新消费）。
"""
import unittest

from ServiceComponent.PersistentQueue import MemoryQueueBackend, PersistentQueue
from ServiceComponent.PushNotifier import PushNotifier


class FakeSender:
    """可编程 fake 发送器：按脚本依次成功/失败。"""

    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = outcomes or []

    def send(self, payload):
        self.calls.append(payload)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        # 默认成功


def _make_notifier(outcomes=None, config=None):
    queue = PersistentQueue(MemoryQueueBackend())
    sender = FakeSender(outcomes)
    cfg = {"enabled": True, "webhook_url": "https://example.invalid/hook",
           "min_total_score": 7.0, "max_retries": 3,
           "retry_backoff_seconds": 0, "poll_interval_seconds": 0.01}
    cfg.update(config or {})
    notifier = PushNotifier(queue=queue, sender=sender, config=cfg,
                            sleep_fn=lambda s: None)
    return notifier, sender, queue


class TestDisabledState(unittest.TestCase):
    def test_unconfigured_webhook_reports_disabled(self):
        """未配置渠道：is_enabled=False，submit 不伪报成功。"""
        n = PushNotifier(queue=PersistentQueue(MemoryQueueBackend()),
                         config={"enabled": False, "webhook_url": ""})
        self.assertFalse(n.is_enabled())
        self.assertFalse(n.submit({"item_id": "a"}))
        self.assertFalse(n.start(), "禁用状态 start 应拒绝并告警")

    def test_enabled_but_empty_url_disabled(self):
        n = PushNotifier(queue=PersistentQueue(MemoryQueueBackend()),
                         config={"enabled": True, "webhook_url": "   "})
        self.assertFalse(n.is_enabled())


class TestSubmitIdempotent(unittest.TestCase):
    def test_duplicate_submit_single_queue_item(self):
        """重复 submit 幂等：队列只保留一项。"""
        n, sender, queue = _make_notifier()
        self.assertTrue(n.submit({"item_id": "item-1", "title": "t"}))
        self.assertFalse(n.submit({"item_id": "item-1", "title": "t"}))
        self.assertEqual(queue.count_pending(), 1)

    def test_submit_without_item_id_rejected(self):
        n, _, _ = _make_notifier()
        self.assertFalse(n.submit({"title": "no id"}))

    def test_score_threshold_filter(self):
        """should_push 按分数阈值过滤。"""
        n, _, _ = _make_notifier()
        self.assertTrue(n.should_push(8.0))
        self.assertTrue(n.should_push(7.0))
        self.assertFalse(n.should_push(6.9))
        self.assertFalse(n.should_push(None))


class TestDeliver(unittest.TestCase):
    def test_send_success_confirms_pushed(self):
        """发送成功才确认已推送：队列项 COMPLETED。"""
        n, sender, queue = _make_notifier(outcomes=[None])
        n.submit({"item_id": "item-1", "title": "标题"})
        self.assertTrue(n.deliver_once())
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0]["item_id"], "item-1")
        self.assertEqual(queue.count_completed(), 1)
        self.assertEqual(queue.count_pending(), 0)
        self.assertEqual(n.sent_count, 1)

    def test_send_failure_retries_then_requeues(self):
        """发送失败：有限次重试（含退避）后交还队列（PENDING）。"""
        n, sender, queue = _make_notifier(
            outcomes=[RuntimeError("timeout"), RuntimeError("timeout"),
                      RuntimeError("timeout")])
        n.submit({"item_id": "item-2"})
        n.deliver_once()
        # max_retries=3：发送器被调用 3 次后放弃
        self.assertEqual(len(sender.calls), 3)
        # 队列级失败：retries+1，未超 max_retries → 回 PENDING
        self.assertEqual(queue.count_pending(), 1)
        self.assertEqual(n.failed_count, 1)

    def test_retry_then_success(self):
        """前两次失败第三次成功 → 确认推送。"""
        n, sender, queue = _make_notifier(
            outcomes=[RuntimeError("502"), RuntimeError("502"), None])
        n.submit({"item_id": "item-3"})
        n.deliver_once()
        self.assertEqual(len(sender.calls), 3)
        self.assertEqual(queue.count_completed(), 1)
        self.assertEqual(n.sent_count, 1)

    def test_exhausted_retries_go_dead(self):
        """多次 deliver 耗尽队列级 retries → DEAD 终态。"""
        n, sender, queue = _make_notifier(
            outcomes=[RuntimeError("down")] * 30)
        n.submit({"item_id": "item-4"})
        for _ in range(4):  # PersistentQueue max_retries 默认 3
            if queue.count_pending() > 0:
                n.deliver_once()
        self.assertEqual(queue.count_dead(), 1)

    def test_recover_after_crash(self):
        """崩溃恢复：租约过期的 PROCESSING 项回收后重新发送成功。"""
        from datetime import datetime, timedelta
        n, sender, queue = _make_notifier()
        n.submit({"item_id": "item-5"})
        # 模拟崩溃：直接把项置为 PROCESSING 且租约已过期
        item = queue.claim(worker_id="crashed-worker")
        past = datetime.now() - timedelta(seconds=9999)
        queue.backend._items[item["item_id"]]["lease_until"] = past.isoformat()
        # 恢复 + 消费
        queue.recover_on_restart()
        self.assertEqual(queue.count_pending(), 1)
        n.deliver_once()
        self.assertEqual(queue.count_completed(), 1)

    def test_stats_reports_state(self):
        n, _, _ = _make_notifier()
        stats = n.stats()
        self.assertTrue(stats["enabled"])
        self.assertTrue(stats["webhook_configured"])
        self.assertIn("sent", stats)
        self.assertIn("pending", stats)


if __name__ == "__main__":
    unittest.main()
