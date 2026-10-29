# -*- coding: utf-8 -*-
"""爬虫抓取链路修复回归测试（review 批次）。

覆盖：
1. fetch 错误透出：503 → 可重试错误路径，404 → 终态 skip 路径（网络层 mock）；
2. CrawlPipeline：ExtractionResult 空内容/提取失败不触发 content_handler，
   单篇 handler 异常不中断整轮；
3. SSRFGuard：内网地址拒绝、公网域名（mock getaddrinfo）放行；
4. IntelligenceCrawlFlow：pub_time 提取不到时为 None，不伪造当前时间。

说明：部分被测模块依赖 bs4/feedparser/tldextract 等重依赖，本文件对其做导入守卫，
在精简环境下相应用例自动跳过，不影响整体测试。
"""
import unittest
from unittest import mock

from Tools.SSRFGuard import validate_url, SSRFError
from Tools.ProcessCotrolException import ProcessProblem, ProcessSkip

# ---------------------------------------------------------------------------
# 重依赖模块导入守卫
# ---------------------------------------------------------------------------

try:
    from Workflow.RssFeedsBasedCrawlFlow import fetch_process_article
    from Tools.RSSFetcher import RssItem
    HAS_RSS_FLOW = True
except Exception:
    fetch_process_article = None
    RssItem = None
    HAS_RSS_FLOW = False

try:
    from IntelligenceCrawler.CrawlPipeline import CrawlPipeline
    from IntelligenceCrawler.Extractor import ExtractionResult
    HAS_PIPELINE = True
except Exception:
    CrawlPipeline = None
    ExtractionResult = None
    HAS_PIPELINE = False

try:
    from Workflow.IntelligenceCrawlFlow import intelligence_crawler_result_handler
    HAS_IC_FLOW = True
except Exception:
    intelligence_crawler_result_handler = None
    HAS_IC_FLOW = False


# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------

class _FakeResponse:
    """最小 requests.Response 替身。"""

    class _Raw:
        decode_content = False

        def __init__(self, data):
            self._data = data

        def read(self, *args, **kwargs):
            return self._data

    def __init__(self, status_code, headers=None, raw_bytes=b''):
        self.status_code = status_code
        self.headers = headers or {}
        self._raw_bytes = raw_bytes

    @property
    def raw(self):
        return self._Raw(self._raw_bytes)

    def close(self):
        pass


def _public_dns(*ips):
    import socket
    default = ips or ('93.184.216.34',)
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in default]


def _make_article(link='http://example.com/a1'):
    return RssItem(
        title='t', link=link, published=None, authors=[],
        description='', guid='', categories=[], media=None
    )


# ---------------------------------------------------------------------------
# 1. fetch 错误透出
# ---------------------------------------------------------------------------

class TestFetchErrorSurfacing(unittest.TestCase):
    """RequestsScraper.fetch 必须透出 HTTP 错误，不再吞异常返回 None。"""

    def setUp(self):
        from Scraper.RequestsScraper import RequestsScraper
        self.scraper = RequestsScraper()
        patcher = mock.patch('socket.getaddrinfo', return_value=_public_dns())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_503_recorded_as_retryable_error(self):
        resp = _FakeResponse(503)
        with mock.patch.object(self.scraper.session, 'get', return_value=resp):
            result = self.scraper.fetch('http://example.com/a')
        self.assertEqual(result['content'], '')
        self.assertEqual(result['status_code'], 503)
        self.assertFalse(result['permanently_gone'])
        self.assertTrue(result['errors'], '503 必须记录错误')

    def test_404_marked_permanently_gone(self):
        resp = _FakeResponse(404)
        with mock.patch.object(self.scraper.session, 'get', return_value=resp):
            result = self.scraper.fetch('http://example.com/a')
        self.assertEqual(result['content'], '')
        self.assertTrue(result['permanently_gone'])
        self.assertTrue(result['errors'])

    def test_410_marked_permanently_gone(self):
        resp = _FakeResponse(410)
        with mock.patch.object(self.scraper.session, 'get', return_value=resp):
            result = self.scraper.fetch('http://example.com/a')
        self.assertTrue(result['permanently_gone'])

    def test_network_error_recorded(self):
        import requests
        with mock.patch.object(self.scraper.session, 'get',
                               side_effect=requests.exceptions.ConnectionError('conn refused')):
            result = self.scraper.fetch('http://example.com/a')
        self.assertEqual(result['content'], '')
        self.assertTrue(result['errors'])

    def test_fetch_content_propagates_errors(self):
        # 模块级 fetch_content：失败时 errors 非空并带状态码（成功路径需 bs4，这里只测失败路径）
        from Scraper.RequestsScraper import fetch_content
        resp = _FakeResponse(503)
        with mock.patch('socket.getaddrinfo', return_value=_public_dns()):
            with mock.patch('requests.Session.get', return_value=resp):
                result = fetch_content('http://example.com/a', 10000)
        self.assertEqual(result['content'], '')
        self.assertTrue(result['errors'])
        self.assertEqual(result.get('status_code'), 503)


