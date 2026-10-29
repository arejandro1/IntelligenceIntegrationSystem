# -*- coding: utf-8 -*-
"""
SSRF 防护：校验抓取目标 URL，防止爬虫被诱导访问内网/本地资源。

规则：
- 仅允许 http/https scheme；
- hostname 经 DNS 解析后逐个校验所有 IP，拒绝环回/私网/链路本地/
  保留段/组播/未指定地址（0.0.0.0、::）等；
- 兼容十进制/八进制/十六进制 IPv4 写法（如 2130706433、0x7f000001、127.1），
  先用 socket.inet_aton 规范化再判定，防止写法混淆绕过；
- 覆盖 IPv6 等价地址（::1、fc00::/7、fe80::/10、IPv4-mapped 如 ::ffff:127.0.0.1）。

注意：校验发生在 DNS 解析时刻，理论上存在 TOCTOU 窗口（DNS rebinding）。
对安全要求更高的场景，应在连接建立层再次校验对端实际 IP。
"""

import socket
import ipaddress
from urllib.parse import urlparse


class SSRFError(ValueError):
    """URL 未通过 SSRF 安全校验。"""


def _normalize_ip(host: str):
    """
    把各种写法的 IP 字符串规范化为 ipaddress 对象。
    支持标准 IPv4/IPv6、去 [] 与 zone id，以及十进制/八进制/十六进制 IPv4 写法。
    不是 IP 写法（普通域名）时返回 None。
    """
    host = (host or '').strip().strip('[]')
    if not host:
        return None
    # 去掉 IPv6 zone id（如 fe80::1%eth0）
    if '%' in host:
        host = host.split('%', 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        # 判定 host 是否为 IP 的探测式解析：失败说明不是标准写法，
        # 落入下方 inet_aton 兼容分支（控制流，非错误）
        pass
    # 兼容十进制/八进制/十六进制 IPv4 写法（inet_aton 接受 "127.1"、"0x7f000001"、纯数字等）
    try:
        packed = socket.inet_aton(host)
        return ipaddress.ip_address(packed)
    except (OSError, ValueError):
        return None


def _is_forbidden_ip(ip) -> bool:
    """判断单个 IP 是否属于禁止抓取的地址段。"""
    # IPv4-mapped IPv6（如 ::ffff:127.0.0.1）按映射后的 IPv4 再判定一次，防止绕过
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None and _is_forbidden_ip(mapped):
        return True
    return (
        ip.is_loopback or      # 127.0.0.0/8、::1
        ip.is_private or       # 10/8、172.16/12、192.168/16、fc00::/7 等
        ip.is_link_local or    # 169.254/16、fe80::/10
        ip.is_reserved or      # 保留段
        ip.is_multicast or     # 组播
        ip.is_unspecified      # 0.0.0.0、::
    )


def validate_url(url: str, allow_private: bool = False) -> str:
    """
    校验 URL 是否允许抓取。

    :param url: 待校验的目标 URL。
    :param allow_private: True 时放行内网/本地地址（仅做 scheme 与基本格式校验）。
    :return: 校验通过时原样返回 url。
    :raises SSRFError: 校验未通过（scheme 非法、无法解析、解析到受禁地址等）。
    """
    parsed = urlparse(url)

    if parsed.scheme not in ('http', 'https'):
        raise SSRFError(f'仅允许 http/https scheme，拒绝: {url!r}')

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError(f'URL 缺少主机名，拒绝: {url!r}')

    if allow_private:
        return url

    # 主机名本身直接就是 IP 写法（含十进制/十六进制混淆写法）时无需 DNS 解析
    direct_ip = _normalize_ip(hostname)
    if direct_ip is not None:
        ip_list = [direct_ip]
    else:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        try:
            infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except Exception as e:
            raise SSRFError(f'主机名解析失败，拒绝抓取: {hostname} ({e})')
        ip_list = []
        for info in infos:
            ip = _normalize_ip(info[4][0])
            if ip is not None:
                ip_list.append(ip)
        if not ip_list:
            raise SSRFError(f'主机名无有效解析结果，拒绝抓取: {hostname}')

    for ip in ip_list:
        if _is_forbidden_ip(ip):
            raise SSRFError(f'目标 {hostname} 解析到受禁地址 {ip}，拒绝抓取: {url!r}')

    return url


class BrowserRequestGuard:
    """批次五：Playwright 浏览器请求级 SSRF 防护。

    初始 URL 经 validate_url 校验后，页面后续仍可能被诱导访问受限地址
    （meta refresh / JS 导航 / 重定向 / 页面子请求）。本防护通过
    context.route('**/*', handler) 拦截浏览器发出的每一个请求：

    - 导航类请求（document / is_navigation_request）：每次完整校验
      （含 DNS 解析）——同 host 的 DNS 记录变化（rebinding）在下次
      导航时会被再次校验发现；
    - 子资源请求：同 host 结果缓存（首次完整校验，后续放行），
      避免每个静态资源都做 DNS 解析拖慢页面加载；
    - 非网络协议（data:/blob:/about:）放行。

    已知限制（说明）：域名校验通过后、实际建立连接前，若该域名的
    DNS 记录被攻击者切换到内网地址（DNS rebinding TOCTOU），本层
    无法感知——Playwright/CDN 不暴露连接层回调。缓解措施：
    1) 导航请求每次重新完整校验（缩小窗口）；
    2) 生产部署应在网络层限制浏览器容器出网范围（docker-compose
       已将 crawler 容器置于内部网络，仅允许经代理出网）。
    """

    def __init__(self, allow_private: bool = False) -> None:
        self._allow_private = allow_private
        self._verified_hosts: set = set()
        self._lock = __import__('threading').Lock()
        self.blocked_count: int = 0
        self.checked_count: int = 0

    @staticmethod
    def _is_non_network_scheme(url: str) -> bool:
        """data:/blob:/about: 等非网络协议不产生外发连接，放行。"""
        try:
            from urllib.parse import urlparse
            return urlparse(url).scheme in ('data', 'blob', 'about')
        except Exception:
            return False

    def check_request_url(self, url: str, is_document: bool) -> bool:
        """校验单个请求 URL。返回 True=放行，False=阻断。"""
        if self._allow_private:
            return True
        if self._is_non_network_scheme(url):
            return True
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ''
        except Exception:
            return False
        if not host:
            return False

        with self._lock:
            host_verified = host in self._verified_hosts
        # 导航请求每次完整校验（防 rebinding/重定向绕过）；子资源按 host 缓存
        if is_document or not host_verified:
            self.checked_count += 1
            try:
                validate_url(url, allow_private=False)
            except SSRFError:
                with self._lock:
                    self._verified_hosts.discard(host)
                return False
            with self._lock:
                self._verified_hosts.add(host)
        return True

    def make_route_handler(self):
        """构造 Playwright route handler（duck-typing，可注入 fake 测试）。"""

        def handler(route) -> None:
            request = getattr(route, 'request', None)
            if request is None:
                route.abort()
                return
            url = getattr(request, 'url', '') or ''
            is_document = False
            try:
                resource_type = getattr(request, 'resource_type', '')
                is_document = resource_type == 'document'
                if not is_document:
                    is_navigation = getattr(request, 'is_navigation_request', None)
                    if callable(is_navigation):
                        is_document = bool(is_navigation())
            except Exception:
                is_document = False
            if self.check_request_url(url, is_document):
                route.continue_()
            else:
                self.blocked_count += 1
                route.abort()

        return handler
