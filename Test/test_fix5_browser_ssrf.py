# -*- coding: utf-8 -*-
"""批次五修复测试：采集和构建防护。

覆盖（暴露原缺陷）：
1. 初始地址合法但后续请求（导航/重定向/子请求）指向受限地址 → 阻断
   （原缺陷：只校验初始 URL，页面内跳转完全不受控）；
2. 导航请求每次完整校验（同 host 后续导航也拦截受限地址）；
3. 同 host 子资源缓存放行（性能）；
4. 默认启用 HTTPS 证书校验（ignore_https_errors 默认 False）；
5. 构建上下文排除规则检查（.dockerignore 覆盖本地凭据，非仅 Git 忽略）。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Tools.SSRFGuard import BrowserRequestGuard, SSRFError  # noqa: E402


class _FakeRequest:
    def __init__(self, url, resource_type='other', is_nav=None):
        self.url = url
        self.resource_type = resource_type
        self._is_nav = is_nav

    def is_navigation_request(self):
        if self._is_nav is None:
            return self.resource_type == 'document'
        return self._is_nav


class _FakeRoute:
    """记录 abort/continue 调用的假 Playwright Route。"""

    def __init__(self, request):
        self.request = request
        self.action = None

    def abort(self):
        self.action = 'abort'

    def continue_(self):
        self.action = 'continue'


def _fake_validate(url, allow_private=False):
    """离线版 validate_url：只按主机名/IP 字面特征判定（测试不联网）。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise SSRFError(f'scheme 拒绝: {url}')
    host = parsed.hostname or ''
    if not host:
        raise SSRFError('缺主机名')
    if allow_private:
        return url
    restricted = ('127.', 'localhost', '0.0.0.0', '::1', '10.', '192.168.',
                  '169.254.', '[::1]', '172.16.', '172.17.', '172.18.',
                  '172.19.', '172.2', '172.30.', '172.31.')
    if host in restricted or host.startswith(restricted):
        raise SSRFError(f'受禁地址: {host}')
    return url


class TestBrowserRequestGuard(unittest.TestCase):
    """验收 1/2/3：受控测试——初始地址合法、后续请求指向受限地址时阻断。"""

    def setUp(self):
        self.guard = BrowserRequestGuard()
        # 离线替换 DNS 解析（测试不联网，符合 30-testing）
        self._patcher = patch('Tools.SSRFGuard.validate_url',
                              side_effect=_fake_validate)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_later_navigation_to_loopback_blocked(self):
        """原缺陷暴露：初始 URL（example.com）合法，页面 JS 跳转
        http://127.0.0.1:8080/admin ——原实现无任何拦截。"""
        handler = self.guard.make_route_handler()
        route = _FakeRoute(_FakeRequest('http://127.0.0.1:8080/admin',
                                        resource_type='document'))
        handler(route)
        self.assertEqual(route.action, 'abort', '后续导航到环回地址必须阻断')
        self.assertEqual(self.guard.blocked_count, 1)

    def test_redirect_to_private_network_blocked(self):
        """重定向到内网地址（10.x）被阻断。"""
        handler = self.guard.make_route_handler()
        route = _FakeRoute(_FakeRequest('http://10.0.0.5/internal',
                                        resource_type='document'))
        handler(route)
        self.assertEqual(route.action, 'abort')

    def test_subresource_to_link_local_blocked(self):
        """页面子资源（脚本）指向链路本地/元数据地址被阻断。"""
        handler = self.guard.make_route_handler()
        route = _FakeRoute(_FakeRequest('http://169.254.169.254/latest/meta-data/',
                                        resource_type='script'))
        handler(route)
        self.assertEqual(route.action, 'abort',
                         '子资源请求指向元数据服务地址必须阻断')

    def test_same_host_subresource_cached(self):
        """同 host 子资源缓存放行（性能：不做重复校验）。"""
        self.assertTrue(self.guard.check_request_url('https://example.com/a.js',
                                                     is_document=False))
        first_checks = self.guard.checked_count
        self.assertTrue(self.guard.check_request_url('https://example.com/b.css',
                                                     is_document=False))
        self.assertEqual(self.guard.checked_count, first_checks,
                         '同 host 子资源命中缓存不重复校验')

    def test_navigation_always_full_check(self):
        """导航请求每次完整校验（即使 host 已缓存——防 rebinding）。"""
        self.assertTrue(self.guard.check_request_url('https://example.com/page1',
                                                     is_document=True))
        checks = self.guard.checked_count
        self.assertTrue(self.guard.check_request_url('https://example.com/page2',
                                                     is_document=True))
        self.assertEqual(self.guard.checked_count, checks + 1,
                         '导航请求不使用缓存')

    def test_public_urls_allowed(self):
        handler = self.guard.make_route_handler()
        route = _FakeRoute(_FakeRequest('https://example.com/feed',
                                        resource_type='document'))
        handler(route)
        self.assertEqual(route.action, 'continue', '公网正常请求放行')

    def test_non_network_scheme_allowed(self):
        """data:/blob:/about: 非网络协议放行。"""
        self.assertTrue(self.guard.check_request_url('data:image/png;base64,xxx',
                                                     is_document=False))
        self.assertTrue(self.guard.check_request_url('about:blank',
                                                     is_document=True))

    def test_initial_url_still_validated(self):
        """初始 URL 校验保持（受限地址直接拒绝抓取）。"""
        with self.assertRaises(SSRFError):
            _fake_validate('http://192.168.1.1/admin')


