# -*- coding: utf-8 -*-
"""
ConfigParsing 严格配置值解析（P0-3 修复）

背景：`bool("false") == True` 是 Python 陷阱。原实现大量使用
`bool(cfg.get('api_enabled', False))` 解析配置，用户在 JSON 中写
字符串 "false" 时会被误判为 True，导致开关反向。

规则（P0-3）：
- 布尔配置必须严格解析：bool 原样返回；字符串仅接受
  true/false/1/0/yes/no/on/off（大小写不敏感）；其余值抛
  InvalidConfigValueError（fail-fast，启动失败并给出明确提示）。
- 数值配置解析失败同样 fail-fast，不静默回退默认值。
"""


class InvalidConfigValueError(ValueError):
    """配置值非法（无法严格解析）。启动时应中止相关功能。"""


_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


def parse_bool(value, key: str = "<unknown>") -> bool:
    """严格解析布尔配置值。

    - bool：原样返回；
    - int：仅 0/1 合法；
    - str：true/false/1/0/yes/no/on/off（大小写不敏感）；
    - 其他（含 None、空串、任意字符串）：抛 InvalidConfigValueError。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        raise InvalidConfigValueError(
            f"配置项 {key} 的值 {value!r} 不是合法布尔（int 仅允许 0/1）")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        raise InvalidConfigValueError(
            f"配置项 {key} 的值 {value!r} 不是合法布尔"
            f"（允许：true/false/1/0/yes/no/on/off）")
    raise InvalidConfigValueError(
        f"配置项 {key} 的类型 {type(value).__name__} 不是布尔（值：{value!r}）")


def parse_bool_or(value, default: bool, key: str = "<unknown>") -> bool:
    """宽松入口：值为 None 或键缺失（None）时返回默认值，否则严格解析。

    用于"配置缺失有默认值、配置存在但非法必须报错"的场景。
    注意：空字符串视为非法（用户显式写了值但写错了），
    只有 None（键不存在 / 显式 null）才走默认值。
    """
    if value is None:
        return default
    return parse_bool(value, key)


def parse_float_or(value, default: float, key: str = "<unknown>",
                   minimum: float = None, maximum: float = None) -> float:
    """严格解析数值配置。None 走默认值；非法值（含 NaN/Inf/越界）抛错。"""
    import math

    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise InvalidConfigValueError(
            f"配置项 {key} 的值 {value!r} 不是合法数值")
    if math.isnan(parsed) or math.isinf(parsed):
        raise InvalidConfigValueError(
            f"配置项 {key} 的值 {value!r} 不是有限数值")
    if minimum is not None and parsed < minimum:
        raise InvalidConfigValueError(
            f"配置项 {key} 的值 {parsed} 低于下限 {minimum}")
    if maximum is not None and parsed > maximum:
        raise InvalidConfigValueError(
            f"配置项 {key} 的值 {parsed} 超过上限 {maximum}")
    return parsed
