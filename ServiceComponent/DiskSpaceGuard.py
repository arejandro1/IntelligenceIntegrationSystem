# -*- coding: utf-8 -*-
"""
DiskSpaceGuard 磁盘空间守卫（P1-8）

实现文档承诺的磁盘三级策略（docs/synology_deployment.md / 50-synology.md）：
- 使用率 >= warn_percent（默认 70）：告警，通知运维检查清理；
- 使用率 >= stop_push_percent（默认 85）：严重告警，停止低优先级任务
  （推送入队）；采集、AI、归档继续；
- 使用率 >= stop_collect_percent（默认 95）：紧急降级，停止采集与队列
  消费，仅保留核心查询与告警。

设计要点：
- 使用率检测函数可注入（usage_fn），测试不依赖真实磁盘（FakeClock/
  FakeUsage 原则，见 30-testing）；
- check() 返回结构化结果并更新 level / should_stop_push /
  should_stop_collect，Hub 与 Web 层按此降级；
- 级别只升不降的迟滞未实现（回落即恢复），当前策略文档未要求迟滞；
  恢复动作记录在 last_result 中供审计。

批次四修复：
- 初始状态与检测失败不再显示为"已确认正常"（LEVEL_UNKNOWN /
  check_error 展示），检测异常对运维可见；
- 达到 stop_collect（95%）后：停止认领新分析任务和新增付费请求
  （OffPeakQueueDrainer.pause_predicate 注入 should_stop_collect）；
  在途请求自然完成（不中断已发出的调用）；已付费分析结果的交付
  与重放继续执行（保护已支付成果，等待空间回收后归档落盘）；
  核心查询与告警不受影响；
- 空间恢复（回落到 95% 以下）后，消费自动继续（级别回落即恢复，
  每轮 drain 循环重新评估 pause_predicate）。
"""
from __future__ import annotations

import logging
import shutil
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_STOP_PUSH = "stop_push"
LEVEL_STOP_COLLECT = "stop_collect"
# 批次四：检测异常/未检测——初始状态与检测失败时不得显示为"已确认正常"。
# 该状态下不触发降级（fail-open：检测失败不代表磁盘满，停止业务影响更大），
# 但 stats() 的展示级别为 check_error，运维可见"检测异常"。
LEVEL_UNKNOWN = "unknown"


def _default_usage_fn(path: str) -> float:
    """默认使用率检测：shutil.disk_usage 百分比。"""
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100.0


