# -*- coding: utf-8 -*-
"""P0-2: 认证数据库持久化写权限与迁移测试。

覆盖：
- 新默认路径 _data/Authentication.db（可写目录），解析无副作用；
- 旧库自动迁移（migrate_legacy_user_db）：账号保留、幂等、失败显式报错；
- UserManager 打开默认路径时触发迁移（hook 验证）；
- AUTH_DB_PATH 环境变量覆盖；
- 登录成功/失败的登录日志写入（SQLite 写路径真实执行）；
- SQLite 打开失败（如目录只读/损坏）时 _init_db fail-fast（不吞异常）。

全部使用临时目录与注入路径，不触碰真实 _config/Authentication.db。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ServiceComponent.UserManager import UserManager  # noqa: E402


class _TempEnv:
    """构造隔离的 PRJ_PATH/_config/_data 环境（patch GlobalConfig 常量）。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.config_dir = self.base / "_config"
        self.data_dir = self.base / "_data"
        self.config_dir.mkdir()
        self.data_dir.mkdir()

    def cleanup(self):
        self.tmp.cleanup()


def _patch_globals(env):
    import GlobalConfig
    return patch.multiple(GlobalConfig,
                          PRJ_PATH=str(env.base),
                          CONFIG_PATH=str(env.config_dir),
                          DATA_PATH=str(env.data_dir))


class TestResolveUserDbPath(unittest.TestCase):
    def test_default_resolves_to_data_dir_without_side_effects(self):
        """无旧库：默认解析到可写的 _data 目录，且不产生任何文件。"""
        from GlobalConfig import resolve_user_db_path
        env = _TempEnv()
        with _patch_globals(env), patch.dict(os.environ, {"AUTH_DB_PATH": ""}):
            path = resolve_user_db_path()
        self.assertEqual(str(env.data_dir / "Authentication.db"), path)
        self.assertFalse((env.data_dir / "Authentication.db").exists(),
                         "路径解析不得产生文件副作用（导入安全）")
        env.cleanup()

    def test_env_override(self):
        from GlobalConfig import resolve_user_db_path
        env = _TempEnv()
        override = str(env.data_dir / "custom-auth.db")
        with _patch_globals(env), patch.dict(os.environ, {"AUTH_DB_PATH": override}):
            self.assertEqual(resolve_user_db_path(), override)
        env.cleanup()


class TestMigrateLegacyUserDb(unittest.TestCase):
    def test_legacy_db_copied_with_accounts(self):
        """旧库存在：自动复制到新路径（账号保留），旧文件保留，幂等。"""
        from GlobalConfig import migrate_legacy_user_db
        env = _TempEnv()
        legacy = str(env.config_dir / "Authentication.db")
        UserManager(legacy).create_user("admin1", "password1", roles=[])
        target = str(env.data_dir / "Authentication.db")
        with _patch_globals(env):
            result = migrate_legacy_user_db(target, legacy_paths=[legacy])
        self.assertEqual(result, target)
        self.assertTrue(Path(target).exists(), "旧库应被复制到新路径")
        self.assertTrue(Path(legacy).exists(), "旧文件保留（不删除用户数据）")
        um = UserManager(target)
        ok, _ = um.authenticate("admin1", "password1", "127.0.0.1")
        self.assertTrue(ok, "迁移后已有账号必须可登录")
        # 幂等：再次迁移不覆盖（目标已存在直接返回）
        with _patch_globals(env):
            migrate_legacy_user_db(target, legacy_paths=[legacy])
        env.cleanup()

    def test_migration_failure_raises_not_silent(self):
        """迁移失败（复制出错）→ 报错，禁止静默新建空库丢账号。

        Windows 的 chmod 不阻止写入，故用 patch shutil.copy2 注入
        OSError 模拟迁移失败（等价于目标目录只读/磁盘满）。
        """
        from GlobalConfig import migrate_legacy_user_db
        env = _TempEnv()
        legacy = str(env.config_dir / "Authentication.db")
        UserManager(legacy).create_user("admin2", "password2", roles=[])
        target = str(env.data_dir / "Authentication.db")
        with _patch_globals(env), \
                patch("shutil.copy2", side_effect=OSError("permission denied")):
            with self.assertRaises(RuntimeError):
                migrate_legacy_user_db(target, legacy_paths=[legacy])
        self.assertFalse(Path(target).exists(),
                         "迁移失败后不得留下半成品/空库")
        env.cleanup()

    def test_no_legacy_returns_target(self):
        """无旧库：返回目标路径（首次部署由 UserManager 建表）。"""
        from GlobalConfig import migrate_legacy_user_db
        env = _TempEnv()
        target = str(env.data_dir / "Authentication.db")
        with _patch_globals(env):
            self.assertEqual(
                migrate_legacy_user_db(target, legacy_paths=[]), target)
        env.cleanup()


