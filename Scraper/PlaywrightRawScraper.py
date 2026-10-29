"""
RSS Fetcher with Headless Browser & Proxy Support

Complete implementation with:
- Playwright headless browser (module-level shared instance)
- HTTP/SOCKS5 proxy support
- Random user agents
- Comprehensive error handling
- Type annotations
"""
import atexit
import logging
import random
import threading
import traceback
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List, Tuple
from playwright.sync_api import sync_playwright

from IntelligenceCrawler.BrowserMonitor import AutoTrackedBrowser
from Scraper.ScraperBase import ScraperResult, ProxyConfig
from Tools.ProxyFormatParser import to_playwright_format, parse_to_intermediate
from Tools.SSRFGuard import SSRFError, BrowserRequestGuard, validate_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 8000  # 8 seconds


DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


class BrowserManager:
    """Manage browser instance with proxy and anti-detection settings"""

    ANTI_DETECTION_ARGS = [
        '--disable-blink-features=AutomationControlled',
        '--disable-automation-software-banner',  # 隐藏自动化提示
        '--disable-dev-shm-usage',
        '--no-sandbox',
        '--enable-features=NetworkService,NetworkServiceInProcess',
        '--disable-site-isolation-for-enterprise-policy'
    ]

    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[Dict[str, str]] = None,
        user_agents: Optional[List[str]] = None
    ):
        self.playwright = None
        self.browser = None
        self.headless = headless

        if proxy:
            proxy = to_playwright_format(parse_to_intermediate(proxy))
        self.proxy = proxy

    def __enter__(self):
        try:
            self.open()
            return self.browser
        except Exception as e:
            self.close()
            raise RuntimeError(f"Browser launch fail: {str(e)}")

    def __exit__(self, *args):
        self.close()

    def open(self):
        if not self.playwright:
            self.playwright = sync_playwright().start()
            try:
                # 尊重构造时传入的 headless 参数，不再硬编码
                options = self._prepare_launch_options(headless=self.headless)
                real_browser = self.playwright.chromium.launch(**options)
                self.browser: "Browser" = AutoTrackedBrowser(real_browser)
            except Exception as e:
                self.playwright.stop()
                self.playwright = None
                raise

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception as e:
                print(f"Browser close exception: {e}")
            finally:
                self.browser = None
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception as e:
                print(f"Playwright close exception: {e}")
            finally:
                self.playwright = None

    def _prepare_launch_options(self, headless: bool) -> Dict[str, Any]:
        options = {
            "args": BrowserManager.ANTI_DETECTION_ARGS + [
                f'--window-size={random.randint(1000, 1400)},{random.randint(800, 1200)}',  # 随机窗口尺寸
                f'--lang=en-US',
            ],
            "headless": headless,
            # 移除 slow_mo 随机减速：该参数面向拟人化调试，生产抓取只会拖慢每篇文章
        }

        if self.proxy:
            self._validate_proxy_config()
            options["proxy"] = {
                "server": self.proxy["server"],
                "username": self.proxy.get("username"),
                "password": self.proxy.get("password")
            }

        return options

    def _validate_proxy_config(self):
        try:
            parsed = urlparse(self.proxy["server"])
            if not all([parsed.scheme, parsed.hostname, parsed.port]):
                raise ValueError
            if parsed.scheme not in ['http', 'https', 'socks5']:
                raise ValueError
        except:
            raise ValueError(f"Invalid proxy format: {self.proxy['server']}")


# ----------------------------------------------------------------------------------------------------------------------
# 模块级共享浏览器池：
# 原实现每抓一篇文章都全新 launch 一个 Chromium 实例，开销巨大（进程启动 + 初始化约秒级）。
# 现改为懒加载共享浏览器，atexit 统一关闭；
# 每次请求仍新建独立 BrowserContext，保证会话/指纹隔离。
# 注意1：Playwright 同步 API 绑定创建线程，共享键包含线程标识，避免跨线程使用崩溃；
#       即"每个工作线程一个共享浏览器"，同一爬虫线程内不再每篇重启 Chromium。
# 注意2：代理在浏览器启动时固化，不同代理配置会各持一个浏览器实例（数量有界）。
# ----------------------------------------------------------------------------------------------------------------------