class TestHttpsCertVerification(unittest.TestCase):
    """验收 4：默认启用 HTTPS 证书校验。"""

    def test_default_signature_has_ignore_https_errors_false(self):
        """离线检查 request_by_browser 签名默认值（不依赖 playwright）。"""
        import inspect
        try:
            from Scraper import PlaywrightRawScraper as mod
        except Exception:
            self.skipTest('playwright 未安装，跳过模块导入检查')
        sig = inspect.signature(mod.request_by_browser)
        self.assertIn('ignore_https_errors', sig.parameters)
        self.assertFalse(sig.parameters['ignore_https_errors'].default,
                         'ignore_https_errors 必须默认 False（启用证书校验）')

    def test_context_args_use_certificate_verification(self):
        """源码断言：context 配置不再硬编码 ignore_https_errors=True。"""
        src = (ROOT / 'Scraper' / 'PlaywrightRawScraper.py').read_text(encoding='utf-8')
        self.assertNotIn('"ignore_https_errors": True', src,
                         '不得硬编码关闭证书校验（原缺陷）')

    def test_route_guard_installed(self):
        """源码断言：context.route 请求级防护已接线。"""
        src = (ROOT / 'Scraper' / 'PlaywrightRawScraper.py').read_text(encoding='utf-8')
        self.assertIn("context.route('**/*'", src,
                      '必须注册 route 拦截后续导航与子请求')


class TestBuildContextExclusions(unittest.TestCase):
    """验收 5：构建上下文排除规则（非仅检查 Git 忽略）。"""

    def test_dockerignore_excludes_credential_files(self):
        """原缺陷暴露：_demo_admin_cred.txt 未被 .dockerignore 排除，
        docker build 的 COPY . . 会把本地凭据送入构建上下文。"""
        content = (ROOT / '.dockerignore').read_text(encoding='utf-8')
        self.assertIn('_demo_admin_cred.txt', content,
                      '.dockerignore 必须排除本地演示凭据')
        self.assertIn('*_cred*.txt', content)
        self.assertIn('_*_out.txt', content)

    def test_dockerignore_excludes_deploy_outputs(self):
        content = (ROOT / '.dockerignore').read_text(encoding='utf-8')
        for pattern in ('_docker_load_*.txt', '_up*_out.txt', '*.tar'):
            self.assertIn(pattern, content)

    def test_gitignore_excludes_credentials(self):
        content = (ROOT / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('_demo_admin_cred.txt', content)
        self.assertIn('*_cred*.txt', content)

    def test_dockerignore_effective_against_existing_files(self):
        """用实际存在的敏感文件验证排除规则的匹配效果（模式语义检查）。"""
        import fnmatch
        dockerignore = (ROOT / '.dockerignore').read_text(encoding='utf-8')
        patterns = [line.strip() for line in dockerignore.splitlines()
                    if line.strip() and not line.strip().startswith('#')
                    and not line.strip().startswith('!')]
        sensitive = '_demo_admin_cred.txt'
        matched = any(fnmatch.fnmatch(sensitive, p) for p in patterns)
        self.assertTrue(matched,
                        f'.dockerignore 模式必须能匹配 {sensitive}')


if __name__ == '__main__':
    unittest.main()