@unittest.skipUnless(HAS_RSS_FLOW, '缺少 feedparser 等依赖，跳过 RSS 流程层测试')
class TestFlowLevelErrorMapping(unittest.TestCase):
    """flow 层：空内容+错误 → ProcessProblem('fetch_error')（可重试）；
    404/410/真空页面 → ProcessSkip（终态）。"""

    def test_503_empty_content_raises_process_problem(self):
        fetch_content = lambda link: {'content': '', 'errors': ['HTTP 错误: 503'], 'status_code': 503}
        with self.assertRaises(ProcessProblem) as cm:
            fetch_process_article(_make_article(), fetch_content, [])
        self.assertEqual(cm.exception.problem, 'fetch_error')

    def test_404_empty_content_raises_process_skip(self):
        fetch_content = lambda link: {'content': '', 'errors': ['HTTP 404: 资源已永久消失'],
                                      'status_code': 404, 'permanently_gone': True}
        with self.assertRaises(ProcessSkip):
            fetch_process_article(_make_article(), fetch_content, [])

    def test_real_empty_page_raises_process_skip(self):
        # 无任何错误信息的空内容（真空页面）→ skip
        fetch_content = lambda link: {'content': '', 'errors': []}
        with self.assertRaises(ProcessSkip):
            fetch_process_article(_make_article(), fetch_content, [])

    def test_fetch_exception_raises_process_problem(self):
        def fetch_content(link):
            raise ConnectionError('boom')
        with self.assertRaises(ProcessProblem):
            fetch_process_article(_make_article(), fetch_content, [])


# ---------------------------------------------------------------------------
# 2. CrawlPipeline：空内容不触发 handler、单篇异常不炸整轮
# ---------------------------------------------------------------------------

class _StubFetcher:
    """按 URL 表返回内容；last_error 模拟抓取故障。"""

    def __init__(self, content_map=None, error_urls=()):
        self._map = content_map or {}
        self._error_urls = set(error_urls)
        self.last_error = None

    def get_content(self, url, **kwargs):
        self.last_error = None
        if url in self._error_urls:
            self.last_error = '503 Server Error'
            return None
        return self._map.get(url)

    def close(self):
        pass


class _StubExtractor:
    def __init__(self, result_map):
        self._map = result_map

    def extract(self, content, url, **kwargs):
        return self._map[url]


def _make_mock_governor():
    """MagicMock 治理器：transaction 返回 mock task，可断言终态调用。"""
    governor = mock.MagicMock()
    governor.should_crawl.return_value = True
    task = mock.MagicMock()
    governor.transaction.return_value.__enter__.return_value = task
    governor.transaction.return_value.__exit__.return_value = False
    return governor, task