_shared_browsers: Dict[Tuple, BrowserManager] = {}
_shared_browsers_lock = threading.Lock()


def _proxy_signature(proxy: Optional[Dict[str, str]]) -> Optional[Tuple]:
    """把代理配置转成可哈希签名，用于共享浏览器的键。"""
    if not proxy:
        return None
    return tuple(sorted((str(k), str(v)) for k, v in proxy.items()))


def _get_shared_browser(headless: bool, proxy: Optional[Dict[str, str]]):
    """懒加载获取（或创建）共享浏览器实例，线程安全。"""
    key = (threading.get_ident(), bool(headless), _proxy_signature(proxy))
    with _shared_browsers_lock:
        manager = _shared_browsers.get(key)
        if manager and manager.browser:
            try:
                # 浏览器被外部关闭/崩溃时丢弃旧实例，重新拉起
                if manager.browser.is_connected():
                    return manager.browser
            except Exception as e:
                # is_connected() 抛异常即视为连接已失效，走下方重建路径
                logger.debug(f"Browser is_connected() probe failed, rebuilding: {e}")
            manager.close()
            _shared_browsers.pop(key, None)
        manager = BrowserManager(headless=headless, proxy=proxy)
        manager.open()
        _shared_browsers[key] = manager
        return manager.browser


@atexit.register
def _close_shared_browsers():
    """进程退出时统一关闭共享浏览器池。"""
    with _shared_browsers_lock:
        for manager in _shared_browsers.values():
            try:
                manager.close()
            except Exception as e:
                # atexit 收尾阶段单个浏览器关闭失败不应阻断其余清理
                logger.debug(f"Close shared browser during atexit failed: {e}")
        _shared_browsers.clear()


def close_shared_browsers() -> int:
    """公开的显式释放入口：关闭当前全部共享浏览器，返回关闭数量。

    供爬虫任务在 module_fini / 停止时调用，避免常驻 Chromium 生命周期
    只依赖 atexit（热重载场景下旧模块卸载时浏览器会滞留）。
    注意：Playwright 同步 API 绑定创建线程，跨线程调用 close 可能失败；
    此处尽力而为逐个关闭并吞掉异常，最终兜底仍由 atexit + BrowserTracker
    强制清理保证。
    """
    closed = 0
    with _shared_browsers_lock:
        managers = list(_shared_browsers.values())
        _shared_browsers.clear()
    for manager in managers:
        try:
            manager.close()
            closed += 1
        except Exception as e:
            # 单个浏览器关闭失败跳过，继续关闭其余（返回已关闭计数）
            logger.debug(f"Close shared browser failed: {e}")
    return closed


