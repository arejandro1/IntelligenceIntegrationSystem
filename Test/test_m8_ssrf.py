# -*- coding: utf-8 -*-
"""M8: SSRF 防护测试——拒绝内网/私有IP/localhost"""
import unittest
import socket

BANNED_HOSTS = ['127.0.0.1', '::1', '0.0.0.0', 'localhost']
PRIVATE_RANGES = [
    ('10.0.0.0', '10.255.255.255'),
    ('172.16.0.0', '172.31.255.255'),
    ('192.168.0.0', '192.168.255.255'),
    ('169.254.0.0', '169.254.255.255'),
]


def is_private_ip(host: str) -> bool:
    """判断主机名/IP是否属于内网或本地"""
    host_lower = host.lower()
    try:
        from ipaddress import ip_address, ip_network
        addr = ip_address(host)
        if addr.is_loopback or addr.is_private or addr.is_link_local:
            return True
        return False
    except ValueError:
        # 不是 IP 地址，检查 localhost 等
        pass
    if host_lower in ('localhost', '0.0.0.0'):
        return True
    # 尝试 DNS 解析后再次校验
    try:
        resolved = socket.gethostbyname(host)
        from ipaddress import ip_address as ipa
        a = ipa(resolved)
        return a.is_loopback or a.is_private or a.is_link_local
    except Exception:
        pass
    return False


def ssrf_validate(url: str) -> bool:
    """SSRF 校验：提取主机名并判断"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    host = parsed.hostname or ''
    if not host:
        return False
    return not is_private_ip(host)


class TestSSRF(unittest.TestCase):
    def test_reject_localhost(self):
        self.assertFalse(ssrf_validate('http://localhost:8080/api'))

    def test_reject_127(self):
        self.assertFalse(ssrf_validate('http://127.0.0.1:5000/test'))

    def test_reject_192_168(self):
        self.assertFalse(ssrf_validate('https://192.168.1.1/admin'))

    def test_reject_10_net(self):
        self.assertFalse(ssrf_validate('http://10.0.0.1:27017'))

    def test_reject_0_0_0_0(self):
        self.assertFalse(ssrf_validate('http://0.0.0.0:80'))

    def test_accept_public(self):
        self.assertTrue(ssrf_validate('https://www.example.com/page'))

    def test_reject_file_protocol(self):
        from urllib.parse import urlparse
        self.assertNotIn(urlparse('file:///etc/passwd').scheme, ('http', 'https'))


if __name__ == '__main__':
    unittest.main()