@unittest.skipUnless(HAS_PIPELINE, '缺少 tldextract 等依赖，跳过 Pipeline 测试')
class TestCrawlPipelineHandling(unittest.TestCase):

    def _build_pipeline(self, urls, fetcher, extractor, governor):
        pipeline = CrawlPipeline(
            name='t', d_fetcher=fetcher, discoverer=None,
            e_fetcher=fetcher, extractor=extractor,
            log_callback=lambda *a, **k: None, crawler_governor=governor)
        pipeline.articles = [(u, 'g') for u in urls]
        return pipeline

    def test_empty_extraction_does_not_call_handler(self):
        url = 'http://example.com/a'
        result = ExtractionResult(markdown_content='')     # 成功但无正文
        pipeline = self._build_pipeline([url], _StubFetcher({url: b'<html/>'}),
                                        _StubExtractor({url: result}), None)
        handler = mock.MagicMock()
        pipeline.extract_articles(content_handler=handler, politeness_delay_s=0)
        handler.assert_not_called()
        self.assertEqual(pipeline.contents, [])

    def test_error_extraction_does_not_call_handler(self):
        url = 'http://example.com/a'
        result = ExtractionResult(error='extract fail')    # 提取失败（BaseModel 恒真，不能只看 if result）
        pipeline = self._build_pipeline([url], _StubFetcher({url: b'<html/>'}),
                                        _StubExtractor({url: result}), None)
        handler = mock.MagicMock()
        pipeline.extract_articles(content_handler=handler, politeness_delay_s=0)
        handler.assert_not_called()

    def test_valid_extraction_calls_handler(self):
        url = 'http://example.com/a'
        result = ExtractionResult(markdown_content='正文内容')
        pipeline = self._build_pipeline([url], _StubFetcher({url: b'<html/>'}),
                                        _StubExtractor({url: result}), None)
        handler = mock.MagicMock()
        pipeline.extract_articles(content_handler=handler, politeness_delay_s=0)
        handler.assert_called_once()

    def test_handler_exception_does_not_break_round(self):
        # 单篇 handler 抛 ValidationError 类异常 → 记 FailPerm 本篇后继续，不中断整轮
        url_bad, url_good = 'http://example.com/bad', 'http://example.com/good'
        fetcher = _StubFetcher({url_bad: b'<html/>', url_good: b'<html/>'})
        extractor = _StubExtractor({
            url_bad: ExtractionResult(markdown_content='坏数据'),
            url_good: ExtractionResult(markdown_content='好数据'),
        })
        governor, task = _make_mock_governor()
        pipeline = self._build_pipeline([url_bad, url_good], fetcher, extractor, governor)

        calls = []

        def handler(u, g, r):
            calls.append(u)
            if u == url_bad:
                raise ValueError('single article validation fail')

        pipeline.extract_articles(content_handler=handler, politeness_delay_s=0)
        # 两篇文章的 handler 都被调用（第一篇炸后第二篇仍处理）
        self.assertEqual(calls, [url_bad, url_good])
        task.fail_perm.assert_called()

    def test_fetch_error_marks_temp_fail_not_skip(self):
        # fetcher 报 last_error（503）→ fail_temp 可重试，而不是 skip 终态
        url = 'http://example.com/a'
        fetcher = _StubFetcher(error_urls=(url,))
        extractor = _StubExtractor({})
        governor, task = _make_mock_governor()
        pipeline = self._build_pipeline([url], fetcher, extractor, governor)
        pipeline.extract_articles(content_handler=mock.MagicMock(), politeness_delay_s=0)
        task.fail_temp.assert_called()
        task.skip.assert_not_called()

    def test_no_content_marks_skip(self):
        # 无内容且无错误（真空页面）→ skip
        url = 'http://example.com/a'
        governor, task = _make_mock_governor()
        pipeline = self._build_pipeline([url], _StubFetcher({}), _StubExtractor({}), governor)
        pipeline.extract_articles(content_handler=mock.MagicMock(), politeness_delay_s=0)
        task.skip.assert_called()
        task.fail_temp.assert_not_called()


# ---------------------------------------------------------------------------
# 3. SSRFGuard（本文件内再覆盖一次核心判定，含 mock DNS 的公网放行）
# ---------------------------------------------------------------------------

class TestSSRFGuardCore(unittest.TestCase):

    def test_reject_all_private_ranges(self):
        for url in ('http://127.0.0.1/', 'http://10.1.2.3/', 'http://172.16.0.1/',
                    'http://192.168.0.1/', 'http://0.0.0.0/', 'http://[::1]/',
                    'http://169.254.1.1/', 'http://2130706433/'):
            with self.assertRaises(SSRFError, msg=f'应拒绝: {url}'):
                validate_url(url)

    def test_accept_public_domain_with_mocked_dns(self):
        with mock.patch('socket.getaddrinfo', return_value=_public_dns('93.184.216.34')):
            self.assertEqual(validate_url('http://example.com/'), 'http://example.com/')


# ---------------------------------------------------------------------------
# 4. pub_time 缺省为 None（不伪造当前时间）
# ---------------------------------------------------------------------------

class TestPubTimeDefaultNone(unittest.TestCase):

    def test_collected_data_allows_none_pub_time(self):
        # 数据模型契约：pub_time 允许 None
        from ServiceComponent.IntelligenceHubDefines_v2 import CollectedData
        data = CollectedData(UUID='u1', token='t', title='t', authors=[],
                             content='这是一段足够长度的正文内容文本', pub_time=None, informant='http://example.com/')
        self.assertIsNone(data.pub_time)

    @unittest.skipUnless(HAS_IC_FLOW and HAS_PIPELINE, '缺少依赖，跳过 handler 级测试')
    def test_handler_passes_none_when_no_date(self):
        captured = {}

        class StubContext:
            def submit_collected_data(self, group, collected_data):
                captured['data'] = collected_data

        result = ExtractionResult(markdown_content='这是一段足够长度的正文内容文本', metadata={})   # 无 date 字段
        intelligence_crawler_result_handler('http://example.com/a', 'g', result, StubContext())
        self.assertIn('data', captured)
        self.assertIsNone(captured['data'].pub_time)


if __name__ == '__main__':
    unittest.main()
