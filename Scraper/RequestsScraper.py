import zlib
import random
import requests
from urllib.parse import urljoin
from typing import Optional

from Scraper.ScraperBase import ScraperResult, ProxyConfig
from Tools.SSRFGuard import validate_url, SSRFError

# BeautifulSoup 仅在 check_content_quality 中使用，延迟到函数内导入，
# 使未安装 bs4 的精简环境也能使用核心抓取功能（及被测试导入）。

try:
    import brotli
except ImportError:
    brotli = None

try:
    import certifi
    # 使用 certifi 根证书做 TLS 校验；不可用时退回 requests 默认（True）
    DEFAULT_TLS_VERIFY = certifi.where()
except ImportError:
    DEFAULT_TLS_VERIFY = True

# 解压后内容大小上限（防 gzip/br 压缩炸弹撑爆内存）
MAX_DECOMPRESSED_SIZE = 64 * 1024 * 1024
# 原始响应体读取上限
MAX_RAW_SIZE = 64 * 1024 * 1024
# 手动跟随重定向的次数上限（每次跳转都会重新做 SSRF 校验）
MAX_REDIRECTS = 5

_REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)


class RequestsScraper:
    def __init__(self, proxies: Optional[dict] = None):
        self.session = requests.Session()
        self.session.proxies = proxies or {}
        self._init_headers()

    def _init_headers(self):
        """动态生成浏览器级请求头"""
        # 仅在 brotli 库可用时声明 br，避免服务端返回 br 压缩后无法解压
        accept_encoding = 'gzip, deflate, br' if brotli else 'gzip, deflate'
        self.headers = {
            'User-Agent': self._random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Encoding': accept_encoding,  # 明确声明支持的压缩格式
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Pragma': 'no-cache',
            'Referer': 'https://www.google.com/',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Upgrade-Insecure-Requests': '1'
        }

    def _random_user_agent(self):
        """生成随机现代浏览器UA"""
        chrome_versions = [
            (122, 0, 6261), (121, 0, 6167), (120, 0, 6099),
            (119, 0, 6045), (118, 0, 5993)
        ]
        version = random.choice(chrome_versions)
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version[0]}.0.{version[1]}.{version[2]} Safari/537.36"

    @staticmethod
    def _decompress_capped(content: bytes, encoding: str, max_size: int) -> bytes:
        """
        解压受信度未知的响应内容，带大小上限防压缩炸弹。
        解压结果超过 max_size 或解压失败时抛 ValueError。
        """
        if encoding == 'br':
            if not brotli:
                raise ValueError("服务端返回 br 压缩内容，但未安装 brotli 库")
            try:
                result = brotli.decompress(content)
            except Exception as e:
                raise ValueError(f"Brotli 解压失败: {e}")
        elif encoding == 'gzip':
            # 流式解压 + max_length 截断，避免一次性分配超大缓冲
            try:
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                result = d.decompress(content, max_size + 1)
                if d.unconsumed_tail:
                    raise ValueError(f"解压后内容超过上限({max_size} 字节)，疑似压缩炸弹")
                result += d.flush()
            except zlib.error as e:
                raise ValueError(f"Gzip 解压失败: {e}")
        elif encoding == 'deflate':
            try:
                d = zlib.decompressobj(-zlib.MAX_WBITS)
                result = d.decompress(content, max_size + 1)
                if d.unconsumed_tail:
                    raise ValueError(f"解压后内容超过上限({max_size} 字节)，疑似压缩炸弹")
                result += d.flush()
            except zlib.error:
                try:
                    # 回退到原始 deflate 格式
                    result = zlib.decompress(content)
                except Exception as e:
                    raise ValueError(f"Deflate 解压失败: {e}")
        else:
            raise ValueError(f"不支持的压缩格式: {encoding}")

        if len(result) > max_size:
            raise ValueError(f"解压后内容超过上限({max_size} 字节)，疑似压缩炸弹")
        return result

    def _decode_response(self, response: requests.Response, content: bytes) -> tuple:
        """
        统一手动处理所有解压逻辑。
        返回 (text, errors)；解压失败/超限时 text 为 '' 且 errors 说明原因。
        """
        errors = []
        content_encoding = response.headers.get('Content-Encoding', '').lower()
        encodings = [e.strip() for e in content_encoding.split(',') if e.strip()]

        # 根据RFC标准，解压顺序应与编码顺序相反
        for encoding in reversed(encodings):
            try:
                content = self._decompress_capped(content, encoding, MAX_DECOMPRESSED_SIZE)
            except ValueError as e:
                errors.append(str(e))
                return '', errors

        try:
            return content.decode('utf-8'), errors
        except UnicodeDecodeError:
            return content.decode('latin-1', errors='replace'), errors

    def set_proxies(self, proxies: dict):
        self.session.proxies = proxies

    def fetch(self, url, timeout=15) -> dict:
        """
        抓取页面内容。

        返回 dict:
            content:            页面文本；失败时为空字符串
            errors:             错误描述列表（失败时非空）
            status_code:        HTTP 状态码（未获得响应时为 None）
            permanently_gone:   True 表示资源永久消失（HTTP 404/410），调用方应视为终态
        """
        result = {'content': '', 'errors': [], 'status_code': None, 'permanently_gone': False}

        # SSRF 入口校验：拒绝内网/环回/保留段等目标
        try:
            validate_url(url)
        except SSRFError as e:
            result['errors'].append(str(e))
            return result

        # 手动跟随重定向（禁用 requests 自动跳转），每次跳转前校验 Location，
        # 防止 30x 把请求诱导到内网地址
        current_url = url
        response = None
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                response = self.session.get(
                    current_url,
                    headers=self.headers,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                    verify=DEFAULT_TLS_VERIFY
                )
            except requests.exceptions.RequestException as e:
                result['errors'].append(f"请求失败: {str(e)}")
                return result
            except Exception as e:
                result['errors'].append(f"意外错误: {str(e)}")
                return result

            result['status_code'] = response.status_code

            if response.status_code in _REDIRECT_STATUS_CODES:
                if redirect_count >= MAX_REDIRECTS:
                    result['errors'].append(f"重定向次数超过上限({MAX_REDIRECTS})")
                    response.close()
                    return result
                location = response.headers.get('Location')
                response.close()
                if not location:
                    result['errors'].append(f"HTTP {response.status_code} 重定向缺少 Location 头")
                    return result
                next_url = urljoin(current_url, location)
                try:
                    validate_url(next_url)
                except SSRFError as e:
                    result['errors'].append(f"重定向目标被 SSRF 校验拒绝: {e}")
                    return result
                current_url = next_url
                continue
            break

        try:
            status = response.status_code

            # 404/410：资源永久消失，标记终态，不值得重试
            if status in (404, 410):
                result['permanently_gone'] = True
                result['errors'].append(f"HTTP {status}: 资源已永久消失")
                return result

            # 其它 4xx/5xx：记录错误（瞬时故障可由上层重试）
            if status >= 400:
                result['errors'].append(f"HTTP 错误: {status}")
                return result

            # 强制禁用自动解压并读取原始内容（带原始大小上限）
            response.raw.decode_content = False
            raw_content = response.raw.read(MAX_RAW_SIZE + 1)
            if len(raw_content) > MAX_RAW_SIZE:
                result['errors'].append(f"原始响应体超过上限({MAX_RAW_SIZE} 字节)，已放弃")
                return result

            # 更新Referer头
            self.headers['Referer'] = url

            text, decode_errors = self._decode_response(response, raw_content)
            result['errors'].extend(decode_errors)
            result['content'] = text
            return result
        except Exception as e:
            result['errors'].append(f"意外错误: {str(e)}")
            return result
        finally:
            response.close()


