# -*- coding: utf-8 -*-
"""
ApiUsageLedger API调用费用台账（M3 / P1-6 重写 / 批次二修复）

P1-6 修复要点：
- 持久化：支持注入存储后端（MemoryLedgerStore / MongoLedgerStore），
  重启后日/月费用与调用计数可从台账恢复（BudgetGuard.restore_from_ledger）。
- 幂等：以 attempt_id（请求尝试 ID）为主键，重复记录返回 False，
  不产生重复记账。task_id（队列任务 ID）与 attempt_id 分离——
  一次任务重试多次会产生多条 attempt 记录。
- 费用未知不静默：cost_cny 允许为 None（如超时后 usage 未知、
  未知模型价格缺失），统计时单列 cost_unknown_count，不按 0 混入。
- 记录状态区分：OK / PARSE_FAILED（响应成功但解析失败，仍计 usage）/
  TIMEOUT_COST_UNKNOWN / FAILED_NO_USAGE / UNKNOWN_PRICE。
- 统一计费时区（默认 Asia/Shanghai），跨日/跨月汇总按计费时区计算。

批次二修复要点：
- IN_FLIGHT：发送请求前持久保存占用额度（cost_cny=预估费用）与尝试状态；
  重启后该类记录按预估费用恢复预算（未知费用不得恢复为 0）；
  成功后用 update_attempt 终态化（实际费用/usage）。
- MISSING_USAGE：响应成功但 usage 缺失时不得补零记 OK。
- LedgerStoreError：只有重复主键可按"已存在"处理（返回 False）；
  连接错误等存储故障必须抛错并进入 degraded 状态——
  台账读取失败不等于空台账，写入失败不等于重复记录；
  degraded 期间暂停新增付费请求（调用方检查 is_degraded），
  查询服务可继续运行（后续成功写入自动解除降级）。
- 时间统一带时区：now_fn 提供 aware 时间时按其时区折算到计费时区；
  naive 时间按计费时区解释（历史数据兼容）。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 记录状态常量
STATUS_OK = "OK"                                      # 正常完成并解析
STATUS_PARSE_FAILED = "PARSE_FAILED"                  # 响应成功但业务解析失败（usage 已记录）
STATUS_TIMEOUT_COST_UNKNOWN = "TIMEOUT_COST_UNKNOWN"  # 请求超时，费用未知
STATUS_FAILED_NO_USAGE = "FAILED_NO_USAGE"            # 请求失败，无 usage 返回
STATUS_UNKNOWN_PRICE = "UNKNOWN_PRICE"                # 模型价格缺失，费用按 0 计但显式标记
STATUS_IN_FLIGHT = "IN_FLIGHT"                        # 批次二：请求已发出但结果未知（占用=预估费用）
STATUS_MISSING_USAGE = "MISSING_USAGE"                # 批次二：响应成功但缺失 usage，费用未知
STATUS_CANCELLED = "CANCELLED"                        # 批次二：护栏在发送前阻止，占用退回（费用 0）

DEFAULT_BILLING_TIMEZONE = "Asia/Shanghai"


class LedgerStoreError(RuntimeError):
    """台账存储后端故障（连接错误等）。

    与"重复主键"（幂等命中，返回 False）严格区分：
    存储故障时记录是否写入不确定，必须让上层感知并进入降级。
    """


class LedgerStore(Protocol):
    """台账存储后端协议（线程安全）。"""

    def insert_if_absent(self, record: Dict[str, Any]) -> bool:
        """按 _id=attempt_id 幂等插入。已存在返回 False。"""
        ...

    def list_all(self) -> List[Dict[str, Any]]:
        """列出全部记录（恢复/汇总用；量级受调用上限约束）。"""
        ...


class MemoryLedgerStore:
    """内存台账后端（测试 / DRY_RUN 模式）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, Dict[str, Any]] = {}

    def insert_if_absent(self, record: Dict[str, Any]) -> bool:
        with self._lock:
            key = record["_id"]
            if key in self._records:
                return False
            self._records[key] = dict(record)
            return True

    def update_attempt(self, attempt_id: str, fields: Dict[str, Any]) -> bool:
        """批次二：终态化 IN_FLIGHT 记录。"""
        with self._lock:
            if attempt_id not in self._records:
                return False
            self._records[attempt_id].update(dict(fields))
            return True

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._records.values()]


