# -*- coding: utf-8 -*-
"""M8: 路径穿越防护测试"""
import os
import unittest


def sanitize_path(user_input: str, base_dir: str) -> str:
    """路径规范化 + 校验，禁止 .. / 绝对路径"""
    normalized = os.path.normpath(os.path.join(base_dir, user_input))
    real_base = os.path.realpath(base_dir)
    real_path = os.path.realpath(normalized)
    if not real_path.startswith(real_base + os.sep) and real_path != real_base:
        raise ValueError(f"路径逃逸: {user_input}")
    return real_path


class TestPathSecurity(unittest.TestCase):
    def test_legitimate_subdir(self):
        path = sanitize_path("data/report.pdf", "/app/files")
        self.assertTrue(path.startswith("/app/files"))

    def test_parent_traversal(self):
        with self.assertRaises(ValueError):
            sanitize_path("../../../etc/passwd", "/app/files")

    def test_absolute_path_bypass(self):
        with self.assertRaises(ValueError):
            sanitize_path("/etc/passwd", "/app/files")

    def test_double_dot_mid(self):
        with self.assertRaises(ValueError):
            sanitize_path("data/../../etc/shadow", "/app/files")


if __name__ == '__main__':
    unittest.main()