def check_content_quality(html: str, format: str, target_keywords=None):
    """
    网页内容质量评估系统
    返回：tuple (is_valid, score, issues)
    """
    # 延迟导入：bs4 仅在本函数中使用，避免其成为模块级硬依赖
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, format)
    report = {'score': 100, 'issues': []}

    # 基础结构检测（网页6/7的完整性标准）
    if not soup.find('body') or not soup.find('html'):
        report['issues'].append('Missing essential HTML tags')
        report['score'] -= 40

    # 主要内容容器检测（网页3/7的架构建议）
    main_content = soup.find(['main', 'div#content', 'article', 'div.container'])
    if not main_content:
        report['issues'].append('Missing main content container')
        report['score'] -= 30

    # 数据密度分析（网页6的内容丰富性标准）
    text_length = len(soup.get_text(strip=True))
    tag_count = len(soup.find_all())
    if tag_count > 0:
        text_ratio = text_length / tag_count
        if text_ratio < 0.3:  # 文本/标签比阈值
            report['issues'].append('Low text density (possible ads/spam)')
            report['score'] -= 25

    # 反爬机制检测（网页3/5的异常识别）
    anti_scraping_phrases = [
        'enable javascript', 'access denied',
        'cloudflare security', 'captcha'
    ]
    page_text = soup.get_text().lower()
    if any(phrase in page_text for phrase in anti_scraping_phrases):
        report['issues'].append('Anti-scraping mechanism detected')
        report['score'] -= 50

    # 动态内容检测（网页1/3的SPA识别）
    if soup.find('noscript') or soup.find('div', class_='loading'):
        report['issues'].append('Dynamic content placeholders found')
        report['score'] -= 20

    # 关键词覆盖检测（网页6/7的相关性标准）
    if target_keywords:
        matched_keywords = sum(
            1 for kw in target_keywords if kw.lower() in page_text
        )
        coverage = matched_keywords / len(target_keywords)
        if coverage < 0.6:
            report['issues'].append(f'Low keyword coverage ({coverage:.0%})')
            report['score'] -= 15 * (1 - coverage)

    # 最终结果判定
    is_valid = (
            report['score'] >= 70
            and 'Anti-scraping mechanism detected' not in report['issues']
    )

    return is_valid, max(report['score'], 0), report['issues']


