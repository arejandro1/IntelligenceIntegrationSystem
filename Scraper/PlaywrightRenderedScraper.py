import traceback
from typing import Optional
from Scraper.ScraperBase import ScraperResult, ProxyConfig
from Scraper.PlaywrightRawScraper import request_by_browser


def fetch_content(
    url: str,
    timeout_ms: int,
    proxy: Optional[ProxyConfig] = None,
    **kwargs
) -> ScraperResult:
    """
    The same as base.
    :param url: The same as base.
    :param timeout_ms: The same as base.
    :param proxy: Format: The same as base.
    :param kwargs: 透传到 request_by_browser（如 headless/locale/timezone_id/
        ignore_https_errors/allow_private_network）。
        批次五：ignore_https_errors 默认 False（启用 HTTPS 证书校验）；
        后续导航/重定向/子请求的目标地址由 BrowserRequestGuard 拦截校验
        （见 request_by_browser），本渲染入口自动获得同等防护。
    :return: The same as base.
    """

    def handler(page, response):
        if not response:
            return {'content': '', "errors": ['No response']}

        # 404/410 标记永久消失，供上层视为终态 skip
        if response.status in (404, 410):
            return {'content': '', "errors": [f'HTTP {response.status}: 资源已永久消失'],
                    'status_code': response.status, 'permanently_gone': True}

        if response.status >= 400:
            return {'content': '', "errors": [f'HTTP response: {response.status}'],
                    'status_code': response.status}

        error_msgs = []

        try:
            page.wait_for_load_state('domcontentloaded', timeout=timeout_ms)
        except Exception as e:
            err_str = f"Wait for load state failed: {str(e)}"
            print(err_str)
            error_msgs.append(err_str)

        try:
            page_content = page.content()
        except Exception as e:
            err_str = f"Failed to get page content: {str(e)}"
            print(err_str)
            error_msgs.append(err_str)
            return {'content': '', "errors": error_msgs}

        return {'content': page_content, "errors": error_msgs}

    try:
        result = request_by_browser(url, handler, timeout_ms, proxy, **kwargs)
        return result
    except Exception as e:
        print(traceback.format_exc())
        return {'content': '', "errors": [str(e)]}


# ----------------------------------------------------------------------------------------------------------------------

def main():
    result = fetch_content("https://machinelearningmastery.com/further-applications-with-context-vectors/",
                           timeout_ms=20000)
    html = result['content']
    if html:
        with open('../web.html', 'wt', encoding='utf-8') as f:
            f.write(html)


# Usage example
if __name__ == "__main__":
    main()