class MongoLedgerStore:
    """MongoDB 台账后端（生产）。

    集合 schema：{_id: attempt_id, task_id, model, input_tokens,
    output_tokens, cost_cny(可空), is_dry_run, status, timestamp(计费时区 ISO)}。
    幂等由 _id 唯一性保证：重复 insert 捕获重复键异常返回 False；
    其余异常（连接错误等）抛 LedgerStoreError——写入失败不等于重复记录。
    """

    def __init__(self, collection: Any) -> None:
        self._col = collection

    @staticmethod
    def _is_duplicate_key(exc: Exception) -> bool:
        """判定异常是否为重复主键（幂等命中）。

        pymongo DuplicateKeyError 是明确语义；额外用错误码 11000 兜底，
        避免 fake collection / 不同 pymongo 版本的导入差异。
        """
        code = getattr(exc, 'code', None)
        if code == 11000:
            return True
        name = type(exc).__name__
        return 'DuplicateKey' in name or 'duplicate key' in str(exc).lower()

    def insert_if_absent(self, record: Dict[str, Any]) -> bool:
        try:
            self._col.insert_one(dict(record))
            return True
        except Exception as e:
            if self._is_duplicate_key(e):
                # 批次二修复：只有重复主键可按"已存在"处理
                logger.debug("台账幂等命中（attempt_id=%s 已存在）。", record.get("_id", "?"))
                return False
            # 连接错误等：不得静默当作已记录（写入失败 ≠ 重复记录）
            raise LedgerStoreError(
                f"台账写入失败（attempt_id={record.get('_id', '?')}）：{e}") from e

    def update_attempt(self, attempt_id: str, fields: Dict[str, Any]) -> bool:
        """批次二：终态化 IN_FLIGHT 记录（成功/失败后更新 usage 与费用）。

        返回 True=更新成功；False=记录不存在（幂等安全）；
        存储故障抛 LedgerStoreError。
        """
        try:
            res = self._col.update_one({"_id": attempt_id}, {"$set": dict(fields)})
            return getattr(res, 'modified_count', 1) >= 0  # upsert 语义：存在即可
        except Exception as e:
            if self._is_duplicate_key(e):
                return False
            raise LedgerStoreError(f"台账更新失败（attempt_id={attempt_id}）：{e}") from e

    def list_all(self) -> List[Dict[str, Any]]:
        try:
            return list(self._col.find({}))
        except Exception as e:
            # 批次二修复：读取失败不等于空台账——必须抛错让上层进入降级，
            # 否则预算恢复会把"读不到"当作"0 消费"造成超支。
            raise LedgerStoreError(f"台账读取失败（恢复汇总不可用）：{e}") from e


