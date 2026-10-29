# -*- coding: utf-8 -*-
"""M8: SSRF 防护测试——直接测试生产实现 Tools.SSRFGuard.validate_url。

覆盖：环回/私网/链路本地/未指定地址、IPv6 等价地址、十进制/十六进制/八进制
混淆 IP 写法、scheme 白名单，以及 RequestsScraper 抓取时的重定向 SSRF 拦截
（网络层全部 mock，不发起真实请求）。
"""
import unittest
from unittest import mock

from Tools.SSRFGuard import validate_url, SSRFError


def _getaddrinfo_result(*ips):
    """构造 getaddrinfo 返回值元组列表。"""
    import socket
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in ips]


class TestSSRFGuardReject(unittest.TestCase):
    """内网/本地/保留地址必须被拒绝。"""

    def assert_reject(self, url):
        with self.assertRaises(SSRFError, msg=f'应拒绝: {url}'):
            validate_url(url)

    def test_reject_loopback_127(self):
        self.assert_reject('http://127.0.0.1:5000/test')
        self.assert_reject('http://127.1/')          # 省略写法

    def test_reject_private_10(self):
        self.assert_reject('http://10.0.0.1:27017')

    def test_reject_private_172_16(self):
        self.assert_reject('http://172.16.3.4/')
        self.assert_reject('http://172.31.255.254/')

    def test_reject_private_192_168(self):
        self.assert_reject('https://192.168.1.1/admin')

    def test_reject_unspecified_0_0_0_0(self):
        self.assert_reject('http://0.0.0.0:80')

    def test_reject_link_local_169_254(self):
        self.assert_reject('http://169.254.169.254/latest/meta-data')

    def test_reject_ipv6_loopback(self):
        self.assert_reject('http://[::1]/')

    def test_reject_ipv6_unique_local(self):
        self.assert_reject('http://[fd00::1]/')

    def test_reject_ipv6_link_local(self):
        self.assert_reject('http://[fe80::1]/')

    def test_reject_ipv4_mapped_ipv6(self):
        self.assert_reject('http://[::ffff:127.0.0.1]/')

    def test_reject_decimal_ip(self):
        # 2130706433 == 0x7F000001 == 127.0.0.1
        self.assert_reject('http://2130706433/')

    def test_reject_hex_ip(self):
        self.assert_reject('http://0x7f000001/')

    def test_reject_octal_ip(self):
        self.assert_reject('http://0177.0.0.1/')

    def test_reject_non_http_scheme(self):
        self.assert_reject('file:///etc/passwd')
        self.assert_reject('ftp://example.com/x')
        self.assert_reject('gopher://example.com/')

    def test_reject_empty_host(self):
        self.assert_reject('http:///path-only')

    def test_reject_unresolvable_host(self):
        with mock.patch('socket.getaddrinfo', side_effect=OSError('DNS fail')):
            self.assert_reject('http://no-such-host.invalid/')


class TestSSRFGuardAccept(unittest.TestCase):
    """公网地址（mock DNS 解析）必须放行。"""

    def test_accept_public_domain(self):
        with mock.patch('socket.getaddrinfo', return_value=_getaddrinfo_result('93.184.216.34')):
            self.assertEqual(validate_url('http://example.com/news'), 'http://example.com/news')

    def test_accept_public_ip(self):
        self.assertEqual(validate_url('http://93.184.216.34/'), 'http://93.184.216.34/')

    def test_accept_public_domain_multi_ip(self):
        # 多 A 记录全部为公网时放行
        with mock.patch('socket.getaddrinfo',
                        return_value=_getaddrinfo_result('93.184.216.34', '93.184.216.35')):
            self.assertEqual(validate_url('https://example.com/'), 'https://example.com/')

    def test_reject_domain_resolving_to_private(self):
        # 域名解析到内网地址（DNS rebinding 形态）必须拒绝
        with mock.patch('socket.getaddrinfo', return_value=_getaddrinfo_result('192.168.0.1')):
            with self.assertRaises(SSRFError):
                validate_url('http://internal.example.com/')

    def test_reject_domain_with_mixed_ips(self):
        # 只要有一个解析结果为内网即拒绝
        with mock.patch('socket.getaddrinfo',
                        return_value=_getaddrinfo_result('93.184.216.34', '127.0.0.1')):
            with self.assertRaises(SSRFError):
                validate_url('http://example.com/')

    def test_allow_private_override(self):
        # allow_private=True 时放行内网（用于明确信任的内网目标）
        self.assertEqual(validate_url('http://127.0.0.1:8001/', allow_private=True),
                         'http://127.0.0.1:8001/')
        # scheme 白名单依然生效
        with self.assertRaises(SSRFError):
            validate_url('file:///etc/passwd', allow_private=True)


class _FakeResponse:
    """最小 requests.Response 替身（供重定向场景 mock）。"""

    def __init__(self, status_code, headers=None, raw_bytes=b''):
        self.status_code = status_code
        self.headers = headers or {}
        self._raw_bytes = raw_bytes
        self.closed = False

    class _Raw:
        decode_content = False

        def __init__(self, data):
            self._data = data

        def read(self, *args, **kwargs):
            return self._data

    @property
    def raw(self):
        return self._Raw(self._raw_bytes)

    def close(self):
        self.closed = True


class TestSSRFRedirect(unittest.TestCase):
    """抓取入口 + 手动重定向跟随的 SSRF 拦截（mock 网络）。"""

    def setUp(self):
        # RequestsScraper 依赖 requests（当前环境可用）；DNS 统一 mock 成公网地址
        from Scraper.RequestsScraper import RequestsScraper
        self.scraper = RequestsScraper()
        patcher = mock.patch('socket.getaddrinfo', return_value=_getaddrinfo_result('93.184.216.34'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fetch_rejects_internal_entry_url(self):
        result = self.scraper.fetch('http://127.0.0.1:8080/admin')
        self.assertEqual(result['content'], '')
        self.assertTrue(result['errors'])

    def test_fetch_blocks_redirect_to_internal(self):
        # 初始 URL 合法（公网），302 跳转到内网：必须拦截且不再跟随
        redirect = _FakeResponse(302, headers={'Location': 'http://127.0.0.1/admin'})
        with mock.patch.object(self.scraper.session, 'get', return_value=redirect) as get_mock:
            result = self.scraper.fetch('http://example.com/start')
        self.assertEqual(result['content'], '')
        self.assertTrue(any('SSRF' in e or '重定向' in e for e in result['errors']))
        self.assertEqual(get_mock.call_count, 1)   # 未跟随跳转

    def test_fetch_follows_public_redirect(self):
        redirect = _FakeResponse(302, headers={'Location': 'http://example.com/final'})
        final = _FakeResponse(200, headers={}, raw_bytes='<html>ok</html>'.encode('utf-8'))
        with mock.patch.object(self.scraper.session, 'get', side_effect=[redirect, final]) as get_mock:
            result = self.scraper.fetch('http://example.com/start')
        self.assertEqual(result['content'], '<html>ok</html>')
        self.assertEqual(get_mock.call_count, 2)

    def test_fetch_redirect_loop_capped(self):
        # 无限自跳转：超过上限（5 次）放弃并记错误
        redirect = _FakeResponse(302, headers={'Location': 'http://example.com/loop'})
        with mock.patch.object(self.scraper.session, 'get', return_value=redirect) as get_mock:
            result = self.scraper.fetch('http://example.com/loop')
        self.assertEqual(result['content'], '')
        self.assertTrue(any('重定向' in e for e in result['errors']))
        self.assertLessEqual(get_mock.call_count, 6)


if __name__ == '__main__':
    unittest.main()