class TestUserManagerMigrationHook(unittest.TestCase):
    def test_default_path_triggers_migration(self):
        """UserManager 打开默认路径时自动迁移旧库（账号保留）。"""
        import GlobalConfig
        env = _TempEnv()
        legacy = str(env.config_dir / "Authentication.db")
        UserManager(legacy).create_user("admin3", "password3", roles=[])
        default_path = str(env.data_dir / "Authentication.db")
        with _patch_globals(env), \
                patch.object(GlobalConfig, "DEFAULT_USER_DB_PATH", default_path):
            um = UserManager(default_path)
        ok, _ = um.authenticate("admin3", "password3", "127.0.0.1")
        self.assertTrue(ok, "经默认路径打开时旧账号必须可用（非空库）")
        env.cleanup()

    def test_custom_path_skips_migration(self):
        """非默认路径不触发迁移（测试/隔离场景）。"""
        import GlobalConfig
        env = _TempEnv()
        legacy = str(env.config_dir / "Authentication.db")
        UserManager(legacy).create_user("admin4", "password4", roles=[])
        custom = str(env.data_dir / "isolated.db")
        with _patch_globals(env), \
                patch.object(GlobalConfig, "DEFAULT_USER_DB_PATH",
                             str(env.data_dir / "Authentication.db")):
            UserManager(custom)
        # custom 库为全新空库（合法：显式指定路径 = 全新数据库场景）
        um = UserManager(custom)
        ok, _ = um.authenticate("admin4", "password4", "127.0.0.1")
        self.assertFalse(ok, "隔离库不含旧账号")


class TestUserManagerFailFast(unittest.TestCase):
    def test_init_db_open_failure_raises(self):
        """SQLite 打开失败（模拟目录只读/损坏）→ _init_db 抛错（fail-fast）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "Authentication.db")

            def _raise_connect(*args, **kwargs):
                raise sqlite3.OperationalError("unable to open database file")

            with patch("ServiceComponent.UserManager.sqlite3.connect",
                       side_effect=_raise_connect):
                with self.assertRaises(RuntimeError):
                    UserManager(db)

    def test_login_log_written_on_success_and_failure(self):
        """登录成功/失败均写登录日志（SQLite 真实写路径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "Authentication.db")
            um = UserManager(db)
            um.create_user("alice", "alice1234", roles=[])
            ok, _ = um.authenticate("alice", "alice1234", "10.0.0.1")
            self.assertTrue(ok)
            bad, _ = um.authenticate("alice", "wrongpass1", "10.0.0.2")
            self.assertFalse(bad)
            logs = um.get_login_logs(per_page=10, page=1)
            self.assertEqual(len(logs), 2)
            results = {log["result"] for log in logs}
            self.assertEqual(results, {"SUCCESS", "FAILURE"})

    def test_journal_files_in_writable_dir(self):
        """可写目录中 SQLite 正常运行（journal/WAL 由 SQLite 自行管理）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "Authentication.db")
            um = UserManager(db)
            um.create_user("bob", "bob12345", roles=[])
            ok, _ = um.authenticate("bob", "bob12345", "127.0.0.1")
            self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

