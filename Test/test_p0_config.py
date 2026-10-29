# -*- coding: utf-8 -*-
"""P0-3: 配置生效规则测试。

覆盖：
- 严格布尔解析（bool("false") 陷阱、非法值 fail-fast）；
- 消费者互斥（同时 true 拒绝、同时 false 合法、字符串开关正确解析）；
- .env.example 不再展示无人读取的开关（防回归）；
- 数值严格解析（budget 负数/NaN 拒绝）。
"""
import unittest
from pathlib import Path

from ServiceComponent.ConfigParsing import (
    InvalidConfigValueError,
    parse_bool,
    parse_bool_or,
    parse_float_or,
)
from ServiceComponent.ConsumerMutex import (
    ConsumerMutexError,
    validate_consumer_mutex,
    validate_startup_config,
)

ROOT = Path(__file__).resolve().parents[1]


class TestStrictBool(unittest.TestCase):
    def test_native_bool(self):
        self.assertIs(parse_bool(True, "k"), True)
        self.assertIs(parse_bool(False, "k"), False)

    def test_string_bools(self):
        self.assertIs(parse_bool("false", "k"), False)   # bool("false") 陷阱封堵
        self.assertIs(parse_bool("FALSE", "k"), False)
        self.assertIs(parse_bool("true", "k"), True)
        self.assertIs(parse_bool("0", "k"), False)
        self.assertIs(parse_bool("yes", "k"), True)
        self.assertIs(parse_bool("off", "k"), False)

    def test_invalid_string_raises(self):
        with self.assertRaises(InvalidConfigValueError):
            parse_bool("flase", "k")   # 拼写错误必须报错而非当 True
        with self.assertRaises(InvalidConfigValueError):
            parse_bool("", "k")

    def test_invalid_type_raises(self):
        with self.assertRaises(InvalidConfigValueError):
            parse_bool(2, "k")
        with self.assertRaises(InvalidConfigValueError):
            parse_bool(None, "k")

    def test_parse_bool_or_default_only_for_none(self):
        self.assertIs(parse_bool_or(None, False, "k"), False)
        self.assertIs(parse_bool_or("false", True, "k"), False)
        with self.assertRaises(InvalidConfigValueError):
            parse_bool_or("", True, "k")


class TestStrictFloat(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_float_or("0.5", 0.0, "k"), 0.5)
        self.assertEqual(parse_float_or(None, 1.5, "k"), 1.5)

    def test_invalid(self):
        with self.assertRaises(InvalidConfigValueError):
            parse_float_or("abc", 0.0, "k")
        with self.assertRaises(InvalidConfigValueError):
            parse_float_or(float("nan"), 0.0, "k")

    def test_bounds(self):
        self.assertEqual(parse_float_or(0.0, 1.0, "k", minimum=0.0), 0.0)
        with self.assertRaises(InvalidConfigValueError):
            parse_float_or(-0.01, 0.0, "k", minimum=0.0)


class TestConsumerMutexStrict(unittest.TestCase):
    def test_both_true_rejected(self):
        with self.assertRaises(ConsumerMutexError):
            validate_consumer_mutex(True, True)

    def test_string_switches_parsed_strictly(self):
        """字符串 "false" 不得被 bool() 误判为 true。"""
        result = validate_startup_config({
            "use_legacy_ai_worker": "false",
            "use_off_peak_drainer": "true",
            "api_enabled": "true",
            "api_dry_run": "false",
        })
        self.assertTrue(result["use_off_peak_drainer"])
        self.assertFalse(result["use_legacy_ai_worker"])
        self.assertTrue(result["api_enabled"])
        self.assertFalse(result["api_dry_run"])

    def test_string_both_true_still_rejected(self):
        with self.assertRaises(ConsumerMutexError):
            validate_startup_config({
                "use_legacy_ai_worker": "true",
                "use_off_peak_drainer": "true",
            })

    def test_invalid_switch_value_fail_fast(self):
        with self.assertRaises(ConsumerMutexError):
            validate_startup_config({"api_enabled": "flase"})

    def test_both_false_collect_only_legal(self):
        result = validate_startup_config({})
        self.assertFalse(result["api_enabled"])
        self.assertTrue(result["api_dry_run"])


class TestEnvExampleNoMisleadingKeys(unittest.TestCase):
    """P0-3：.env.example 不得再展示无人读取的开关（防回归）。"""

    def test_misleading_keys_absent(self):
        content = (ROOT / ".env.example").read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            # 注释行允许提及（历史说明），但生效键（行首 key=）不允许
            if stripped.startswith("#"):
                continue
            for key in ("API_DRY_RUN=", "USE_LEGACY_AI_WORKER=",
                        "USE_OFF_PEAK_DRAINER=", "API_ENABLED="):
                self.assertFalse(
                    stripped.startswith(key),
                    f".env.example 仍包含无人读取的键 {key}")

    def test_config_example_has_new_sections(self):
        import json
        cfg = json.loads((ROOT / "_config" / "config_example.json")
                         .read_text(encoding="utf-8"))
        hub = cfg["intelligence_hub"]
        self.assertIn("push_notifier", hub)
        self.assertIn("disk_guard", hub)
        self.assertFalse(hub["push_notifier"]["enabled"],
                         "示例配置默认关闭真实推送")
        self.assertEqual(hub["china_watch"]["api_enabled"], False,
                         "示例配置默认关闭真实付费调用")


if __name__ == "__main__":
    unittest.main()
