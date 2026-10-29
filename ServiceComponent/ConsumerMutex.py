# -*- coding: utf-8 -*-
"""
ConsumerMutex AI 消费者互斥启动校验（M3 修复）

按 .clinerules/10-architecture.md "新旧AI消费者互斥" 要求：
- 旧 AI Worker（LEGACY）和 OffPeakQueueDrainer（新）不得同时启用
- 两者同时为 true 必须启动失败
- 两者同时为 false 是合法的只采集模式
- 不得在 M1—M2 中默认启用旧 AI Worker

P0-3 修复：布尔开关改用 ConfigParsing.parse_bool 严格解析，
避免 bool("false") 为真导致开关反向；非法值 fail-fast。
"""
from __future__ import annotations

from typing import Any

from ServiceComponent.ConfigParsing import InvalidConfigValueError, parse_bool_or


class ConsumerMutexError(RuntimeError):
    """AI 消费者互斥校验失败。启动时应中止。"""


def validate_consumer_mutex(
    use_legacy_ai_worker: bool,
    use_off_peak_drainer: bool,
) -> None:
    """校验新旧 AI 消费者互斥。

    - 两者同时为 True：抛 ConsumerMutexError（启动失败）
    - 两者同时为 False：合法（只采集模式）
    - 仅其一为 True：合法

    本函数不读取系统配置，由调用方传入，便于测试。
    """
    if use_legacy_ai_worker and use_off_peak_drainer:
        raise ConsumerMutexError(
            "新旧 AI 消费者不得同时启用："
            "USE_LEGACY_AI_WORKER=true 且 USE_OFF_PEAK_DRAINER=true。"
            "请仅启用其一，或两者都关闭进入只采集模式。"
        )


def validate_startup_config(config: dict[str, Any]) -> dict[str, bool]:
    """从 china_watch 配置块读取开关并校验互斥。

    config 形如：
        {"api_enabled": bool, "api_dry_run": bool,
         "use_legacy_ai_worker": bool, "use_off_peak_drainer": bool, ...}

    布尔值严格解析（parse_bool_or）：缺失（None）用默认 False，
    非法值（如 "flase"、2、[]）抛 InvalidConfigValueError（fail-fast），
    不再出现 bool("false") 为真的静默反向。

    校验失败抛 ConsumerMutexError，使进程启动失败（fail-fast）。
    返回解析后的四个开关，供调用方复用（避免二次解析）。
    """
    cw = config or {}
    try:
        use_legacy = parse_bool_or(cw.get("use_legacy_ai_worker"), False,
                                   "china_watch.use_legacy_ai_worker")
        use_drainer = parse_bool_or(cw.get("use_off_peak_drainer"), False,
                                    "china_watch.use_off_peak_drainer")
        api_enabled = parse_bool_or(cw.get("api_enabled"), False,
                                    "china_watch.api_enabled")
        api_dry_run = parse_bool_or(cw.get("api_dry_run"), True,
                                    "china_watch.api_dry_run")
    except InvalidConfigValueError as e:
        # 布尔值非法属于配置错误，同样必须 fail-fast（P0-3）
        raise ConsumerMutexError(f"china_watch 布尔开关解析失败：{e}") from e
    validate_consumer_mutex(use_legacy, use_drainer)
    return {
        "api_enabled": api_enabled,
        "api_dry_run": api_dry_run,
        "use_legacy_ai_worker": use_legacy,
        "use_off_peak_drainer": use_drainer,
    }