def request_by_browser(
    url: str,
    handler: callable,
    timeout: int = DEFAULT_TIMEOUT_MS,
    proxy: Optional[Dict[str, str]] = None,
    headless: bool = True,
    locale: Optional[str] = None,
    timezone_id: Optional[str] = None,
    ignore_https_errors: bool = False,
    allow_private_network: bool = False,
    **kwargs
):
    """
    Request by headless browser. Handle request result by callable.
    :param url: The request url.
    :param handler: 处理 (page, response) 的回调，返回结果 dict。
    :param timeout: 页面导航超时（毫秒）。
    :param proxy: 代理配置（首次启动浏览器时固化）。
    :param headless: 是否无头模式（默认 True）。
    :param locale: 浏览器 locale；默认 None 表示不设置，使用浏览器默认值。
    :param timezone_id: 浏览器时区；默认 None 表示不设置，使用浏览器默认值。
    :param ignore_https_errors: 批次五修复——默认 False（启用 HTTPS 证书校验）。
        仅在确需抓取自签名证书站点时由调用方显式传 True。
    :param allow_private_network: 允许访问内网/环回地址（默认 False，
        仅本地调试场景使用；生产保持 False）。
    :return: handler 的返回结果，或 {'content': '', 'errors': [...]}。
    """
    # SSRF 校验：goto 前校验初始 URL。
    try:
        validate_url(url, allow_private=allow_private_network)
    except SSRFError as e:
        return {'content': '', 'errors': [str(e)]}

    context_args = {
        "user_agent": random.choice(DEFAULT_USER_AGENTS),
        "viewport": {"width": 1366, "height": 768},
        "java_script_enabled": True,
        # 批次五修复：默认启用 HTTPS 证书校验（原 True 会放过中间人攻击
        # 与自签名证书劫持，属于传输层安全弱点）
        "ignore_https_errors": bool(ignore_https_errors),
        "extra_http_headers": {
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",  # 指定内容类型
        }
    }
    # locale/timezone 仅在显式指定时传入，默认使用浏览器自身设置
    if locale:
        context_args["locale"] = locale
    if timezone_id:
        context_args["timezone_id"] = timezone_id

    try:
        browser = _get_shared_browser(headless=headless, proxy=proxy)
    except Exception as e:
        return {'content': '', 'errors': [f"Browser launch fail: {str(e)}"]}

    context = browser.new_context(**context_args)
    # 批次五修复：请求级 SSRF 防护——拦截浏览器后续导航、重定向与
    # 页面子请求的目标地址（初始 URL 合法但页面被诱导访问内网/环回/
    # 链路本地地址时阻断）。详见 Tools/SSRFGuard.BrowserRequestGuard。
    request_guard = BrowserRequestGuard(allow_private=allow_private_network)
    try:
        context.route('**/*', request_guard.make_route_handler())
    except Exception as route_err:
        # route 注册失败时保守处理：拒绝本次抓取（fail-closed），
        # 避免在无后续请求防护的状态下浏览不可信页面
        try:
            context.close()
        except Exception as e:
            # 兜底清理失败无需处理：外层即将重建浏览器实例
            logger.debug(f"Close context after route guard failure failed: {e}")
        return {'content': '', 'errors': [f'Browser route guard setup fail: {route_err}']}
    try:
        page = context.new_page()
        try:
            response = page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            result = handler(page, response)
            # 防护命中时在结果中附注（不改变 handler 结果语义，便于监控）
            if request_guard.blocked_count and isinstance(result, dict):
                result.setdefault('ssrf_blocked_count', request_guard.blocked_count)
            return result
        except Exception as e:
            # 抓取异常透出到 errors，不再静默吞掉
            print(f'request_by_browser gets exception: {str(e)}')
            return {'content': '', 'errors': [str(e)]}
        finally:
            try:
                page.close()
            except Exception as e:
                # finally 兜底清理失败不掩盖主流程已透出的错误
                logger.debug(f"Close page in finally failed: {e}")
    finally:
        try:
            context.close()
        except Exception as e:
            # finally 兜底清理失败不掩盖主流程已透出的错误
            logger.debug(f"Close context in finally failed: {e}")


def fetch_content(
    url: str,
    timeout_ms: Optional[int] = DEFAULT_TIMEOUT_MS,
    proxy: Optional[ProxyConfig] = None,
    **kwargs
) -> ScraperResult:
    """
    The same as base.
    :param url: The same as base.
    :param timeout_ms: The same as base.
    :param proxy: Format: The same as base.
    :param kwargs: 透传到 request_by_browser（如 headless/locale/timezone_id）。
    :return: The same as base.
    """
    try:
        def handler(_, response):
            if not response:
                return {'content': '', 'errors': ['No response']}
            # 404/410 标记永久消失，供上层视为终态 skip
            if response.status in (404, 410):
                return {'content': '', 'errors': [f'HTTP {response.status}: 资源已永久消失'],
                        'status_code': response.status, 'permanently_gone': True}
            if response.status >= 400:
                return {'content': '', 'errors': [f'HTTP response: {response.status}'],
                        'status_code': response.status}
            raw_content = response.text()
            return {'content': raw_content, 'errors': []}

        result = request_by_browser(url, handler, timeout_ms, proxy, **kwargs)
        return result
    except Exception as e:
        traceback.print_exc()
        return {'content': '', 'errors': [str(e)]}


# ----------------------------------------------------------------------------------------------------------------------

def main():
    # SOCKS5 Proxy Example
    proxy_config = {
        "server": "socks5://127.0.0.1:10808",
        "username": "",
        "password": ""
    }

    result = fetch_content(
        "https://blogs.technet.microsoft.com/machinelearning/feed",
        proxy=None
    )

    print(result['content'])
    print(result['errors'])

if __name__ == "__main__":
    main()