class DiskSpaceGuard:
    """磁盘空间守卫（线程安全；check 由后台线程周期调用）。"""

    def __init__(
        self,
        watch_path: str,
        warn_percent: float = 70.0,
        stop_push_percent: float = 85.0,
        stop_collect_percent: float = 95.0,
        usage_fn: Optional[Callable[[str], float]] = None,
    ) -> None:
        if not 0 < warn_percent < stop_push_percent < stop_collect_percent <= 100:
            raise ValueError(
                f"磁盘阈值非法：warn={warn_percent} stop_push={stop_push_percent} "
                f"stop_collect={stop_collect_percent}（要求 0 < warn < stop_push < stop_collect <= 100）")
        self.watch_path = watch_path
        self.warn_percent = warn_percent
        self.stop_push_percent = stop_push_percent
        self.stop_collect_percent = stop_collect_percent
        self._usage_fn = usage_fn or _default_usage_fn
        self._lock = threading.Lock()
        # 批次四：初始 level 为 unknown——从未检测过不得显示为"已确认正常"。
        self.level: str = LEVEL_UNKNOWN
        self.last_result: Dict[str, Any] = {"percent": None, "level": LEVEL_UNKNOWN,
                                            "error": None, "check_failed": False}

    def check(self) -> Dict[str, Any]:
        """执行一次检测并更新级别。返回结构化结果。

        批次四：检测失败时内部判定保持上次级别（降级开关不因检测抖动
        翻转），但 last_result 标记 check_failed=True——stats() 展示
        "check_error"（检测异常），不再冒充"已确认正常"；从未成功检测过
        的初始状态展示 unknown。
        """
        try:
            percent = float(self._usage_fn(self.watch_path))
            error = None
        except Exception as e:
            # 检测失败不改变现有级别（保持上一次判定），仅记录错误：
            # 磁盘信息读取失败不代表磁盘恢复健康，也不代表磁盘已满。
            percent = None
            error = str(e)
            with self._lock:
                self.last_result = {"percent": None, "level": self.level,
                                    "error": error, "check_failed": True}
            logger.error("磁盘使用率检测失败（watch_path=%s）：%s", self.watch_path, e)
            return dict(self.last_result)

        if percent >= self.stop_collect_percent:
            new_level = LEVEL_STOP_COLLECT
        elif percent >= self.stop_push_percent:
            new_level = LEVEL_STOP_PUSH
        elif percent >= self.warn_percent:
            new_level = LEVEL_WARN
        else:
            new_level = LEVEL_OK

        with self._lock:
            prev = self.level
            self.level = new_level
            self.last_result = {"percent": round(percent, 2), "level": new_level,
                                "error": None, "check_failed": False}

        if new_level != prev:
            # 级别变化才告警，避免周期刷屏
            if new_level == LEVEL_WARN:
                logger.warning("[DiskGuard] 磁盘使用率 %.1f%%（>= %.0f%%）："
                               "请检查清理。", percent, self.warn_percent)
            elif new_level == LEVEL_STOP_PUSH:
                logger.error("[DiskGuard] 磁盘使用率 %.1f%%（>= %.0f%%）：严重告警，"
                             "停止推送入队。", percent, self.stop_push_percent)
            elif new_level == LEVEL_STOP_COLLECT:
                logger.critical("[DiskGuard] 磁盘使用率 %.1f%%（>= %.0f%%）：紧急降级，"
                                "停止采集与队列消费，仅保留核心查询与告警。",
                                percent, self.stop_collect_percent)
            elif new_level == LEVEL_OK:
                logger.info("[DiskGuard] 磁盘使用率回落到 %.1f%%，恢复正常运行。", percent)
        return dict(self.last_result)

    # ---------- 降级判定（业务侧读取） ----------
    @property
    def should_stop_push(self) -> bool:
        """>=85%：停止推送入队（低优先级任务）。"""
        return self.level in (LEVEL_STOP_PUSH, LEVEL_STOP_COLLECT)

    @property
    def should_stop_collect(self) -> bool:
        """>=95%：停止采集与队列消费。"""
        return self.level == LEVEL_STOP_COLLECT

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            # 批次四：展示级别区分内部判定与检测状态——
            # - check_failed（最近一次检测失败）→ 展示 "check_error"（检测异常）；
            # - unknown（从未成功检测）→ 展示 "unknown"（未检测）；
            # - 其余 → 实际判定级别。
            # 检测失败/未检测不得继续展示为"已确认正常"。
            if self.last_result.get("check_failed"):
                display_level = "check_error"
            elif self.level == LEVEL_UNKNOWN:
                display_level = LEVEL_UNKNOWN
            else:
                display_level = self.level
            return {
                "watch_path": self.watch_path,
                "percent": self.last_result.get("percent"),
                "level": display_level,
                "internal_level": self.level,
                "check_error": self.last_result.get("error"),
                "should_stop_push": self.should_stop_push,
                "should_stop_collect": self.should_stop_collect,
                "thresholds": {
                    "warn": self.warn_percent,
                    "stop_push": self.stop_push_percent,
                    "stop_collect": self.stop_collect_percent,
                },
                "error": self.last_result.get("error"),
            }


def start_disk_guard_worker(guard: DiskSpaceGuard, interval_seconds: float = 300.0,
                            stop_event: Optional[threading.Event] = None) -> threading.Thread:
    """启动后台周期检测线程（daemon）。

    interval_seconds：检测间隔（默认 300s，可配置 intelligence_hub.disk_guard.interval_seconds）。
    """
    ev = stop_event or threading.Event()

    def _worker() -> None:
        while not ev.is_set():
            try:
                guard.check()
            except Exception as e:  # 防御：check 内部已捕获，双保险防线程死亡
                logger.error("DiskSpaceGuard worker 异常：%s", e)
            ev.wait(interval_seconds)

    t = threading.Thread(target=_worker, name="DiskSpaceGuard", daemon=True)
    t.start()
    return t
