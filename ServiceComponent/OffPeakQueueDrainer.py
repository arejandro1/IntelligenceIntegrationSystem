# -*- coding: utf-8 -*-
"""
OffPeakQueueDrainer 闲时队列消费器（M3 修复版 / P1-5、P1-6 重写）

按 ADR-006 要求使用 PersistentQueue（持久化）：
- 默认 MemoryQueueBackend（单机/测试）
- 生产注入 MongoQueueBackend（持久化，不可用时启动失败，禁止静默降级内存）
- 仅非高峰时段消费，受 Guard 窗口约束
- 默认 Mock/DRY_RUN 模式，不产生真实 API 调用
- 与旧 AI Worker 互斥（由 ConsumerMutex 校验，启动时执行）

P1-5 修复（分析完成 ≠ 归档完成）：
- complete() 后调用 on_complete 回调交付归档链路，回调成功才
  queue.deliver() 标记 DELIVERED；回调失败/崩溃/队列满保持未交付；
- 启动与每轮维护时 replay_undelivered()：重放"分析成功、尚未归档"的
  结果，只重交付、不重复调用 AI；
- 归档幂等性由 Hub 侧查重保证（重复交付不产生重复归档）。

P1-6 修复（逐请求护栏）：
- 每个任务处理前检查：停止信号、时间窗口（router.can_call）、
  预算原子预留（budget.try_reserve）；
- 每分钟最大开始次数按真实时间滑动窗口计算，失败尝试也计入；
- 每次真实 HTTP 尝试（含客户端内部重试）经 pre_attempt_hook 护栏；
- 成功响应但业务解析失败仍记录 usage（异常附带 api_usage）；
- 请求失败/超时费用未知时台账记 FAILED_NO_USAGE /
  TIMEOUT_COST_UNKNOWN，不按 0 静默。
"""
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from ServiceComponent.ApiUsageLedger import (
    STATUS_CANCELLED,
    STATUS_FAILED_NO_USAGE,
    STATUS_IN_FLIGHT,
    STATUS_MISSING_USAGE,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_TIMEOUT_COST_UNKNOWN,
    STATUS_UNKNOWN_PRICE,
)
from ServiceComponent.DeepSeekModelRouter import ApiCallBlocked, DeepSeekModelRouter
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
from ServiceComponent.PersistentQueue import (
    DELIVERY_DELIVERED,
    DELIVERY_STATUS_FIELD,
    MemoryQueueBackend,
    PersistentQueue,
    QueueBackend,
)

logger = logging.getLogger(__name__)


class StartRateLimiter:
    """每分钟最大开始次数限流（真实时间滑动窗口）。

    P1-6：按"开始处理的次数"计数，失败尝试同样占用配额，
    防止失败风暴导致的实际请求量超限。now_fn 可注入（测试）。
    """

    def __init__(self, max_per_minute: int, now_fn: Optional[Callable[[], float]] = None):
        self.max_per_minute = max(1, int(max_per_minute))
        self._now_fn = now_fn or time.monotonic
        self._starts: Deque[float] = deque()

    def _prune(self) -> None:
        cutoff = self._now_fn() - 60.0
        while self._starts and self._starts[0] <= cutoff:
            self._starts.popleft()

    def allow(self) -> bool:
        self._prune()
        return len(self._starts) < self.max_per_minute

    def record_start(self) -> None:
        self._starts.append(self._now_fn())


