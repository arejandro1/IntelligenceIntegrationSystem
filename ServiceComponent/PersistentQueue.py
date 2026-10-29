# -*- coding: utf-8 -*-
"""
PersistentQueue AI延期队列（M3 修复）

按 ADR-006 要求：
- 支持持久化后端（生产用 MongoDB），保证进程重启后队列不丢失
- 提供内存后端（测试与单机后备）
- 幂等入队：同一 content_hash 重复入队不产生重复
- 租约机制：处理中项带租约，超时自动释放
- 并发认领：同一项不得被两个 worker 同时认领（原子 claim）
- 重启恢复：未完成项重新可被认领

不修改 Git 子模块；后端通过依赖注入传入，便于测试。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Protocol


# ============================================================
# 后端协议（Protocol）：生产用 MongoDB 实现，测试用 Memory 实现
# ============================================================
class QueueBackend(Protocol):
    """队列后端协议。所有方法必须是线程安全/原子。"""

    def insert_if_absent(self, doc: Dict[str, Any]) -> bool:
        """按 content_hash 幂等插入。已存在返回 False。"""
        ...

    def find_and_claim(self, worker_id: str, lease_until: datetime) -> Optional[Dict[str, Any]]:
        """原子地取出一个 PENDING 项并标记为 PROCESSING（带租约）。返回该项或 None。"""
        ...

    def renew_lease(self, item_id: str, worker_id: str, lease_until: datetime) -> bool:
        """续租。仅当 worker_id 匹配且当前为 PROCESSING 时成功。"""
        ...

    def mark_completed(self, item_id: str, worker_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        """标记完成。仅当 worker_id 匹配时成功。

        P4 修复:result 为可选的分析结果,后端应将其保存到文档中,
        避免"真实模式花钱分析但结果被丢弃"。

        P1-5 修复:完成(COMPLETED)仅代表"分析完成",不等于"归档完成"。
        归档交付状态由 delivery_status 字段单独跟踪(deliver 之后才置
        DELIVERED),重启后可据此重放"分析成功但尚未归档"的结果。
        """
        ...

    def mark_delivered(self, item_id: str) -> bool:
        """P1-5: 标记 COMPLETED 项的归档交付完成(delivery_status -> DELIVERED)。"""
        ...

    def list_completed_undelivered(self) -> List[Dict[str, Any]]:
        """P1-5: 列出 COMPLETED 但未交付(delivery_status != DELIVERED)的项。

        兼容历史记录:delivery_status 字段缺失视为未交付(交付一次后即写入
        DELIVERED,不会重复交付)。仅返回含 result.business 的项(有可交付数据)。
        """
        ...

    def requeue_dead(self, item_id: str) -> bool:
        """P1-5: 受控重试——把 DEAD 项重置回 PENDING(retries 清零)。"""
        ...

    def release(self, item_id: str, worker_id: str) -> bool:
        """P1-6: 无损释放——PROCESSING 项回到 PENDING，不增加重试计数。

        用于护栏在认领后阻止处理的场景（如高峰边界到达）：
        这不是任务失败，不应消耗 max_retries。
        """
        ...

    def mark_failed(self, item_id: str, worker_id: str, reason: str, max_retries: int) -> bool:
        """标记失败。超过 max_retries 则置 DEAD，否则回 PENDING 等待重试。"""
        ...

    def reap_expired_leases(self, now: datetime) -> int:
        """回收租约已过期但仍为 PROCESSING 的项，重置为 PENDING。返回回收数量。"""
        ...

    def count_by_status(self, status: str) -> int:
        """按状态计数。"""
        ...

    def list_by_status(self, status: str) -> List[Dict[str, Any]]:
        """按状态列出（主要用于测试与诊断）。"""
        ...


# 队列项状态常量
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_DEAD = "DEAD"

# P1-5: 归档交付状态常量。
# COMPLETED = 分析完成(AI 已调用、费用已记账);DELIVERED = 分析结果已成功
# 交给归档链路(processed_queue → post_process → MongoDB archive)。
# 两者的间隙(崩溃/回调失败/队列满)由重启重放(replay_undelivered)弥补,
# 重放只重交付、不重复调用 AI。
DELIVERY_STATUS_FIELD = "delivery_status"
DELIVERY_PENDING = "PENDING"
DELIVERY_DELIVERED = "DELIVERED"

# 批次三：最终确认状态。
# DELIVERED 只代表"已投递到归档链路入口（内存队列）"——内存队列接收成功
# 不代表持久归档完成。ACKNOWLEDGED 代表归档链路确认完成（归档成功 A，
# 或确认已存在相同归档结果 U/D）。DELIVERED 后崩溃的项由对账重放
# （archive_confirm_fn 判定）补投，不漏归档、不重复调用模型。
DELIVERY_ACKNOWLEDGED = "ACKNOWLEDGED"

# 批次三：入队三态结果（区分新增成功、确定重复与存储故障——故障走异常）。
ENQUEUE_NEW = "new"              # 新增成功
ENQUEUE_DUPLICATE = "duplicate"  # 确定重复（幂等命中）


# ============================================================
# 内存后端（测试与单机后备；线程安全）
# ============================================================
class MemoryQueueBackend:
    """内存队列后端。语义与 MongoDB 实现一致，用于测试与单机后备。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def insert_if_absent(self, doc: Dict[str, Any]) -> bool:
        with self._lock:
            item_id = doc["item_id"]
            if item_id in self._items:
                return False
            self._items[item_id] = dict(doc)
            return True

    def find_and_claim(self, worker_id: str, lease_until: datetime) -> Optional[Dict[str, Any]]:
        with self._lock:
            # 找最早的 PENDING 项（FIFO）
            pending = [
                it for it in self._items.values()
                if it["status"] == STATUS_PENDING
            ]
            if not pending:
                return None
            pending.sort(key=lambda x: x["enqueued_at"])
            target = pending[0]
            target["status"] = STATUS_PROCESSING
            target["worker_id"] = worker_id
            target["lease_until"] = lease_until.isoformat()
            target["claim_count"] = target.get("claim_count", 0) + 1
            return dict(target)

    def renew_lease(self, item_id: str, worker_id: str, lease_until: datetime) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it.get("worker_id") != worker_id:
                return False
            if it["status"] != STATUS_PROCESSING:
                return False
            it["lease_until"] = lease_until.isoformat()
            return True

    def mark_completed(self, item_id: str, worker_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it.get("worker_id") != worker_id:
                return False
            it["status"] = STATUS_COMPLETED
            it["completed_at"] = datetime.now().isoformat()
            # P1-5: 交付状态复位为 PENDING,等待 on_complete 成功后置 DELIVERED
            it[DELIVERY_STATUS_FIELD] = DELIVERY_PENDING
            if result is not None:
                it["result"] = result
            return True

    def mark_delivered(self, item_id: str) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it["status"] != STATUS_COMPLETED:
                return False
            if it.get(DELIVERY_STATUS_FIELD) == DELIVERY_ACKNOWLEDGED:
                return False  # 已最终确认，不回退
            it[DELIVERY_STATUS_FIELD] = DELIVERY_DELIVERED
            it["delivered_at"] = datetime.now().isoformat()
            return True

    def mark_acknowledged(self, item_id: str) -> bool:
        """批次三：最终确认（归档链路确认完成）。幂等。"""
        with self._lock:
            it = self._items.get(item_id)
            if not it or it["status"] != STATUS_COMPLETED:
                return False
            it[DELIVERY_STATUS_FIELD] = DELIVERY_ACKNOWLEDGED
            it["acknowledged_at"] = datetime.now().isoformat()
            return True

    def list_completed_undelivered(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for it in self._items.values():
                if it.get("status") != STATUS_COMPLETED:
                    continue
                # 历史记录兼容:delivery_status 缺失视为未交付
                if it.get(DELIVERY_STATUS_FIELD) in (DELIVERY_DELIVERED,
                                                     DELIVERY_ACKNOWLEDGED):
                    continue
                result = it.get("result") or {}
                if not isinstance(result, dict) or not isinstance(result.get("business"), dict):
                    continue  # 无可交付业务数据(如旧版本记录),跳过
                out.append(dict(it))
            return out

    def list_delivered_unacknowledged(self) -> List[Dict[str, Any]]:
        """批次三：已投递(DELIVERED)但归档链路尚未最终确认的项。"""
        with self._lock:
            out = []
            for it in self._items.values():
                if it.get("status") != STATUS_COMPLETED:
                    continue
                if it.get(DELIVERY_STATUS_FIELD) != DELIVERY_DELIVERED:
                    continue
                result = it.get("result") or {}
                if not isinstance(result, dict) or not isinstance(result.get("business"), dict):
                    continue
                out.append(dict(it))
            return out

    def requeue_dead(self, item_id: str) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it["status"] != STATUS_DEAD:
                return False
            it["status"] = STATUS_PENDING
            it["retries"] = 0
            it["worker_id"] = None
            it["lease_until"] = None
            it["requeued_at"] = datetime.now().isoformat()
            return True

    def release(self, item_id: str, worker_id: str) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it.get("worker_id") != worker_id or it["status"] != STATUS_PROCESSING:
                return False
            it["status"] = STATUS_PENDING
            it["worker_id"] = None
            it["lease_until"] = None
            return True

    def mark_failed(self, item_id: str, worker_id: str, reason: str, max_retries: int) -> bool:
        with self._lock:
            it = self._items.get(item_id)
            if not it or it.get("worker_id") != worker_id:
                return False
            it["retries"] = it.get("retries", 0) + 1
            it["last_error"] = reason
            if it["retries"] > max_retries:
                it["status"] = STATUS_DEAD
                it["dead_at"] = datetime.now().isoformat()
            else:
                it["status"] = STATUS_FAILED
                # 失败项回到 PENDING 等待重试
                it["status"] = STATUS_PENDING
                it["worker_id"] = None
                it["lease_until"] = None
            return True

    def reap_expired_leases(self, now: datetime) -> int:
        count = 0
        with self._lock:
            for it in self._items.values():
                if it["status"] != STATUS_PROCESSING:
                    continue
                lease_str = it.get("lease_until")
                if not lease_str:
                    continue
                try:
                    lease_until = datetime.fromisoformat(lease_str)
                except ValueError:
                    # 坏租约时间戳记录：无法判定是否过期则保守跳过，等待重建/人工处理
                    continue
                if lease_until <= now:
                    it["status"] = STATUS_PENDING
                    it["worker_id"] = None
                    it["lease_until"] = None
                    count += 1
            return count

    def count_by_status(self, status: str) -> int:
        with self._lock:
            return sum(1 for it in self._items.values() if it["status"] == status)

    def list_by_status(self, status: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(it) for it in self._items.values() if it["status"] == status]


# ============================================================
# MongoDB 后端（生产；惰性导入 pymongo，无 MongoDB 时不可用）
# ============================================================
class MongoQueueBackend:
    """MongoDB 持久化队列后端。

    线程安全由 MongoDB 单文档原子操作（findOneAndUpdate）保证。
    集合 schema：
      {
        _id: item_id (= content_hash),
        content_hash: str,
        payload: dict,
        status: str,
        priority: int,
        worker_id: str | None,
        lease_until: ISO | None,
        retries: int,
        claim_count: int,
        enqueued_at: ISO,
        completed_at: ISO | None,
        dead_at: ISO | None,
        last_error: str | None,
      }
    在 (content_hash) 上建唯一索引以保证幂等。
    """

    def __init__(self, collection) -> None:
        # collection 为 pymongo.collection.Collection 实例
        self._col = collection
        # 确保唯一索引（content_hash 已作为 _id，天然唯一）
        self._col.create_index("status")
        self._col.create_index([("status", 1), ("enqueued_at", 1)])

    @classmethod
    def from_uri(cls, uri: str, db_name: str, collection_name: str) -> "MongoQueueBackend":
        from pymongo import MongoClient  # 惰性导入
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        col = client[db_name][collection_name]
        return cls(col)

    def insert_if_absent(self, doc: Dict[str, Any]) -> bool:
        try:
            self._col.insert_one(doc)
            return True
        except Exception as e:
            # 批次三修复：只有重复主键可按"已存在"（幂等命中）处理；
            # 连接错误等存储故障必须抛出——数据库错误不得被误报为
            # "重复任务"或"提交成功"。
            code = getattr(e, 'code', None)
            name = type(e).__name__
            if code == 11000 or 'DuplicateKey' in name or 'duplicate key' in str(e).lower():
                return False
            raise

    def find_and_claim(self, worker_id: str, lease_until: datetime) -> Optional[Dict[str, Any]]:
        # 惰性导入 ReturnDocument，并通过 getattr 容错：
        # 单元测试使用 fake collection 时允许 return_document 参数缺失（None → Mongo 默认 AFTER 行为由 fake 接管）。
        try:
            from pymongo import ReturnDocument
            return_doc = ReturnDocument.AFTER
        except Exception:
            return_doc = None
        doc = self._col.find_one_and_update(
            {"status": STATUS_PENDING},
            {
                "$set": {
                    "status": STATUS_PROCESSING,
                    "worker_id": worker_id,
                    "lease_until": lease_until.isoformat(),
                },
                "$inc": {"claim_count": 1},
            },
            sort=[("enqueued_at", 1)],
            return_document=return_doc,
        )
        return doc

    def renew_lease(self, item_id: str, worker_id: str, lease_until: datetime) -> bool:
        res = self._col.update_one(
            {"_id": item_id, "worker_id": worker_id, "status": STATUS_PROCESSING},
            {"$set": {"lease_until": lease_until.isoformat()}},
        )
        return res.modified_count == 1

    def mark_completed(self, item_id: str, worker_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        update_set: Dict[str, Any] = {
            "status": STATUS_COMPLETED,
            "completed_at": datetime.now().isoformat(),
            # P1-5: 分析完成 ≠ 归档完成,交付状态单独跟踪
            DELIVERY_STATUS_FIELD: DELIVERY_PENDING,
        }
        if result is not None:
            update_set["result"] = result
        res = self._col.update_one(
            {"_id": item_id, "worker_id": worker_id, "status": STATUS_PROCESSING},
            {"$set": update_set},
        )
        return res.modified_count == 1

    def mark_delivered(self, item_id: str) -> bool:
        res = self._col.update_one(
            {"_id": item_id, "status": STATUS_COMPLETED,
             DELIVERY_STATUS_FIELD: {"$nin": [DELIVERY_DELIVERED,
                                              DELIVERY_ACKNOWLEDGED]}},
            {"$set": {DELIVERY_STATUS_FIELD: DELIVERY_DELIVERED,
                      "delivered_at": datetime.now().isoformat()}},
        )
        return res.modified_count == 1

    def mark_acknowledged(self, item_id: str) -> bool:
        """批次三：最终确认（归档链路确认完成）。幂等。"""
        res = self._col.update_one(
            {"_id": item_id, "status": STATUS_COMPLETED,
             DELIVERY_STATUS_FIELD: DELIVERY_DELIVERED},
            {"$set": {DELIVERY_STATUS_FIELD: DELIVERY_ACKNOWLEDGED,
                      "acknowledged_at": datetime.now().isoformat()}},
        )
        return res.modified_count == 1

    def list_delivered_unacknowledged(self) -> List[Dict[str, Any]]:
        """批次三：已投递(DELIVERED)但尚未最终确认的项。"""
        docs = self._col.find({
            "status": STATUS_COMPLETED,
            DELIVERY_STATUS_FIELD: DELIVERY_DELIVERED,
        })
        out = []
        for doc in docs:
            result = doc.get("result") or {}
            if isinstance(result, dict) and isinstance(result.get("business"), dict):
                out.append(doc)
        return out

    def list_completed_undelivered(self) -> List[Dict[str, Any]]:
        # 历史记录兼容:$ne DELIVERED 同时命中"缺失字段"与"PENDING"两类文档;
        # 无 result.business 的旧记录在 Python 侧过滤(避免复杂 Mongo 条件)。
        docs = self._col.find({
            "status": STATUS_COMPLETED,
            DELIVERY_STATUS_FIELD: {"$ne": DELIVERY_DELIVERED},
        })
        out = []
        for doc in docs:
            result = doc.get("result") or {}
            if isinstance(result, dict) and isinstance(result.get("business"), dict):
                out.append(doc)
        return out

    def requeue_dead(self, item_id: str) -> bool:
        res = self._col.update_one(
            {"_id": item_id, "status": STATUS_DEAD},
            {"$set": {"status": STATUS_PENDING, "retries": 0,
                      "worker_id": None, "lease_until": None,
                      "requeued_at": datetime.now().isoformat()}},
        )
        return res.modified_count == 1

    def release(self, item_id: str, worker_id: str) -> bool:
        res = self._col.update_one(
            {"_id": item_id, "worker_id": worker_id, "status": STATUS_PROCESSING},
            {"$set": {"status": STATUS_PENDING, "worker_id": None, "lease_until": None}},
        )
        return res.modified_count == 1

    def mark_failed(self, item_id: str, worker_id: str, reason: str, max_retries: int) -> bool:
        # 惰性导入 ReturnDocument，无 pymongo 环境（如单测用 fake collection）时容错
        try:
            from pymongo import ReturnDocument
            return_doc = ReturnDocument.AFTER
        except Exception:
            return_doc = None
        doc = self._col.find_one_and_update(
            {"_id": item_id, "worker_id": worker_id, "status": STATUS_PROCESSING},
            {
                "$inc": {"retries": 1},
                "$set": {"last_error": reason},
            },
            return_document=return_doc,
        )
        if not doc:
            return False
        if doc["retries"] > max_retries:
            self._col.update_one(
                {"_id": item_id},
                {"$set": {"status": STATUS_DEAD, "dead_at": datetime.now().isoformat()}},
            )
        else:
            self._col.update_one(
                {"_id": item_id},
                {"$set": {"status": STATUS_PENDING, "worker_id": None, "lease_until": None}},
            )
        return True

    def reap_expired_leases(self, now: datetime) -> int:
        res = self._col.update_many(
            {
                "status": STATUS_PROCESSING,
                "lease_until": {"$lte": now.isoformat()},
            },
            {
                "$set": {"status": STATUS_PENDING, "worker_id": None, "lease_until": None},
            },
        )
        return res.modified_count

    def count_by_status(self, status: str) -> int:
        return self._col.count_documents({"status": status})

    def list_by_status(self, status: str) -> List[Dict[str, Any]]:
        return list(self._col.find({"status": status}))


# ============================================================
# PersistentQueue 队列门面
# ============================================================
class PersistentQueue:
    """AI 延期队列门面。封装幂等入队、租约认领、重启恢复。

    所有时间通过传入的 now_fn 获取，便于 FakeClock 测试。
    """

    def __init__(
        self,
        backend: QueueBackend,
        lease_seconds: int = 300,
        max_retries: int = 3,
        now_fn=None,
    ) -> None:
        self.backend = backend
        self.lease_seconds = lease_seconds
        self.max_retries = max_retries
        self._now_fn = now_fn or datetime.now

    # ---------- 入队（幂等） ----------
    def enqueue(
        self,
        content_hash: str,
        payload: Dict[str, Any],
        priority: int = 0,
    ) -> bool:
        """幂等入队。content_hash 相同的项不会重复入队。

        返回 True 表示新插入，False 表示已存在（幂等命中）。
        后端存储故障会抛出异常（调用方按失败处理，不得当作重复）。
        """
        return self.enqueue_ex(content_hash, payload, priority) == ENQUEUE_NEW

    def enqueue_ex(
        self,
        content_hash: str,
        payload: Dict[str, Any],
        priority: int = 0,
    ) -> str:
        """批次三：三态入队结果。

        返回值：
        - ENQUEUE_NEW：新增成功；
        - ENQUEUE_DUPLICATE：确定重复（content_hash 已存在，幂等命中）；
        - 后端存储故障：抛出异常（既不是重复也不是成功）。
        """
        now = self._now_fn()
        doc = {
            "_id": content_hash,          # MongoDB 主键，保证唯一
            "item_id": content_hash,      # 内存后端用
            "content_hash": content_hash,
            "payload": payload,
            "status": STATUS_PENDING,
            "priority": priority,
            "worker_id": None,
            "lease_until": None,
            "retries": 0,
            "claim_count": 0,
            "enqueued_at": now.isoformat(),
            "completed_at": None,
            "dead_at": None,
            "last_error": None,
            # P1-5: 归档交付状态(None/缺失=历史兼容按未交付处理)
            DELIVERY_STATUS_FIELD: DELIVERY_PENDING,
        }
        inserted = self.backend.insert_if_absent(doc)
        return ENQUEUE_NEW if inserted else ENQUEUE_DUPLICATE

    # ---------- 认领（带租约，原子） ----------
    def claim(self, worker_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """原子认领一个 PENDING 项，标记为 PROCESSING 并设租约。"""
        wid = worker_id or f"w-{uuid.uuid4().hex[:8]}"
        lease_until = self._now_fn() + timedelta(seconds=self.lease_seconds)
        return self.backend.find_and_claim(wid, lease_until)

    def renew_lease(self, item_id: str, worker_id: str) -> bool:
        lease_until = self._now_fn() + timedelta(seconds=self.lease_seconds)
        return self.backend.renew_lease(item_id, worker_id, lease_until)

    # ---------- 完成 / 失败 ----------
    def complete(self, item_id: str, worker_id: str, result: Optional[Dict[str, Any]] = None) -> bool:
        """标记完成。可选传入 result 以持久化分析结果(P4)。

        P1-5:complete 只代表"分析完成";归档交付状态置为 PENDING,
        待 deliver() 成功后才算归档链路接收完成。
        """
        return self.backend.mark_completed(item_id, worker_id, result)

    def deliver(self, item_id: str) -> bool:
        """P1-5: 标记分析结果已成功交给归档链路(幂等)。"""
        return self.backend.mark_delivered(item_id)

    def acknowledge(self, item_id: str) -> bool:
        """批次三: 最终确认(归档成功或确认已存在相同归档结果)。幂等。"""
        return self.backend.mark_acknowledged(item_id)

    def list_delivered_unacknowledged(self) -> List[Dict[str, Any]]:
        """批次三: 已投递但归档链路尚未最终确认的项。"""
        return self.backend.list_delivered_unacknowledged()

    def list_completed_undelivered(self) -> List[Dict[str, Any]]:
        """P1-5: 列出"分析成功、尚未归档交付"的项(含历史记录兼容)。"""
        return self.backend.list_completed_undelivered()

    def requeue_dead(self, item_id: str) -> bool:
        """P1-5: 受控重试——DEAD 项重置回 PENDING(retries 清零)。

        供运维经管理接口对不可恢复任务发起人工重试。
        """
        return self.backend.requeue_dead(item_id)

    def release(self, item_id: str, worker_id: str) -> bool:
        """P1-6: 无损释放——认领后因护栏阻止的处理项回到 PENDING。"""
        return self.backend.release(item_id, worker_id)

    def fail(self, item_id: str, worker_id: str, reason: str) -> bool:
        return self.backend.mark_failed(item_id, worker_id, reason, self.max_retries)

    # ---------- 租约回收（重启恢复 / 周期清理） ----------
    def reap_expired_leases(self) -> int:
        return self.backend.reap_expired_leases(self._now_fn())

    def recover_on_restart(self) -> int:
        """重启恢复：回收所有过期租约，使未完成项重新可被认领。"""
        return self.reap_expired_leases()

    # ---------- 统计 ----------
    def count_pending(self) -> int:
        return self.backend.count_by_status(STATUS_PENDING)

    def count_processing(self) -> int:
        return self.backend.count_by_status(STATUS_PROCESSING)

    def count_completed(self) -> int:
        return self.backend.count_by_status(STATUS_COMPLETED)

    def count_dead(self) -> int:
        return self.backend.count_by_status(STATUS_DEAD)