class ApiUsageLedger:
    """API 调用费用台账。

    兼容旧接口：record(...) / total_cost() / count()。
    新增：daily_summary / monthly_summary（按计费时区）、持久化后端注入。
    """

    def __init__(self,
                 store: Optional[LedgerStore] = None,
                 timezone: str = DEFAULT_BILLING_TIMEZONE,
                 now_fn=None) -> None:
        self._lock = threading.Lock()
        self._store = store or MemoryLedgerStore()
        self._tz = ZoneInfo(timezone)
        self._now_fn = now_fn or datetime.now
        # 批次二：降级状态——后端读写故障时置位，恢复成功后自动清除。
        # degraded 期间调用方（router/drainer）应暂停新增付费请求。
        self._degraded: bool = False

    # ---------- 降级状态 ----------
    def is_degraded(self) -> bool:
        """台账后端是否处于故障降级状态（读/写失败未恢复）。"""
        return self._degraded

    # ---------- 记录（幂等） ----------
    def record(self,
               attempt_id: str,
               model: str,
               input_tokens: int,
               output_tokens: int,
               cost_cny: Optional[float],
               is_dry_run: bool,
               task_id: Optional[str] = None,
               status: str = STATUS_OK) -> bool:
        """记录一次请求尝试。

        - attempt_id：请求尝试 ID（唯一键，幂等）；同一任务重试的每次
          尝试应使用不同 attempt_id（推荐 uuid4）。
        - task_id：关联的队列任务 ID（如 original_uuid），与 attempt_id 分离。
        - cost_cny：费用；None 表示费用未知（超时/价格缺失），不按 0 静默。
          IN_FLIGHT 记录传入预估费用作为占用（重启恢复用）。
        - status：见模块头部常量。

        返回 True=新记录；False=attempt_id 已存在（幂等命中）。
        批次二：写入失败（LedgerStoreError）不再与幂等命中混淆——
        置位 degraded 并返回 False，调用方必须检查 is_degraded()。
        """
        attempt_id = attempt_id or f"auto-{uuid.uuid4().hex}"
        now = self._now_fn()
        # 统一使用带时区时间：naive 视为计费时区，aware 折算到计费时区
        timestamp = now if now.tzinfo is not None else now.replace(tzinfo=self._tz)
        rec = {
            "_id": attempt_id,
            "task_id": task_id,
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cost_cny": None if cost_cny is None else round(float(cost_cny), 6),
            "is_dry_run": bool(is_dry_run),
            "status": status,
            # 统一以计费时区存储，便于跨日/跨月聚合与人工核对
            "timestamp": timestamp.astimezone(self._tz).isoformat(),
        }
        try:
            inserted = self._store.insert_if_absent(rec)
        except LedgerStoreError as e:
            self._degraded = True
            logger.error("台账写入故障，进入降级（暂停新增付费请求）：%s", e)
            return False
        if inserted:
            self._degraded = False  # 成功写入即解除降级（自动恢复）
        else:
            logger.debug("台账幂等命中（attempt_id=%s 已存在），不重复记账。", attempt_id)
        return inserted

    # ---------- 终态化（批次二） ----------
    def update_attempt(self, attempt_id: str, fields: Dict[str, Any]) -> bool:
        """更新既有尝试记录（IN_FLIGHT → 终态）。

        典型用法：请求成功后写入实际 usage/费用/状态；失败后写入失败状态。
        保留原 timestamp（计费归属时间不变）。
        """
        try:
            return self._store.update_attempt(attempt_id, dict(fields))
        except LedgerStoreError as e:
            self._degraded = True
            logger.error("台账更新故障，进入降级（暂停新增付费请求）：%s", e)
            return False

    def get_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        """读取单条尝试记录（诊断/测试用）。"""
        for r in self._store.list_all():
            if r.get("_id") == attempt_id:
                return dict(r)
        return None

    # ---------- 汇总 ----------
    def total_cost(self) -> float:
        """全部记录费用合计（费用未知记录不计入，单独看 cost_unknown_count）。"""
        return round(sum(r["cost_cny"] for r in self._safe_list_all()
                         if r.get("cost_cny") is not None), 6)

    def count(self) -> int:
        return len(self._safe_list_all())

    def _safe_list_all(self) -> List[Dict[str, Any]]:
        """list_all 的降级包装：读取失败置位 degraded 并返回缓存空列表。

        注意：返回空列表不代表"空台账"——degraded 期间上层必须把汇总
        视为不可信（查询服务可继续展示，但预算决策不得据此放行）。
        """
        try:
            records = self._store.list_all()
            self._degraded = False
            return records
        except LedgerStoreError as e:
            self._degraded = True
            logger.error("台账读取故障，进入降级（汇总不可信）： %s", e)
            return []

    def _parse_keys(self, record: Dict[str, Any]) -> tuple:
        try:
            ts = datetime.fromisoformat(str(record.get("timestamp", "")))
            # 批次二：naive 时间戳按计费时区解释（历史记录兼容），
            # 不得按宿主机本地时区解释（宿主机为 UTC 时会错切日账）。
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=self._tz)
            local = ts.astimezone(self._tz)
            return local.date(), local.strftime("%Y-%m")
        except ValueError:
            return None, None

    def daily_summary(self, day: Optional[date] = None) -> Dict[str, Any]:
        """按计费时区统计某日费用与调用次数。day=None 表示今天（now_fn）。"""
        now = self._now_fn()
        now_local = now.astimezone(self._tz) if now.tzinfo is not None else now
        target = day or now_local.date()
        cost = 0.0
        calls = 0
        unknown = 0
        for r in self._safe_list_all():
            d, _ = self._parse_keys(r)
            if d == target:
                calls += 1
                if r.get("cost_cny") is None:
                    unknown += 1
                else:
                    cost += r["cost_cny"]
        return {"date": str(target), "cost": round(cost, 6), "calls": calls,
                "cost_unknown_count": unknown}

    def monthly_summary(self, month: Optional[str] = None) -> Dict[str, Any]:
        """按计费时区统计某月（YYYY-MM）费用与调用次数。month=None 表示当月。"""
        now = self._now_fn()
        now_local = now.astimezone(self._tz) if now.tzinfo is not None else now
        target = month or now_local.strftime("%Y-%m")
        cost = 0.0
        calls = 0
        unknown = 0
        for r in self._safe_list_all():
            _, m = self._parse_keys(r)
            if m == target:
                calls += 1
                if r.get("cost_cny") is None:
                    unknown += 1
                else:
                    cost += r["cost_cny"]
        return {"month": target, "cost": round(cost, 6), "calls": calls,
                "cost_unknown_count": unknown}

    def records(self, limit: int = 100) -> List[Dict[str, Any]]:
        """最近记录（诊断用）。"""
        all_records = self._safe_list_all()
        all_records.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
        return all_records[:limit]