class OffPeakQueueDrainer:
    """闲时 AI 队列消费器。

    构造时注入 PersistentQueue（持久化队列）。
    若不注入，默认创建内存后端（用于单机/测试，不持久化）。

    与旧 AI Worker 的互斥校验由 ConsumerMutex.validate_consumer_mutex 负责，
    调用方应在启动本类前执行。
    """

    def __init__(
        self,
        router: DeepSeekModelRouter,
        mock_client: MockDeepSeekClient,
        drain_interval_seconds: int = 30,
        max_per_minute: int = 2,
        persistent_queue: Optional[PersistentQueue] = None,
        real_client: Optional[Any] = None,
        invoke_handler: Optional[Callable[[Dict[str, Any]], Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]] = None,
        on_complete: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        """real_client：真实 DeepSeek 客户端（api_dry_run=false 时使用）。
        不传则真实模式下抛错（避免误调用空实现）。

        invoke_handler(task_payload) -> (resp, business_result)：
            业务注入点。业务侧（Hub）负责构造 prompt、调用客户端与解析结果；
            本类统一负责时间窗口、限流、租约、预算与费用台账。
            解析失败时应在异常对象上附带 api_usage 属性（见 _process_item），
            以便"响应成功但解析失败"仍能记录 usage。
        on_complete(item_id, result) -> bool：
            交付回调（分析结果送归档链路）。返回 True 表示已成功接收，
            此时才标记 DELIVERED；False/异常保持未交付等待重放（P1-5）。
        """
    def __init__(
        self,
        router: DeepSeekModelRouter,
        mock_client: MockDeepSeekClient,
        drain_interval_seconds: int = 30,
        max_per_minute: int = 2,
        persistent_queue: Optional[PersistentQueue] = None,
        real_client: Optional[Any] = None,
        invoke_handler: Optional[Callable[[Dict[str, Any]], Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]] = None,
        on_complete: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        now_fn: Optional[Callable[[], float]] = None,
        estimate_messages_fn: Optional[Callable[[Dict[str, Any]], Tuple[Any, int]]] = None,
        pause_predicate: Optional[Callable[[], bool]] = None,
    ) -> None:
        """real_client：真实 DeepSeek 客户端（api_dry_run=false 时使用）。
        不传则真实模式下抛错（避免误调用空实现）。

        invoke_handler(task_payload) -> (resp, business_result)：
            业务注入点。业务侧（Hub）负责构造 prompt、调用客户端与解析结果；
            本类统一负责时间窗口、限流、租约、预算与费用台账。
            解析失败时应在异常对象上附带 api_usage 属性（见 _process_item），
            以便"响应成功但解析失败"仍能记录 usage。
        on_complete(item_id, result) -> bool：
            交付回调（分析结果送归档链路）。返回 True 表示已成功接收，
            此时才标记 DELIVERED；False/异常保持未交付等待重放（P1-5）。
        estimate_messages_fn(task) -> (messages, max_tokens)（批次二）：
            预估钩子——返回与最终实际发送一致的 messages 与输出上限。
            预估输入费用必须覆盖 prompt 与正文；预估输出上限必须与实际
            请求使用的 max_tokens 相同。缺省退回 content 文本估算。
        pause_predicate() -> bool（批次四）：
            暂停谓词——返回 True 时停止认领新任务与新增付费请求
            （如磁盘使用率 >=95%）；在途请求自然完成，已付费结果的
            重放继续（保护已支付成果的空间回收由归档链路负责）。
        """
        self.router = router
        self.mock_client = mock_client
        self.real_client = real_client
        self.invoke_handler = invoke_handler
        self.on_complete = on_complete
        self.drain_interval = drain_interval_seconds
        self.max_per_minute = max_per_minute
        self.estimate_messages_fn = estimate_messages_fn
        self.pause_predicate = pause_predicate
        # 批次三：归档确认回调 archive_confirm_fn(business) -> bool——
        # 判定该业务结果是否已被归档链路最终确认（归档成功或已存在
        # 相同归档结果）。用于 DELIVERED 后崩溃的对账重放。
        self.archive_confirm_fn = None
        # P1-6：真实时间滑动窗口限流（含失败尝试）
        self.rate_limiter = StartRateLimiter(max_per_minute, now_fn=now_fn)
        # 持久化队列（生产注入 MongoQueueBackend；默认内存后端）
        if persistent_queue is not None:
            self.queue: PersistentQueue = persistent_queue
        else:
            self.queue = PersistentQueue(MemoryQueueBackend())
        self._stop_event = threading.Event()
        self._drain_thread: Optional[threading.Thread] = None
        self.processed_count: int = 0
        self.error_count: int = 0
        self.delivered_count: int = 0
        self.replayed_count: int = 0
        self.worker_id = f"drainer-{uuid.uuid4().hex[:8]}"
        # 批次二：当前处理任务的记账上下文（AI worker 并发=1，进程内安全）
        self._current_task: Optional[Dict[str, Any]] = None
        # 批次二：注入逐尝试护栏与逐尝试记账回调（含限流）
        if self.real_client is not None:
            self.attach_real_client_guards()

    def attach_real_client_guards(self) -> None:
        """向真实客户端注入逐尝试护栏与记账回调（幂等，可重复调用）。

        批次二要点：
        - pre_attempt_hook：停止信号 → 时段窗口 → 限流（每次尝试独立计数）
          → 预算（有活跃预留时跳过，防止已取得名额被误拒）；
        - attempt_notifier：每次实际网络尝试（含内部重试）独立记账——
          失败尝试各记一条（费用未知不按 0）；成功尝试由任务级
          IN_FLIGHT 记录终态化承载（避免费用双计）。
        """
        if self.real_client is None:
            return
        self.real_client.pre_attempt_hook = self.router.make_attempt_guard(
            stop_event=self._stop_event, rate_limiter=self.rate_limiter)
        self.real_client.attempt_notifier = self._on_client_attempt


    # ---------- 入队（幂等） ----------
    def enqueue(self, task: Dict[str, Any]) -> str:
        """幂等入队。以 content_hash 为去重键（批次三：返回三态结果）。

        task 应包含：
        - content_hash（幂等键，缺失则用 original_uuid 或自动生成）
        - content（送入模型的内容）
        - original_uuid（关联原始数据）
        - model / is_priority（可选）

        返回 ENQUEUE_NEW（新增成功）或 ENQUEUE_DUPLICATE（确定重复）；
        后端存储故障抛出异常（调用方按失败处理，不得当作重复或成功）。
        """
        content_hash = task.get("content_hash") or task.get("original_uuid") or f"auto-{uuid.uuid4().hex}"
        payload = dict(task)
        return self.queue.enqueue_ex(content_hash=content_hash, payload=payload)

    def queue_size(self) -> int:
        """当前待处理（PENDING）数量。"""
        return self.queue.count_pending()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._drain_thread and self._drain_thread.is_alive():
            return
        # 启动前先做重启恢复：回收遗留过期租约 + 重放未交付分析结果（P1-5）
        try:
            recovered = self.queue.recover_on_restart()
            if recovered:
                logger.info("Drainer 重启恢复：回收过期租约 %d 项。", recovered)
        except Exception as e:
            # 后端不可用不应阻断线程启动（循环内会持续重试并告警）
            logger.warning("Drainer 启动恢复失败（后端暂不可用？）：%s", e)
        try:
            self.replay_undelivered()
        except Exception as e:
            logger.warning("Drainer 启动重放未交付结果失败（循环内重试）：%s", e)
        self._stop_event.clear()
        self._drain_thread = threading.Thread(target=self._drain_loop, name="OffPeakDrainer", daemon=True)
        self._drain_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._drain_thread:
            self._drain_thread.join(timeout=10)

    def is_running(self) -> bool:
        """P0-1：Drainer 消费线程是否存活（供深度健康检查按模式探活）。"""
        return bool(self._drain_thread and self._drain_thread.is_alive())

    # ---------- 维护：租约回收 + 未交付重放（P1-5） ----------
    def _maintenance_pass(self) -> None:
        """周期性维护：回收过期租约；重放分析成功但未归档交付的结果。"""
        try:
            reaped = self.queue.reap_expired_leases()
            if reaped:
                logger.info("Drainer 维护：回收过期租约 %d 项。", reaped)
        except Exception as e:
            logger.warning("Drainer 租约回收失败（后端暂不可用？）：%s", e)
        self.replay_undelivered()

    def replay_undelivered(self) -> int:
        """重放"分析成功、尚未归档交付"的结果。

        只重交付（调用 on_complete），绝不重复调用 AI；重放失败（如归档
        数据库暂不可用）保持未交付状态，下一轮维护继续重试。
        兼容历史记录：delivery_status 字段缺失的 COMPLETED 项视为未交付，
        交付一次后写入 DELIVERED 即不再重复。

        批次三新增对账：已 DELIVERED 但未 ACKNOWLEDGED 的项（内存队列
        接收成功 ≠ 持久归档完成）——经 archive_confirm_fn 判定：
        - 已确认（归档成功 A / 确认重复 U / 低值 D）→ 标记最终确认；
        - 未确认（如 processed_queue 接收后崩溃）→ 重新投递已保存的
          分析结果（复用结果，不重新付费调用模型）。
        """
        try:
            items = self.queue.list_completed_undelivered()
        except Exception as e:
            logger.warning("Drainer 查询未交付项失败（后端暂不可用？）：%s", e)
            return 0
        delivered = 0
        for item in items:
            result = item.get("result") or {}
            if not isinstance(result, dict):
                continue
            if self._deliver_result(item, result):
                delivered += 1
        if items:
            logger.info("Drainer 重放未交付结果：%d 项待交付，本轮成功 %d 项。",
                        len(items), delivered)
            self.replayed_count += delivered
        # 批次三：DELIVERED 未确认项对账
        self._reconcile_delivered_unacknowledged()
        return delivered

    def _reconcile_delivered_unacknowledged(self) -> int:
        """批次三：对账已投递未确认项。

        内存队列（processed_queue）接收成功不代表持久归档完成——
        投递后、归档前崩溃的项必须重新投递（复用已保存的分析结果，
        绝不重新调用 AI）；归档链路确认完成的项标记最终确认。
        归档幂等（INFORMANT 查重）保证重投不产生重复归档。
        """
        try:
            items = self.queue.list_delivered_unacknowledged()
        except Exception as e:
            logger.warning("Drainer 查询未确认项失败（后端暂不可用？）：%s", e)
            return 0
        if self.archive_confirm_fn is None:
            # 未注入确认回调（独立测试/未完成接线）：无法判定归档状态，
            # 跳过对账避免无依据的重复投递。生产环境由 Startup 注入
            # hub._drainer_archive_confirmed。
            return 0
        reconciled = 0
        redelivered = 0
        for item in items:
            result = item.get("result") or {}
            business = result.get("business") if isinstance(result, dict) else None
            item_id = item.get("item_id", "?")
            # 先问归档链路是否已最终确认
            if self.archive_confirm_fn is not None:
                try:
                    if self.archive_confirm_fn(business):
                        if self.queue.acknowledge(item_id):
                            reconciled += 1
                            continue
                except Exception as e:
                    logger.warning("Drainer 归档确认回调异常 item=%s：%s（下轮再试）",
                                   item_id, e)
                    continue
            # 未确认 → 重投已保存的分析结果（不重新调用 AI）
            if self._deliver_result(item, result):
                redelivered += 1
        if items:
            logger.info("Drainer 对账已投递未确认项：%d 项（确认 %d，重投 %d）。",
                        len(items), reconciled, redelivered)
            self.replayed_count += redelivered
        return redelivered

    def _deliver_result(self, item: Dict[str, Any], result: Dict[str, Any]) -> bool:
        """交付单个分析结果到归档链路。成功才标记 DELIVERED（P1-5）。

        无回调（独立运行/测试）时直接视为已交付（结果已持久化在队列）。
        批次三：deliver() 状态更新失败/返回 False 时不得视为已交付——
        保持未交付状态，由下一轮维护重放再次投递（归档幂等由 Hub 侧
        INFORMANT 查重保证，重放不产生重复归档）。
        对账重投（已 DELIVERED 未 ACK）时 on_complete 接受即视为成功——
        状态已是 DELIVERED，mark_delivered 幂等返回 False 不代表失败。
        """
        item_id = item.get("item_id", "?")
        was_delivered = item.get(DELIVERY_STATUS_FIELD) == DELIVERY_DELIVERED
        if self.on_complete is None:
            marked = was_delivered or self._mark_delivered_safe(item_id)
            if marked:
                self.delivered_count += 1
            return marked
        try:
            accepted = self.on_complete(item_id, result)
        except Exception as e:
            logger.error("Drainer on_complete 回调异常 item=%s：%s（保持未交付，等待重放）",
                         item_id, e)
            return False
        if accepted is False:
            logger.warning("Drainer on_complete 拒收 item=%s（如归档队列满），"
                           "保持未交付，等待重放。", item_id)
            return False
        if was_delivered:
            # 对账重投：回调已接受（数据重新进入归档链路），状态保持 DELIVERED
            self.delivered_count += 1
            return True
        marked = self._mark_delivered_safe(item_id)
        if marked:
            self.delivered_count += 1
        else:
            logger.warning("Drainer deliver 状态更新失败 item=%s，"
                           "保持未交付，下一轮维护重放（归档幂等兜底）。", item_id)
        return marked

    def _mark_delivered_safe(self, item_id: str) -> bool:
        """标记 DELIVERED，失败（异常/False）返回 False 并保留未交付状态。"""
        try:
            return bool(self.queue.deliver(item_id))
        except Exception as e:
            logger.warning("Drainer deliver 异常 item=%s：%s（保留未交付）", item_id, e)
            return False

    # ---------- 消费循环 ----------
    def _drain_loop(self) -> None:
        while not self._stop_event.is_set():
            # 周期性维护：回收过期租约 + 重放未交付结果
            try:
                self._maintenance_pass()
            except Exception as e:
                logger.warning("Drainer 维护轮失败：%s", e)

            while not self._stop_event.is_set():
                # 批次四：磁盘等暂停谓词——停止认领新任务与新增付费请求；
                # 在途请求自然完成，已付费结果的维护重放不受影响（外层执行）。
                if self.pause_predicate is not None and self.pause_predicate():
                    logger.info("Drainer 暂停认领新任务（pause_predicate 触发，"
                                "如磁盘 >=95%% 紧急降级），等待恢复。")
                    break
                # P1-6：逐请求护栏——时间窗口 + 预算（等待非高峰/预算耗尽
                # 不是故障，退出内循环等待下一轮即可）
                if not self.router.can_call():
                    break
                # P1-6：每分钟开始次数限流（真实时间窗口）。
                # 批次二：真正的计数在逐尝试护栏（make_attempt_guard）内进行，
                # 覆盖客户端内部重试——此处只做快速预检，不重复计数。
                if not self.rate_limiter.allow():
                    break
                try:
                    item = self.queue.claim(worker_id=self.worker_id)
                except Exception as e:
                    logger.warning("Drainer claim 失败（后端暂不可用？）：%s", e)
                    break
                if item is None:
                    break  # 无可认领项（可能已被其他 worker 认领）
                try:
                    result = self._process_item(item)
                    # 批次三：complete 状态更新失败必须处理（原先忽略返回值，
                    # 结果未持久化却继续交付，租约回收后将重复付费分析）。
                    completed = self.queue.complete(item["item_id"], self.worker_id,
                                                    result=result)
                    if not completed:
                        logger.error(
                            "Drainer complete 状态更新失败 item=%s（结果仍在内存）。"
                            "先交付保护已付费成果，交付成功后补写一次；"
                            "仍失败则该租约过期回收，存在重复分析风险（已告警）。",
                            item.get("item_id", "?"))
                    self.processed_count += 1
                except ApiCallBlocked as e:
                    # 护栏在处理中途收紧（如高峰边界到达）：不是任务失败，
                    # 无损释放回队列等待下一窗口（不消耗 max_retries）
                    logger.info("Drainer 处理被护栏阻止 item=%s：%s（无损释放回队列）",
                                item.get("item_id", "?"), e)
                    try:
                        self.queue.release(item["item_id"], self.worker_id)
                    except Exception as rel_err:
                        logger.warning("Drainer 无损释放失败 item=%s：%s",
                                       item.get("item_id", "?"), rel_err)
                    break
                except Exception as e:
                    self.error_count += 1
                    # 失败交还队列（受 max_retries 控制，超限进 DEAD）
                    self.queue.fail(item["item_id"], self.worker_id, reason=str(e))
                    logger.warning("Drainer 处理失败 item=%s：%s",
                                   item.get("item_id", "?"), e)
                    continue
                # P1-5：分析完成 → 交付归档（成功才标记 DELIVERED）
                self._deliver_result(item, result)
                # 批次三：交付成功后补写 complete（若此前失败）
                if not completed:
                    try:
                        self.queue.complete(item["item_id"], self.worker_id,
                                            result=result)
                    except Exception as comp_err:
                        logger.warning("Drainer 补写 complete 失败 item=%s：%s",
                                       item.get("item_id", "?"), comp_err)

            self._stop_event.wait(self.drain_interval)

    def _process_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个队列项。DRY_RUN 用 mock；真实模式用注入的 real_client。

        P4：真实模式传 max_tokens，避免长文截断仍按完整内容计费。
        P1-6：
        1. 处理前预算原子预留（按内容预估费用，try_reserve）；
        2. 成功后按实际 usage 结算（settle）并幂等记账
           （task_id 与 attempt_id 分离，重试各记一条）；
        3. 成功响应但业务解析失败：异常附带 api_usage 属性时仍记录费用
           （status=PARSE_FAILED），绕过费用记录的漏洞被封堵；
        4. 请求失败/超时：台账记 FAILED_NO_USAGE / TIMEOUT_COST_UNKNOWN，
           费用未知不按 0 静默；预算按预估值保留（最坏情况）。
        """
        task = item.get("payload", {})
        content = task.get("content", "")
        is_priority = task.get("is_priority", False)
        model = task.get("model") or self.router.route(is_priority)
        task_id = str(task.get("original_uuid")
                      or task.get("content_hash")
                      or item.get("item_id", ""))

        # 批次二：预估输出上限与输入消息——优先使用业务注入的
        # estimate_messages_fn（与实际发送一致：prompt + 正文 + 输出上限）。
        # 输入费用必须覆盖提示词与正文；输出上限必须与实际请求一致。
        if self.estimate_messages_fn is not None:
            est_messages, est_max_tokens = self.estimate_messages_fn(task)
            max_tokens = int(est_max_tokens)
            estimate = self.router.estimate_cost_for_messages(
                model, est_messages, max_tokens)
        else:
            max_tokens = int(task.get("max_tokens", 2048))
            estimate = self.router.estimate_cost(model, content, max_tokens)

        # P1-6：预算原子预留（预估费用先占额度，防止并发超支）
        if not self.router.budget.try_reserve(estimate):
            raise ApiCallBlocked(
                f"预算预留失败（预估 {estimate:.6f} 元），取消本次调用。")

        # 批次二：请求前持久保存占用额度与尝试状态（IN_FLIGHT，cost=预估）。
        # 重启后 restore_from_ledger 按该预估恢复预算——未知费用不得归零。
        inflight_id = f"{task_id}#{item.get('claim_count', 0)}#{uuid.uuid4().hex[:8]}"
        inserted = self.router.ledger.record(
            attempt_id=inflight_id, model=model,
            input_tokens=0, output_tokens=0,
            cost_cny=estimate, is_dry_run=self.router.api_dry_run,
            task_id=task_id, status=STATUS_IN_FLIGHT)
        if not inserted and self.router.ledger.is_degraded():
            # 台账写入故障：fail-closed，取消调用并退回占用（未发送）
            self.router.budget.cancel_reserve(estimate)
            raise ApiCallBlocked(
                "费用台账写入故障（降级），暂停新增付费请求，本次调用已取消。")

        # 批次二：登记逐尝试记账上下文（AI worker 并发=1，进程内安全）
        self._current_task = {'task_id': task_id, 'model': model,
                              'estimate': estimate, 'inflight_id': inflight_id}

        business_result: Optional[Dict[str, Any]] = None
        try:
            if self.invoke_handler is not None:
                # 业务注入入口（Hub 负责 prompt 构造、客户端调用与解析）。
                resp, business_result = self.invoke_handler(task)
            elif self.router.api_dry_run:
                resp = self.mock_client.chat_completion(
                    messages=[{"role": "user", "content": content}]
                )
            else:
                # 真实模式：必须注入 real_client，否则 fail-fast（避免误用空实现）
                if self.real_client is None:
                    raise RuntimeError(
                        "真实 API 模式（api_dry_run=false）必须注入 real_client；"
                        "当前未注入，已拒绝调用以防误产生费用。"
                    )
                resp = self.real_client.chat_completion(
                    messages=[{"role": "user", "content": content}],
                    model=model,
                    max_tokens=max_tokens,
                )
        except ApiCallBlocked as e:
            # 批次二：区分"从未发出请求"（可退款）与"已发送但结果未知"
            # （必须保留占用，IN_FLIGHT 记录保留——重启按预估恢复）。
            if getattr(e, 'sent_before_block', False):
                self.router.budget.settle(estimate, None)
                logger.warning(
                    "护栏阻止 item=%s 时已有尝试发出（费用未知）：占用 %.6f 元保留。",
                    item.get("item_id", "?"), estimate)
            else:
                self.router.budget.cancel_reserve(estimate)
                self.router.ledger.update_attempt(inflight_id, {
                    'status': STATUS_CANCELLED, 'cost_cny': 0.0})
            self._current_task = None
            raise
        except Exception as e:
            self._record_failed_attempt(e, inflight_id, task_id, model, estimate)
            self._current_task = None
            raise

        # 批次二：usage 缺失不得补零记 OK（MISSING_USAGE，费用未知）
        usage = resp.get("usage") or {}
        if not isinstance(usage, dict) or not usage:
            self.router.budget.settle(estimate, None)  # 费用未知：保留预估
            self.router.ledger.update_attempt(inflight_id, {
                'status': STATUS_MISSING_USAGE, 'cost_cny': None})
            result: Dict[str, Any] = {
                'model': model, 'is_dry_run': self.router.api_dry_run,
                'input_tokens': 0, 'output_tokens': 0, 'cost_cny': None,
            }
            self._finalize_result_content(result, resp, business_result)
            self._current_task = None
            return result

        # 成功：按实际 usage 结算并记账
        input_tok = int(usage.get("prompt_tokens", 0) or 0)
        output_tok = int(usage.get("completion_tokens", 0) or 0)
        cost = self.router.compute_cost(model, input_tok, output_tok)
        self.router.budget.settle(estimate, cost)
        # 价格缺失：费用记 None（UNKNOWN_PRICE），不按 0 静默
        known_price = self.router.has_price(model)
        # 批次二：成功后把 IN_FLIGHT 终态化（实际 usage 与费用）
        self.router.ledger.update_attempt(inflight_id, {
            'status': STATUS_OK if known_price else STATUS_UNKNOWN_PRICE,
            'input_tokens': input_tok, 'output_tokens': output_tok,
            'cost_cny': cost if known_price else None})
        # 提取分析结果内容,回传给 complete 持久化
        result = {
            "model": model,
            "is_dry_run": self.router.api_dry_run,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cost_cny": cost if known_price else None,
        }
        self._finalize_result_content(result, resp, business_result)
        self._current_task = None
        return result

    @staticmethod
    def _finalize_result_content(result: Dict[str, Any], resp: Dict[str, Any],
                                 business_result: Optional[Dict[str, Any]]) -> None:
        """把响应内容与业务结果附加到返回结构（容错）。"""
        try:
            choices = resp.get("choices") or []
            if choices:
                result["content"] = choices[0].get("message", {}).get("content", "")
        except Exception as e:
            # content 提取属尽力而为的附加字段：失败时 result 不含 content，
            # 主流程按无 content 处理（调用方已有空值分支），不视为错误
            logger.debug(f"Extract choices content failed (non-fatal): {e}")
        if business_result is not None:
            result["business"] = business_result

    def _on_client_attempt(self, attempt_id: str, ok: bool,
                           resp: Optional[Dict[str, Any]],
                           error: Optional[Exception]) -> None:
        """批次二：每次实际网络尝试的独立记账回调（真实客户端注入）。

        - 失败尝试（含内部重试）：各记一条独立记录——超时类记
          TIMEOUT_COST_UNKNOWN，其余记 FAILED_NO_USAGE；费用未知（None）
          不按 0 静默、不混入费用合计（避免与任务级结算双计）；
        - 成功尝试：不在此记账——由任务级 IN_FLIGHT 记录终态化承载
          （费用只计一次），token 用量随终态化写入。
        """
        ctx = self._current_task
        if ctx is None or ok:
            return
        msg = str(error or '').lower()
        is_timeout = 'timeout' in msg or 'timed out' in msg
        self.router.ledger.record(
            attempt_id=f"{ctx['task_id']}@{attempt_id}",
            model=ctx['model'],
            input_tokens=0, output_tokens=0,
            cost_cny=None,
            is_dry_run=self.router.api_dry_run,
            task_id=ctx['task_id'],
            status=STATUS_TIMEOUT_COST_UNKNOWN if is_timeout else STATUS_FAILED_NO_USAGE)

    def _record_failed_attempt(self, e: Exception, inflight_id: str,
                               task_id: str, model: str, estimate: float) -> None:
        """P1-6 / 批次二：失败尝试的 IN_FLIGHT 记录终态化（费用未知不按 0 静默）。

        - 异常附带 api_usage（成功响应但解析失败）：按实际 usage 终态化
          PARSE_FAILED，绕过费用记录的漏洞被封堵；
        - 其余失败：无 usage，费用未知；超时类记 TIMEOUT_COST_UNKNOWN，
          其他记 FAILED_NO_USAGE；预算保留预估值（最坏情况）。
        """
        usage = getattr(e, "api_usage", None)
        if isinstance(usage, dict):
            input_tok = int(usage.get("input_tokens", 0) or 0)
            output_tok = int(usage.get("output_tokens", 0) or 0)
            cost = self.router.compute_cost(model, input_tok, output_tok)
            self.router.budget.settle(estimate, cost)
            self.router.ledger.update_attempt(inflight_id, {
                'status': STATUS_PARSE_FAILED,
                'input_tokens': input_tok, 'output_tokens': output_tok,
                'cost_cny': cost if self.router.has_price(model) else None})
            return
        msg = str(e).lower()
        is_timeout = "timeout" in msg or "timed out" in msg
        self.router.budget.settle(estimate, None)  # 保留预估（最坏情况）
        self.router.ledger.update_attempt(inflight_id, {
            'status': STATUS_TIMEOUT_COST_UNKNOWN if is_timeout else STATUS_FAILED_NO_USAGE,
            'input_tokens': 0, 'output_tokens': 0, 'cost_cny': None})
