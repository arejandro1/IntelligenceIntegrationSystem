# -*- coding: utf-8 -*-
"""
OffPeakQueueDrainer 闲时队列消费器（M3）
仅非高峰时段消费AI延期队列，受Guard窗口约束。
默认Mock/DRY_RUN模式；与旧AI Worker互斥。
"""
import threading
import queue
import time
from typing import Dict, Optional
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient

class OffPeakQueueDrainer:
    def __init__(self, router: DeepSeekModelRouter, mock_client: MockDeepSeekClient,
                 drain_interval_seconds: int = 30, max_per_minute: int = 2):
        self.router = router
        self.mock_client = mock_client
        self.drain_interval = drain_interval_seconds
        self.max_per_minute = max_per_minute
        self._task_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._drain_thread: Optional[threading.Thread] = None
        self.processed_count: int = 0
        self.error_count: int = 0

    def enqueue(self, task: Dict) -> None:
        self._task_queue.put(task)

    def queue_size(self) -> int:
        return self._task_queue.qsize()

    def start(self) -> None:
        if self._drain_thread and self._drain_thread.is_alive():
            return
        self._stop_event.clear()
        self._drain_thread = threading.Thread(target=self._drain_loop, name="OffPeakDrainer", daemon=True)
        self._drain_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._drain_thread:
            self._drain_thread.join(timeout=10)

    def _drain_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.router.can_call():
                self._stop_event.wait(self.drain_interval)
                continue
            count_this_minute = 0
            while count_this_minute < self.max_per_minute and not self._task_queue.empty():
                try:
                    task = self._task_queue.get_nowait()
                    self._process_task(task)
                    self.processed_count += 1
                    count_this_minute += 1
                except queue.Empty:
                    break
                except Exception:
                    self.error_count += 1
            self._stop_event.wait(self.drain_interval)

    def _process_task(self, task: Dict) -> None:
        if self.router.api_dry_run:
            resp = self.mock_client.chat_completion(
                messages=[{"role": "user", "content": task.get('content', '')}])
        else:
            resp = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        input_tok = resp.get('usage', {}).get('prompt_tokens', 0)
        output_tok = resp.get('usage', {}).get('completion_tokens', 0)
        model = task.get('model', self.router.route(task.get('is_priority', False)))
        cost = self.router.compute_cost(model, input_tok, output_tok)
        self.router.budget.record(cost)
        self.router.ledger.record(attempt_id=task.get('original_uuid', ''), model=model,
                                   input_tokens=input_tok, output_tokens=output_tok,
                                   cost_cny=cost, is_dry_run=self.router.api_dry_run)