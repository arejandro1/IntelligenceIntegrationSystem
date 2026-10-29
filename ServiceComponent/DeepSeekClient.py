# -*- coding: utf-8 -*-
"""
DeepSeekClient 真实 DeepSeek API 客户端（M3 真实接入）

OpenAI 兼容接口；仅在 api_enabled=true 且 api_dry_run=false 时使用。
- 使用有限重试 + 指数退避（受时间策略约束，仅非高峰）
- 必须有超时
- 不打印密钥
- 接口与 MockDeepSeekClient 一致：chat_completion(messages) -> dict
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class NonRetryableError(RuntimeError):
    """不可重试错误（HTTP 400/401/403）——避免依赖中文字符串匹配判断。"""


class ApiAttemptBlocked(RuntimeError):
    """护栏阻止本次尝试（时间窗口/预算/停止信号）——立即终止，不重试。

    批次二新增属性 sent_before_block：阻止发生时本次请求是否已有
    尝试实际发出网络请求。True 表示"已发送但结果未知"（费用未知，
    上层必须保留预算占用，不得退款）；False 表示"从未发出"（可退款）。
    """

    is_guard_block = True

    def __init__(self, *args):
        super().__init__(*args)
        self.sent_before_block = False


class DeepSeekClient:
    """真实 DeepSeek API 客户端（OpenAI 兼容）。

    安全要点：
    - api_key 仅从构造参数注入，不读取环境、不写日志
    - 每次请求强制 timeout
    - 有限重试（默认 3 次），指数退避，不无限重试
    - 高峰时段由上层 ApiTimePolicy/DeepSeekModelRouter 控制，本类不自行判断时段
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        default_model: str = "deepseek-chat",
        timeout: float = 60.0,
        max_retries: int = 3,
        pre_attempt_hook: Optional[Callable[[], None]] = None,
        attempt_notifier: Optional[Callable[[str, bool, Optional[Dict[str, Any]], Optional[Exception]], None]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeekClient 缺少 api_key")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout
        self.max_retries = max_retries
        # P1-6：每次 HTTP 尝试（含重试）前执行的护栏钩子。
        # 钩子抛 ApiAttemptBlocked 时立即终止重试循环（高峰期重试为 0 的规则保证）。
        self.pre_attempt_hook = pre_attempt_hook
        # 批次二：逐尝试记账回调 attempt_notifier(attempt_id, ok, resp, error)。
        # 每次实际网络尝试（含内部重试）独立回调一次：
        # - ok=True：resp 为规范化响应（可能缺 usage）；
        # - ok=False：error 为本次尝试的异常（超时/网络/5xx）。
        # 被护栏阻止（未发送）的尝试不回调——由上层按 sent_before_block 处理。
        self.attempt_notifier = attempt_notifier
        self.call_count: int = 0
        # 批次二：本次请求生命周期内是否已有尝试实际发出网络请求。
        # 用于区分"从未发出请求"（可退款）与"已发送但结果未知"（保留占用）。
        self.any_attempt_sent: bool = False

    def _run_pre_attempt_hook(self) -> None:
        if self.pre_attempt_hook is not None:
            self.pre_attempt_hook()

    def _notify_attempt(self, attempt_id: str, ok: bool,
                        resp: Optional[Dict[str, Any]] = None,
                        error: Optional[Exception] = None) -> None:
        if self.attempt_notifier is not None:
            try:
                self.attempt_notifier(attempt_id, ok, resp, error)
            except Exception as e:  # 记账不得阻断请求流程
                logger.warning("attempt_notifier 回调异常（忽略）：%s", e)

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发起一次 chat completion 请求。

        返回与 MockDeepSeekClient 一致的结构：
            {"id": str, "choices": [...], "usage": {...}, "model": str}
        不可重试错误（400/401/403）立即抛出；可重试错误（5xx/超时/网络）按退避重试。
        """
        # 惰性导入 requests，避免无网络环境安装负担；优先用 httpx（依赖已在 server requirements）
        try:
            import httpx  # type: ignore
            return self._call_httpx(httpx, messages, model, temperature, max_tokens)
        except ImportError:
            import urllib.request
            import json
            return self._call_urllib(urllib.request, json, messages, model, temperature, max_tokens)

    # ---------- httpx 实现 ----------
    def _call_httpx(self, httpx, messages, model, temperature, max_tokens) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        last_exc: Optional[Exception] = None
        request_id = uuid.uuid4().hex[:8]
        self.any_attempt_sent = False
        for attempt in range(1, self.max_retries + 1):
            attempt_id = f"{request_id}-{attempt}"
            try:
                self._run_pre_attempt_hook()  # P1-6：逐尝试护栏（高峰/预算/停止即终止）
            except ApiAttemptBlocked as e:
                # 批次二：标记阻止时是否已有尝试发出网络请求——
                # 未发送可退款；已发送费用未知必须保留占用。
                e.sent_before_block = self.any_attempt_sent
                raise
            try:
                self.any_attempt_sent = True  # 即将发出网络请求
                resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
                status = resp.status_code
                # 不可重试（400/401/403）——使用 NonRetryableError 类型区分，不依赖字符串匹配
                if status in (400, 401, 403):
                    # 不打印 body（可能含敏感信息摘要），仅状态码
                    nre = NonRetryableError(f"DeepSeek 不可重试错误 HTTP {status}")
                    self._notify_attempt(attempt_id, False, None, nre)
                    raise nre
                if status >= 500:
                    last_exc = RuntimeError(f"DeepSeek 服务端错误 HTTP {status}")
                    self._notify_attempt(attempt_id, False, None, last_exc)
                    self._backoff(attempt)
                    continue
                if status != 200:
                    last_exc = RuntimeError(f"DeepSeek 异常状态 HTTP {status}")
                    self._notify_attempt(attempt_id, False, None, last_exc)
                    self._backoff(attempt)
                    continue
                data = resp.json()
                self.call_count += 1
                normalized = self._normalize(data, model or self.default_model)
                self._notify_attempt(attempt_id, True, normalized, None)
                return normalized
            except NonRetryableError:
                # 不可重试错误直接抛出，不进入退避重试
                raise
            except ApiAttemptBlocked:
                # 护栏阻止（高峰/预算/停止）：立即终止，不消耗重试
                raise
            except RuntimeError as e:
                # 服务端/异常状态错误：退避后重试（逐尝试记账）
                last_exc = e
                self._notify_attempt(attempt_id, False, None, e)
                self._backoff(attempt)
            except Exception as e:  # 网络错误/超时
                last_exc = e
                self._notify_attempt(attempt_id, False, None, e)
                self._backoff(attempt)
        # 重试耗尽
        raise RuntimeError(f"DeepSeek 调用失败（{self.max_retries} 次重试后仍失败）: {last_exc}")

    # ---------- urllib 后备实现（无 httpx 时） ----------
    def _call_urllib(self, urllib_request, json, messages, model, temperature, max_tokens) -> Dict[str, Any]:
        import json as _json
        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        body = _json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_exc: Optional[Exception] = None
        request_id = uuid.uuid4().hex[:8]
        self.any_attempt_sent = False
        for attempt in range(1, self.max_retries + 1):
            attempt_id = f"{request_id}-{attempt}"
            try:
                self._run_pre_attempt_hook()  # P1-6：逐尝试护栏（高峰/预算/停止即终止）
            except ApiAttemptBlocked as e:
                # 批次二：标记阻止时是否已有尝试发出网络请求
                e.sent_before_block = self.any_attempt_sent
                raise
            try:
                self.any_attempt_sent = True
                with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    if status != 200:
                        last_exc = RuntimeError(f"DeepSeek 异常状态 HTTP {status}")
                        self._notify_attempt(attempt_id, False, None, last_exc)
                        self._backoff(attempt)
                        continue
                    data = _json.loads(resp.read().decode("utf-8"))
                    self.call_count += 1
                    normalized = self._normalize(data, model or self.default_model)
                    self._notify_attempt(attempt_id, True, normalized, None)
                    return normalized
            except urllib_request.HTTPError as e:
                if e.code in (400, 401, 403):
                    nre = NonRetryableError(f"DeepSeek 不可重试错误 HTTP {e.code}")
                    self._notify_attempt(attempt_id, False, None, nre)
                    raise nre
                last_exc = e
                self._notify_attempt(attempt_id, False, None, e)
                self._backoff(attempt)
            except ApiAttemptBlocked:
                # 护栏阻止（高峰/预算/停止）：立即终止，不消耗重试
                raise
            except Exception as e:
                last_exc = e
                self._notify_attempt(attempt_id, False, None, e)
                self._backoff(attempt)
        raise RuntimeError(f"DeepSeek 调用失败（{self.max_retries} 次重试后仍失败）: {last_exc}")

    # ---------- 工具 ----------
    def _backoff(self, attempt: int) -> None:
        # 指数退避：1s, 2s, 4s ...（受上层时间策略约束，本类不判断时段）
        delay = min(2 ** (attempt - 1), 8)
        time.sleep(delay)

    @staticmethod
    def _normalize(data: Dict[str, Any], model: str) -> Dict[str, Any]:
        """规范化返回结构。

        批次二：usage 缺失时返回空 usage dict（不自动补零）——
        上层据 usage 为空判定 MISSING_USAGE，缺失用量不得记为正常完成。
        """
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return {
            "id": data.get("id", ""),
            "model": data.get("model", model),
            "choices": data.get("choices", []),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            } if usage else {},
        }