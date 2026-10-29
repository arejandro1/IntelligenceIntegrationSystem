"""
All scraper MUST provide the same interface and the same result format.
"""
from urllib.parse import quote, unquote
from typing import Optional, TypedDict, List


class ProxyConfig(TypedDict):
    http: str
    https: str


class ScraperResult(TypedDict):
    content: str
    errors: List[str]


# ScraperResult 的可选扩展字段（各实现可按需附加）：
#   status_code: int        —— HTTP 响应状态码（如有）
#   permanently_gone: bool  —— True 表示资源已永久消失（HTTP 404/410），
#                              调用方应将其视为终态（skip），不再重试
# 约定：抓取失败时 content 为空字符串，errors 必须非空并说明原因；
#       这样上层流程才能区分"真空页面"（skip）与"抓取故障"（可重试）。


def fetch_content(
    url: str,
    timeout_ms: int,
    proxy: Optional[ProxyConfig] = None,
    **kwargs
) -> ScraperResult:
    """
    Fetch web content from the specified url.
    :param url: The url to fetch.
    :param timeout_ms: The timeout in ms.
    :param proxy: The proxy setting. In requests format.
        {
            "http": "socks5://user:password@proxy_host:port",
            "https": "socks5://user:password@proxy_host:port"
        }
    :return:
        {
            'content': 'web content',           # The content in str. If it's an empty string, that means scrap fail.
            'errors': ['error description'],    # The error list of str
            'other optional fields': Any        # Any other extra fields that depends on scraper itself.
        }
    """
    return {
        'content': '',
        'errors': ['This is just an example of scraper implementation,']
    }
