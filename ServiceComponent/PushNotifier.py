# -*- coding: utf-8 -*-
"""
PushNotifier 推送服务（M6 / P1-7 重写为真实可用实现）

P1-7 修复要点：
- 真实发送：默认实现可配置的通用 HTTP webhook（POST JSON，含超时、
  有限次指数退避重试）。仓库未指定专用推送渠道，通用 webhook 是
  最小且可审计的选择，后续可在 Sender 层扩展具体渠道；
- 可靠产生推送任务：Hub 归档成功后 submit() 幂等写入 MongoDB 持久化
  队列（push_tasks 集合，复用 PersistentQueue 租约/幂等/重试语义）；
- 常驻消费：run_forever() 消费循环 + 优雅退出（SIGTERM/SIGINT）；
- 只有发送成功才确认已推送：队列项在 sender 返回成功后才 complete；
  确认状态持久化在队列文档中（COMPLETED=已送达）；
- 重复发送边界（文档化）：外部 webhook 无幂等能力时，若发送成功但
  响应在网络传输中丢失（超时），重试会导致接收方收到同一 item 的
  重复推送。缓解：payload 携带 item_id，接收方可按 item_id 去重；
- 未配置渠道：is_enabled()=False 并在日志中明确显示禁用，不伪报成功。

容器入口：python -m ServiceComponent.PushNotifier
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from ServiceComponent.PersistentQueue import (
    MemoryQueueBackend,
    PersistentQueue,
)

logger = logging.getLogger(__name__)

# 推送队列 Mongo 集合名（Hub 与 Notifier 容器共享）
PUSH_TASKS_COLLECTION = "push_tasks"


class WebhookSender:
    """通用 HTTP webhook 发送器（POST JSON）。

    - 强制超时（默认 10s）；
    - 仅允许 http/https（经 SSRFGuard 校验，默认拒绝内网地址，
      可经 allow_private 显式放行自建内网网关）；
    - 非 2xx 响应抛异常（由调用方按有限重试处理）。
    """

    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0,
                 secret_token: str = "", allow_private: bool = False) -> None:
        from Tools.SSRFGuard import validate_url

        self.webhook_url = webhook_url
        self.timeout = timeout_seconds
        self.secret_token = secret_token
        # SSRF 校验失败直接抛错（fail-closed，启动即暴露配置问题）
        self._validated_url = validate_url(webhook_url, allow_private=allow_private)

    def send(self, payload: Dict[str, Any]) -> None:
        """发送一次 webhook。失败抛异常；成功（2xx）静默返回。"""
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.secret_token:
            # 常见 webhook 鉴权约定：Bearer / X-Webhook-Token 双头携带
            headers["Authorization"] = f"Bearer {self.secret_token}"
            headers["X-Webhook-Token"] = self.secret_token
        resp = httpx.post(self._validated_url, json=payload,
                          headers=headers, timeout=self.timeout)
        if not (200 <= resp.status_code < 300):
            # 不打印响应体（可能含敏感信息），仅状态码
            raise RuntimeError(f"webhook 响应非 2xx：HTTP {resp.status_code}")


class PushNotifier:
    """推送服务：消费持久化 push_tasks 队列并向 webhook 发送。

    config 键（_config/config.json → intelligence_hub.push_notifier）：
      enabled: bool            总开关（默认 false）
      webhook_url: str         渠道地址；为空 = 未配置（禁用，不伪报成功）
      secret_token: str        可选鉴权 token（Bearer / X-Webhook-Token）
      min_total_score: float   归档总分达到该值才推送（默认 7.0）
      timeout_seconds: float   发送超时（默认 10）
      max_retries: int         单次认领内发送重试次数（默认 3）
      retry_backoff_seconds: float  重试基础退避（默认 2，指数递增）
      allow_private_webhook: bool   允许内网 webhook 地址（默认 false）
      poll_interval_seconds: float  消费轮询间隔（默认 10）
    """

    def __init__(
        self,
        queue: Optional[PersistentQueue] = None,
        sender: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        cfg = dict(config or {})
        self.config = cfg
        self.enabled = bool(cfg.get("enabled", False))
        self.webhook_url = str(cfg.get("webhook_url", "") or "").strip()
        self.min_total_score = float(cfg.get("min_total_score", 7.0))
        self.timeout_seconds = float(cfg.get("timeout_seconds", 10.0))
        self.max_retries = max(1, int(cfg.get("max_retries", 3)))
        self.retry_backoff = float(cfg.get("retry_backoff_seconds", 2.0))
        self.poll_interval = float(cfg.get("poll_interval_seconds", 10.0))

        # 持久化推送队列（生产 MongoQueueBackend；测试/单机 Memory）
        self.queue: PersistentQueue = queue or PersistentQueue(MemoryQueueBackend())
        self.sender = sender
        self._sleep = sleep_fn or time.sleep
        self._stop_event = threading.Event()
        self._consume_thread: Optional[threading.Thread] = None
        self.sent_count: int = 0
        self.failed_count: int = 0
        self.worker_id = f"notifier-{uuid.uuid4().hex[:8]}"

    # ---------- 状态 ----------
    def is_enabled(self) -> bool:
        """服务是否可用：总开关开启且 webhook 已配置。"""
        return bool(self.enabled and self.webhook_url)

    def _get_sender(self) -> Optional[Any]:
        """惰性构造发送器（webhook 未配置时返回 None，不伪报成功）。"""
        if self.sender is not None:
            return self.sender
        if not self.is_enabled():
            return None
        allow_private = bool(self.config.get("allow_private_webhook", False))
        self.sender = WebhookSender(
            self.webhook_url,
            timeout_seconds=self.timeout_seconds,
            secret_token=str(self.config.get("secret_token", "") or ""),
            allow_private=allow_private)
        return self.sender

    # ---------- 生产端（Hub 归档后调用） ----------
    def submit(self, item: Dict[str, Any]) -> bool:
        """幂等提交一个推送任务（归档成功后调用）。

        item 需含 item_id（幂等键）；建议附带 title/url/total_score/archived_at。
        服务禁用时返回 False 并说明原因（不伪报成功）。
        """
        if not self.is_enabled():
            logger.info("PushNotifier 未启用（enabled=%s, webhook_url=%s），"
                        "跳过推送 item=%s。", self.enabled,
                        "已配置" if self.webhook_url else "未配置",
                        item.get("item_id", "?"))
            return False
        item_id = str(item.get("item_id", "") or "")
        if not item_id:
            logger.warning("PushNotifier.submit 缺少 item_id，拒绝入队。")
            return False
        payload = dict(item)
        payload["item_id"] = item_id
        payload["submitted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return self.queue.enqueue(content_hash=f"push-{item_id}", payload=payload)

    def should_push(self, total_score) -> bool:
        """按分数阈值判断是否需要推送（供 Hub 归档后过滤）。"""
        try:
            return float(total_score) >= self.min_total_score
        except (TypeError, ValueError):
            return False

    # ---------- 消费端 ----------
    def start(self) -> bool:
        """启动常驻消费线程。服务禁用时拒绝启动并明确告警。"""
        if not self.is_enabled():
            logger.warning("PushNotifier 已禁用（未配置渠道），消费线程不启动。"
                           "如需推送请在 config.json 的 intelligence_hub.push_notifier"
                           " 中配置 enabled=true 与 webhook_url。")
            return False
        if self._get_sender() is None:
            logger.error("PushNotifier sender 初始化失败，消费线程不启动。")
            return False
        if self._consume_thread and self._consume_thread.is_alive():
            return True
        try:
            self.queue.recover_on_restart()
        except Exception as e:
            logger.warning("PushNotifier 启动恢复失败（稍后重试）：%s", e)
        self._stop_event.clear()
        self._consume_thread = threading.Thread(
            target=self.run_forever, name="PushNotifier", daemon=True)
        self._consume_thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._consume_thread:
            self._consume_thread.join(timeout=15)

    def is_running(self) -> bool:
        return bool(self._consume_thread and self._consume_thread.is_alive())


    def run_forever(self) -> None:
        """常驻消费循环：claim → 发送（有限退避重试）→ complete/fail。"""
        logger.info("PushNotifier 消费循环启动（worker=%s）。", self.worker_id)
        disabled_logged = False
        while not self._stop_event.is_set():
            if not self.is_enabled():
                if not disabled_logged:
                    logger.warning("PushNotifier 处于禁用状态（配置被移除？），"
                                   "等待重新配置，不消费、不伪报成功。")
                    disabled_logged = True
                self._stop_event.wait(self.poll_interval)
                continue
            disabled_logged = False
            try:
                self._maintain()
                processed_any = self.deliver_once()
                if not processed_any:
                    self._stop_event.wait(self.poll_interval)
            except Exception as e:
                logger.warning("PushNotifier 消费轮异常：%s", e)
                self._stop_event.wait(self.poll_interval)
        logger.info("PushNotifier 消费循环退出。")

    def _maintain(self) -> None:
        """周期维护：回收过期租约（崩溃恢复）。"""
        try:
            reaped = self.queue.reap_expired_leases()
            if reaped:
                logger.info("PushNotifier 维护：回收过期租约 %d 项。", reaped)
        except Exception as e:
            logger.warning("PushNotifier 租约回收失败：%s", e)

    def deliver_once(self) -> bool:
        """认领并尝试发送一个任务。返回是否处理了任务（含失败）。"""
        try:
            item = self.queue.claim(worker_id=self.worker_id)
        except Exception as e:
            logger.warning("PushNotifier claim 失败（后端暂不可用？）：%s", e)
            return False
        if item is None:
            return False
        payload = item.get("payload", {}) or {}
        item_id = payload.get("item_id", item.get("item_id", "?"))
        last_err: Optional[Exception] = None
        sent = False
        for attempt in range(1, self.max_retries + 1):
            if self._stop_event.is_set():
                # 优雅退出：无损释放，等下次启动继续
                try:
                    self.queue.release(item.get("item_id", ""), self.worker_id)
                except Exception as e:
                    # 停止路径上的释放失败：队列项将靠租约超时自动回收，无需阻断退出
                    logger.debug(f"Release item on stop failed (lease will reclaim): {e}")
                return True
            try:
                sender = self._get_sender()
                if sender is None:
                    raise RuntimeError("webhook sender 不可用（未配置渠道）")
                sender.send(payload)
                sent = True
                break
            except Exception as e:
                last_err = e
                logger.warning("PushNotifier 发送失败 item=%s（第 %d/%d 次）：%s",
                               item_id, attempt, self.max_retries, e)
                if attempt < self.max_retries:
                    self._sleep(min(self.retry_backoff * (2 ** (attempt - 1)), 60.0))
        if sent:
            # 只有发送成功才确认已推送（P1-7）
            self.queue.complete(item.get("item_id", ""), self.worker_id,
                                result={"sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            self.sent_count += 1
            logger.info("PushNotifier 已推送 item=%s。", item_id)
        else:
            self.failed_count += 1
            # 队列级失败（retries 递增，超 max_retries 进 DEAD 终态，可受控重试）
            self.queue.fail(item.get("item_id", ""), self.worker_id,
                            reason=f"send failed: {last_err}")
            logger.error("PushNotifier 推送失败 item=%s（重试耗尽，交还队列或转 DEAD）：%s",
                         item_id, last_err)
        return True


    def stats(self) -> Dict[str, Any]:
        """状态汇总（监控/健康检查）。"""
        try:
            pending = self.queue.count_pending()
            dead = self.queue.count_dead()
        except Exception:
            pending = dead = -1
        return {
            "enabled": self.is_enabled(),
            "webhook_configured": bool(self.webhook_url),
            "running": self.is_running(),
            "sent": self.sent_count,
            "failed": self.failed_count,
            "pending": pending,
            "dead": dead,
        }


# ---------------------------- 容器独立入口 ----------------------------

def _load_runtime_config() -> Dict[str, Any]:
    """读取运行配置：_config/config.json（标准库，避免重型导入链）。"""
    import json as _json

    config_dir = os.getenv("CONFIG_DIR", "")
    config_path = os.path.join(config_dir, "config.json") if config_dir \
        else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "_config", "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except FileNotFoundError:
        logger.warning("配置文件不存在（%s），推送服务以禁用状态运行。", config_path)
    except Exception as e:
        logger.error("配置文件读取失败（%s）：%s", config_path, e)
    return {}


def _build_mongo_queue(cfg: Dict[str, Any]) -> "PersistentQueue":
    """按配置/环境变量构造 Mongo 持久化推送队列（连接校验 fail-fast）。"""
    from pymongo import MongoClient

    from ServiceComponent.PersistentQueue import MongoQueueBackend

    hub_cfg = (cfg.get("intelligence_hub") or {})
    mongo_cfg = hub_cfg.get("mongodb") or {}
    host = os.getenv("MONGO_HOST", "") or mongo_cfg.get("host", "localhost")
    port = int(os.getenv("MONGO_PORT", "") or mongo_cfg.get("port", 27017))
    user = os.getenv("MONGO_USER", "") or mongo_cfg.get("user", "")
    password = os.getenv("MONGO_PASSWORD", "") or mongo_cfg.get("password", "")
    uri = (f"mongodb://{user}:{password}@{host}:{port}/?authSource=admin"
           if user and password else f"mongodb://{host}:{port}/")
    client = MongoClient(uri, connectTimeoutMS=3000, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")  # 短超时连接校验
    collection = client["IntelligenceIntegrationSystem"][PUSH_TASKS_COLLECTION]
    return PersistentQueue(MongoQueueBackend(collection))


def main() -> int:
    """容器入口：常驻消费 + 优雅退出（SIGTERM/SIGINT）。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    cfg = _load_runtime_config()
    push_cfg = ((cfg.get("intelligence_hub") or {}).get("push_notifier")) or {}

    try:
        queue = _build_mongo_queue(cfg)
    except Exception as e:
        # MongoDB 不可用时不静默降级内存队列（会丢推送任务）：
        # 明确告警并退出，由容器编排重启重试
        logger.error("推送队列 MongoDB 后端不可用，拒绝以易丢数据的内存队列启动：%s", e)
        return 1

    notifier = PushNotifier(queue=queue, config=push_cfg)
    if not notifier.is_enabled():
        logger.warning("China Watch Notifier：推送渠道未配置（enabled=%s，webhook_url=%s）。"
                       "服务以禁用状态常驻运行，不伪报成功。",
                       push_cfg.get("enabled", False),
                       "已配置" if push_cfg.get("webhook_url") else "未配置")
    else:
        notifier.start()

    def _on_signal(signum, frame):
        logger.info("收到信号 %s，推送服务退出中……", signum)
        notifier.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while True:  # 主线程保活（消费线程为 daemon）
            time.sleep(3600)
    except KeyboardInterrupt:
        notifier.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
