# -*- coding: utf-8 -*-
"""M4: 来源注册表与爬虫任务文件一致性测试。

验证 china_watch_sources.yaml 中每个来源声明的 crawler_task 在 CrawlTasks/ 下存在。
不调用真实 API，不实际抓取网络。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_YAML = os.path.join(ROOT, "sources", "china_watch_sources.yaml")
CRAWLTASKS_DIR = os.path.join(ROOT, "CrawlTasks")


class TestSourcesTaskConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ServiceComponent.SourceRegistry import SourceRegistry
        cls.registry = SourceRegistry(SOURCES_YAML)

    def test_at_least_8_sources(self):
        """M4 要求首批至少 8 个来源"""
        enabled = self.registry.get_enabled()
        self.assertGreaterEqual(
            len(enabled), 8,
            f"M4 要求首批至少 8 个启用来源，当前 {len(enabled)} 个",
        )

    def test_every_source_has_crawler_task_file(self):
        """每个来源的 crawler_task 文件必须在 CrawlTasks/ 下存在"""
        missing = []
        for src in self.registry.get_enabled():
            task = getattr(src, "crawler_task", None)
            if not task:
                missing.append(f"{src.source_id}: 无 crawler_task 字段")
                continue
            task_path = os.path.join(CRAWLTASKS_DIR, f"{task}.py")
            if not os.path.exists(task_path):
                missing.append(f"{src.source_id}: {task}.py 不存在")
        self.assertEqual(missing, [], f"爬虫任务文件缺失: {missing}")

    def test_channel_urls_nonempty(self):
        """每个启用来源必须至少有 1 个 channel_url"""
        bad = []
        for src in self.registry.get_enabled():
            urls = getattr(src, "channel_urls", []) or []
            if len(urls) == 0:
                bad.append(src.source_id)
        self.assertEqual(bad, [], f"来源缺少 channel_urls: {bad}")

    def test_channel_urls_valid_scheme(self):
        """channel_urls 必须是 http/https（SSRF 防护第一道闸）"""
        bad = []
        for src in self.registry.get_enabled():
            for url in getattr(src, "channel_urls", []) or []:
                if not url.startswith(("http://", "https://")):
                    bad.append(f"{src.source_id}: {url}")
        self.assertEqual(bad, [], f"非法协议 URL: {bad}")

    def test_channel_urls_no_private_ip(self):
        """channel_urls 不得指向 localhost/私有 IP（SSRF 防护）"""
        bad = []
        banned = ("127.0.0.1", "localhost", "0.0.0.0", "::1",
                  "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                  "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                  "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                  "172.30.", "172.31.", "192.168.", "169.254.")
        for src in self.registry.get_enabled():
            for url in getattr(src, "channel_urls", []) or []:
                low = url.lower()
                if any(b in low for b in banned):
                    bad.append(f"{src.source_id}: {url}")
        self.assertEqual(bad, [], f"内网/私有 IP 来源: {bad}")

    def test_priority_sources_marked(self):
        """至少有 1 个 is_priority 来源（用于 Pro 复核路径验证）"""
        priority = self.registry.get_priority()
        self.assertGreaterEqual(len(priority), 1, "至少应有 1 个重点来源")

    def test_source_ids_unique(self):
        """source_id 必须唯一（SourceRegistry 加载时已强制去重，这里复核）"""
        ids = self.registry.all_source_ids()
        self.assertEqual(len(ids), len(set(ids)), f"source_id 重复: {ids}")


class TestSourceRegistrySchema(unittest.TestCase):
    """注册表符合 source_schema.json 基本约束（轻量校验，不依赖 jsonschema 库）"""
    @classmethod
    def setUpClass(cls):
        from ServiceComponent.SourceRegistry import SourceRegistry
        cls.registry = SourceRegistry(SOURCES_YAML)

    def _all_entries(self):
        return [self.registry.get(sid) for sid in self.registry.all_source_ids()]

    def test_required_fields_present(self):
        for s in self._all_entries():
            self.assertTrue(s.source_id, "source_id 必填")
            self.assertTrue(s.name, f"{s.source_id}: name 必填")
            self.assertIn(s.source_type, ("rss", "list_page", "api", "other"))

    def test_source_id_pattern(self):
        import re
        pat = re.compile(r"^[a-z0-9_]+$")
        for s in self._all_entries():
            self.assertTrue(pat.match(s.source_id),
                            f"{s.source_id}: 不符合 ^[a-z0-9_]+$")


if __name__ == "__main__":
    unittest.main()