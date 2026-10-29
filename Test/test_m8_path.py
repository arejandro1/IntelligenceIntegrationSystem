# -*- coding: utf-8 -*-
"""M8: 路径穿越防护测试"""
import os
import unittest


def sanitize_path(user_input: str, base_dir: str) -> str:
    """路径规范化 + 校验，禁止 .. / 绝对路径"""
    normalized = os.path.normpath(os.path.join(base_dir, user_input))
    real_base = os.path.realpath(base_dir)
    real_path = os.path.realpath(normalized)
    # Windows 下盘符大小写不敏感（如 d:\ vs D:\），比较前统一规范化
    norm_base = os.path.normcase(real_base)
    norm_path = os.path.normcase(real_path)
    if not norm_path.startswith(norm_base + os.sep) and norm_path != norm_base:
        raise ValueError(f"路径逃逸: {user_input}")
    return real_path


class TestPathSecurity(unittest.TestCase):
    def setUp(self):
        # 使用 os.path.join 构造平台无关的 base_dir，避免 Windows/Unix 路径差异
        self.base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_tmp_base")
        os.makedirs(self.base, exist_ok=True)

    def tearDown(self):
        import shutil
        if os.path.exists(self.base):
            shutil.rmtree(self.base, ignore_errors=True)

    def test_legitimate_subdir(self):
        # 合法子目录应返回 base 之内的路径（Windows 盘符大小写不敏感）
        path = sanitize_path("data/report.pdf", self.base)
        self.assertTrue(os.path.normcase(path).startswith(os.path.normcase(self.base)))

    def test_parent_traversal(self):
        with self.assertRaises(ValueError):
            sanitize_path("../../../etc/passwd", self.base)

    def test_absolute_path_bypass(self):
        # Windows 上 /etc/passwd 会被 normpath 转为当前盘 \etc\passwd；用一个明确的绝对路径
        abs_path = os.path.join(os.path.dirname(self.base), "evil.txt")
        with self.assertRaises(ValueError):
            sanitize_path(abs_path, self.base)

    def test_double_dot_mid(self):
        with self.assertRaises(ValueError):
            sanitize_path("data/../../etc/shadow", self.base)


if __name__ == '__main__':
    unittest.main()