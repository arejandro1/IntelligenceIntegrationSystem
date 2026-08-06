# -*- coding: utf-8 -*-
"""M1 配置测试：config_example.json 开关、依赖分层。不调用 API。"""
import json, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_config():
    with open(os.path.join(ROOT, '_config', 'config_example.json'), 'r', encoding='utf-8') as f:
        return json.load(f)

class TestM1Config(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.cw = cls.config['intelligence_hub'].get('china_watch', {})

    def test_vectordb_enabled_false(self):
        self.assertFalse(self.config['intelligence_hub']['vectordb']['enabled'])

    def test_api_enabled_false(self):
        self.assertFalse(self.cw.get('api_enabled'))

    def test_api_dry_run_true(self):
        self.assertTrue(self.cw.get('api_dry_run'))

    def test_use_legacy_ai_worker_false(self):
        self.assertFalse(self.cw.get('use_legacy_ai_worker'))

    def test_use_off_peak_drainer_false(self):
        self.assertFalse(self.cw.get('use_off_peak_drainer'))

    def test_concurrency_values(self):
        c = self.cw.get('crawler_concurrency', {})
        self.assertEqual(c.get('normal'), 2)
        self.assertEqual(c.get('playwright'), 1)
        self.assertEqual(c.get('pdf_worker'), 1)
        self.assertEqual(c.get('ai_worker'), 1)

class TestRequirements(unittest.TestCase):
    def test_server_no_vectordb(self):
        with open(os.path.join(ROOT, 'requirements-server.txt'), 'r', encoding='utf-8') as f:
            content = f.read()
        for b in ['chromadb', 'sentence_transformers', 'onnxruntime', 'faiss', 'huggingface-hub', 'langchain_text_splitters', 'torch']:
            self.assertNotRegex(content, rf'^{re.escape(b)}\s*=', msg=f'不得包含 {b}')

    def test_server_has_core(self):
        with open(os.path.join(ROOT, 'requirements-server.txt'), 'r', encoding='utf-8') as f:
            content = f.read()
        for p in ['Flask', 'pymongo', 'playwright', 'feedparser']:
            self.assertIn(p, content, msg=f'缺少 {p}')

    def test_dev_refers_requirements(self):
        with open(os.path.join(ROOT, 'requirements-dev.txt'), 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('-r requirements.txt', content)

    def test_requirements_txt_unchanged(self):
        with open(os.path.join(ROOT, 'requirements.txt'), 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('chromadb==1.2.1', content)

if __name__ == '__main__':
    unittest.main()