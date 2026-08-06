# -*- coding: utf-8 -*-
"""M2: SourceRegistry + RulePreFilter 测试。不调用 API。"""
import json
import os
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCES_YAML = os.path.join(ROOT, 'sources', 'china_watch_sources.yaml')
SCHEMA_JSON = os.path.join(ROOT, 'sources', 'source_schema.json')

RULES = {
    'reject_patterns': ['赌博', 'gambling', 'casino', '色情', 'porn'],
    'metadata_only_patterns': [],
    'min_content_length': 10,
    'require_title': True,
}


def _make_sources_yaml(tmpdir: str) -> str:
    """创建含 2 个来源（1 启用 1 停用）的临时 YAML"""
    path = os.path.join(tmpdir, 'sources_test.yaml')
    data = {
        'sources': [
            {
                'source_id': 'voa',
                'name': 'Voice of America',
                'source_type': 'rss',
                'channel_urls': ['https://www.voachinese.com/api/zhengce'],
                'enabled': True,
                'is_priority': True,
                'crawler_task': 'task_crawl_voanews',
                'tags': ['美国'],
            },
            {
                'source_id': 'bbc',
                'name': 'BBC',
                'source_type': 'rss',
                'channel_urls': ['https://feeds.bbci.co.uk/news/rss.xml'],
                'enabled': False,
                'is_priority': False,
                'crawler_task': 'task_crawl_bbc',
                'tags': ['英国'],
            },
        ]
    }
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True)
    return path


class TestSourceRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.yaml_path = _make_sources_yaml(cls.tmpdir)
        from ServiceComponent.SourceRegistry import SourceRegistry
        cls.registry = SourceRegistry(cls.yaml_path)

    def test_load_count(self):
        self.assertEqual(self.registry.count(), 2)

    def test_get_enabled(self):
        enabled = self.registry.get_enabled()
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0].source_id, 'voa')

    def test_get_priority(self):
        priority = self.registry.get_priority()
        self.assertEqual(len(priority), 1)
        self.assertEqual(priority[0].source_id, 'voa')

    def test_is_enabled(self):
        self.assertTrue(self.registry.is_enabled('voa'))
        self.assertFalse(self.registry.is_enabled('bbc'))

    def test_exists(self):
        self.assertTrue(self.registry.exists('voa'))
        self.assertFalse(self.registry.exists('unknown'))


class TestRulePreFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        yaml_path = _make_sources_yaml(cls.tmpdir)
        from ServiceComponent.SourceRegistry import SourceRegistry
        from ServiceComponent.RulePreFilter import RulePreFilter
        cls.registry = SourceRegistry(yaml_path)
        cls.prefilter = RulePreFilter(cls.registry, RULES)

    def _screen(self, title='', content='', source='voa', uuid='u1'):
        raw = {'UUID': uuid, 'source': source, 'title': title, 'content': content}
        return self.prefilter.screen(raw)

    def test_needs_ai_pass(self):
        r = self._screen(title='China trade', content='China and US trade talks continue today.')
        self.assertEqual(r.decision, 'NEEDS_AI')

    def test_rejected_missing_uuid(self):
        r = self._screen(uuid='')
        self.assertEqual(r.decision, 'RULE_REJECTED')
        self.assertEqual(r.matched_rule, 'PARAM_UUID_MISSING')

    def test_rejected_missing_source(self):
        r = self._screen(source='')
        self.assertEqual(r.decision, 'RULE_REJECTED')
        self.assertEqual(r.matched_rule, 'PARAM_SOURCE_MISSING')

    def test_rejected_unregistered_source(self):
        r = self._screen(source='unknown')
        self.assertEqual(r.decision, 'RULE_REJECTED')
        self.assertEqual(r.matched_rule, 'SOURCE_UNREGISTERED')

    def test_rejected_disabled_source(self):
        r = self._screen(source='bbc')
        self.assertEqual(r.decision, 'RULE_REJECTED')
        self.assertEqual(r.matched_rule, 'SOURCE_DISABLED')

    def test_rejected_empty_title(self):
        r = self._screen(title='', content='This is a long enough content body.')
        self.assertEqual(r.decision, 'RULE_REJECTED')
        self.assertEqual(r.matched_rule, 'TITLE_REQUIRED')

    def test_metadata_only_short_content(self):
        r = self._screen(title='Short', content='tiny')
        self.assertEqual(r.decision, 'METADATA_ONLY')
        self.assertEqual(r.matched_rule, 'CONTENT_TOO_SHORT')

    def test_rejected_pattern(self):
        r = self._screen(title='News', content='This article mentions gambling in the casino.')
        self.assertEqual(r.decision, 'RULE_REJECTED')
        self.assertTrue(r.matched_rule.startswith('REJECT_PATTERN:'))

    def test_hash_stable(self):
        r1 = self._screen(title='Same', content='Same content body here.')
        r2 = self._screen(title='Same', content='Same content body here.')
        self.assertEqual(r1.content_hash, r2.content_hash)


class TestSourcesFiles(unittest.TestCase):
    def test_sources_yaml_exists(self):
        self.assertTrue(os.path.exists(SOURCES_YAML))

    def test_schema_json_exists(self):
        self.assertTrue(os.path.exists(SCHEMA_JSON))
        with open(SCHEMA_JSON, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        self.assertEqual(schema['title'], 'ChinaWatch Source Registry Schema')


if __name__ == '__main__':
    unittest.main()