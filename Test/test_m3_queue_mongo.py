# -*- coding: utf-8 -*-
"""
M3 补充测试：MongoQueueBackend 语义（使用 mock collection，不连接真实 MongoDB）

验证 Mongo 后端与 Memory 后端语义一致：
- 幂等入队（_id 唯一）
- 原子认领（find_one_and_update）
- 完成/失败仅限 owner
- 租约过期回收
- 重启恢复
"""
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from ServiceComponent.PersistentQueue import (
    MongoQueueBackend,
    PersistentQueue,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_DEAD,
)


class FakeMongoCollection:
    """极简 fake collection：模拟 find_one_and_update / update_one / count_documents 语义。

    用 Python 字典模拟 MongoDB 单文档原子操作的行为。
    """

    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}

    def create_index(self, *args, **kwargs):
        """MongoQueueBackend.__init__ 会调用 create_index 建索引；测试中忽略。"""
        return None

    # ---- 模拟 Mongo 方法 ----
    def insert_one(self, doc: Dict[str, Any]):
        _id = doc["_id"]
        if _id in self._docs:
            # 与真实 MongoDB 行为对齐：抛带 11000 错误码的 DuplicateKeyError，
            # 使 PersistentQueue.insert_if_absent 的三种重复键信号均能命中。
            raise DuplicateKeyError("duplicate key error: E11000 duplicate key", code=11000)
        self._docs[_id] = dict(doc)

    def find_one_and_update(self, query: Dict[str, Any], update: Dict[str, Any],
                            sort=None, return_document=None):
        """模拟原子 find_one_and_update：按 query 匹配第一个 PENDING 并更新。"""
        import copy
        # 找到符合 query 的第一条（排序按 enqueued_at 升序）
        matched_key = None
        matched_doc = None
        for _id, doc in self._docs.items():
            if all(doc.get(k) == v for k, v in query.items()):
                if matched_doc is None or doc.get("enqueued_at", "") < matched_doc.get("enqueued_at", ""):
                    matched_key = _id
                    matched_doc = doc

        if matched_key is None:
            return None

        new_doc = dict(matched_doc)
        if "$set" in update:
            new_doc.update(update["$set"])
        if "$inc" in update:
            for k, v in update["$inc"].items():
                new_doc[k] = new_doc.get(k, 0) + v
        self._docs[matched_key] = new_doc
        return dict(new_doc)

    def update_one(self, query: Dict[str, Any], update: Dict[str, Any]):
        """模拟 update_one：匹配第一条并更新。"""
        matched = None
        for _id, doc in self._docs.items():
            if all(doc.get(k) == v for k, v in query.items()):
                matched = _id
                break
        if matched is None:
            return _FakeResult(0)
        new_doc = dict(self._docs[matched])
        if "$set" in update:
            new_doc.update(update["$set"])
        self._docs[matched] = new_doc
        return _FakeResult(1)

    def update_many(self, query: Dict[str, Any], update: Dict[str, Any]):
        def _matches(doc: Dict[str, Any]) -> bool:
            for k, cond in query.items():
                if isinstance(cond, dict) and any(op.startswith('$') for op in cond):
                    for op, val in cond.items():
                        if op == '$lte' and not (doc.get(k) <= val):
                            return False
                        if op == '$gte' and not (doc.get(k) >= val):
                            return False
                        # 其他操作符可按需扩展
                elif doc.get(k) != cond:
                    return False
            return True

        count = 0
        for _id, doc in self._docs.items():
            if _matches(doc):
                new_doc = dict(doc)
                if "$set" in update:
                    new_doc.update(update["$set"])
                self._docs[_id] = new_doc
                count += 1
        return _FakeResult(count)

    def count_documents(self, query: Dict[str, Any]) -> int:
        return sum(1 for doc in self._docs.values() if all(doc.get(k) == v for k, v in query.items()))

    def find(self, query: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [dict(doc) for doc in self._docs.values() if all(doc.get(k) == v for k, v in query.items())]


class _FakeResult:
    def __init__(self, modified_count: int):
        self.modified_count = modified_count


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class TestMongoIdempotentEnqueue(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.backend = MongoQueueBackend(FakeMongoCollection())
        self.q = PersistentQueue(self.backend, now_fn=self.clock)

    def test_first_enqueue_inserts(self):
        ok = self.q.enqueue("h1", {"content": "x"})
        self.assertTrue(ok)
        self.assertEqual(self.q.count_pending(), 1)

    def test_duplicate_enqueue_no_op(self):
        self.q.enqueue("h1", {"content": "x"})
        ok = self.q.enqueue("h1", {"content": "x"})
        self.assertFalse(ok)  # _id 冲突 -> 幂等命中
        self.assertEqual(self.q.count_pending(), 1)


class TestMongoLeaseAndConcurrentClaim(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.backend = MongoQueueBackend(FakeMongoCollection())
        self.q = PersistentQueue(self.backend, lease_seconds=60, now_fn=self.clock)
        self.q.enqueue("h1", {"content": "task1"})

    def test_claim_marks_processing(self):
        item = self.q.claim(worker_id="w1")
        self.assertIsNotNone(item)
        self.assertEqual(item["status"], STATUS_PROCESSING)
        self.assertEqual(item["worker_id"], "w1")

    def test_concurrent_claim_only_one_wins(self):
        item1 = self.q.claim(worker_id="w1")
        item2 = self.q.claim(worker_id="w2")
        self.assertIsNotNone(item1)
        self.assertIsNone(item2)

    def test_lease_expiry_releases_back_to_pending(self):
        self.q.claim(worker_id="w1")
        self.clock.advance(seconds=61)
        reaped = self.q.reap_expired_leases()
        self.assertEqual(reaped, 1)
        self.assertEqual(self.q.count_pending(), 1)
        self.assertEqual(self.q.count_processing(), 0)

    def test_complete_only_by_owner(self):
        self.q.claim(worker_id="w1")
        self.assertFalse(self.q.complete("h1", "w2"))
        self.assertTrue(self.q.complete("h1", "w1"))
        self.assertEqual(self.q.count_completed(), 1)


class TestMongoRestartRecovery(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.backend = MongoQueueBackend(FakeMongoCollection())
        self.q = PersistentQueue(self.backend, lease_seconds=60, now_fn=self.clock)

    def test_orphaned_processing_recovers(self):
        self.q.enqueue("h1", {"content": "task1"})
        self.q.claim(worker_id="dead-worker")
        self.clock.advance(seconds=120)
        recovered = self.q.recover_on_restart()
        self.assertEqual(recovered, 1)
        item = self.q.claim(worker_id="new-worker")
        self.assertIsNotNone(item)
        self.assertEqual(item["worker_id"], "new-worker")


class TestMongoRetryAndDead(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 8, 8, 0, 1, 0))
        self.backend = MongoQueueBackend(FakeMongoCollection())
        self.q = PersistentQueue(self.backend, max_retries=1, now_fn=self.clock)
        self.q.enqueue("h1", {"content": "x"})

    def test_fail_returns_to_pending_within_retry_limit(self):
        self.q.claim(worker_id="w1")
        ok = self.q.fail("h1", "w1", reason="timeout")
        self.assertTrue(ok)
        self.assertEqual(self.q.count_pending(), 1)

    def test_fail_exceeding_retries_goes_dead(self):
        for i in range(2):  # max_retries=1，第 2 次超限
            self.q.claim(worker_id=f"w{i}")
            self.q.fail("h1", f"w{i}", reason="err")
            self.clock.advance(seconds=61)
            self.q.reap_expired_leases()
        self.assertEqual(self.q.count_dead(), 1)
        self.assertEqual(self.q.count_pending(), 0)


if __name__ == '__main__':
    unittest.main()