def fetch_content(
    url: str,
    timeout_ms: int,
    proxy: Optional[ProxyConfig] = None,
    format: str = 'xml',
    **kwargs
) -> ScraperResult:
    """
    The same as base.
    :param url: The same as base.
    :param timeout_ms: The same as base.
    :param proxy: Format: The same as base.
    :param format: Specify format to check.
    :return: The same as base.
    """

    scraper = RequestsScraper(proxy)
    fetch_result = scraper.fetch(url, int(timeout_ms / 1000))
    html_content = fetch_result['content']
    if html_content:
        is_valid, score, issues = check_content_quality(html_content, format)
        return {
            'content': html_content,
            'errors': issues,
            'valid': is_valid,
            'score': score,
        }
    else:
        # 抓取失败：透出错误信息与状态标记，供上层区分"真空页面"与"抓取故障"
        return {
            'content': '',
            'errors': fetch_result['errors'],
            'status_code': fetch_result.get('status_code'),
            'permanently_gone': fetch_result.get('permanently_gone', False),
        }


# ----------------------------------------------------------------------------------------------------------------------

def main():
    proxies = {
        "http": "socks5://user:password@proxy_host:port",
        "https": "socks5://user:password@proxy_host:port"
    }

    result = fetch_content('https://www.cbc.ca/news/world/israel-gaza-aid-military-1.7538495?cmp=rss', 30000)

    if result['content']:
        print(f'Content : {result["content"]}')
        print('')
        print(f'Valid : {result["valid"]}')
        print(f'Score : {result["score"]}')
    print(f'Errors : {result["errors"]}')

if __name__ == "__main__":
    main()
