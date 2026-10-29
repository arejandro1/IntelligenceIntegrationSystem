# --------------------------------------------------------------------- #
#   RssFeedsBasedCrawlFlow.py                                              #
#    - Traditional crawl framework.                                     #
#    - New implemented crawls will use IntelligenceCrawler framework.   #
# --------------------------------------------------------------------- #
import os

import threading
from uuid import uuid4
from typing import Callable, TypedDict, Dict, List

from IntelligenceHub import CollectedData
from Tools.RSSFetcher import FeedData, RssItem
from CrawlerServiceEngine import ServiceContext
from GlobalConfig import DEFAULT_COLLECTOR_TOKEN
from MyPythonUtility.easy_config import EasyConfig
from Workflow.CommonFlowUtility import CrawlContext
from IntelligenceHubWebService import DEFAULT_IHUB_PORT
from IntelligenceCrawler.CrawlerGovernanceCore import CrawlSession
from Tools.ProcessCotrolException import ProcessError, ProcessProblem, ProcessSkip


DEFAULT_FEEDS_CRAWL_LOOP_DURATION = 15 * 60


class FetchContentResult(TypedDict):
    content: str


# --------------------------------- Helper Functions ---------------------------------

def build_crawl_ctx_by_service_ctx(name, service_context: ServiceContext) -> CrawlContext:
    config = service_context.config
    governor = service_context.crawler_governor
    # P1-8 修复：优先读取环境变量 CHINA_WATCH_SUBMIT_URL（Docker 容器间通信用），
    # 其次读取配置文件，最后回落本机地址。原实现只看配置，容器内配置缺失时
    # 回落 http://127.0.0.1:5000 即容器自身，采集链路整体断开。
    submit_ihub_url = (
        os.environ.get('CHINA_WATCH_SUBMIT_URL')
        or config.get('collector.submit_ihub_url', f'http://127.0.0.1:{DEFAULT_IHUB_PORT}')
    )
    collector_tokens = config.get('intelligence_hub_web_service.collector.tokens')
    token = collector_tokens[0] if collector_tokens else DEFAULT_COLLECTOR_TOKEN
    crawl_context = CrawlContext(name, submit_ihub_url, token, governor)
    return crawl_context


def fetch_process_article(article: RssItem,
                          fetch_content: Callable[[str], FetchContentResult],
                          scrubbers: List[Callable[[str], str]]
                          ) -> CollectedData:
    try:
        content = fetch_content(article.link)
    except Exception:
        raise ProcessProblem('fetch_error', article.link)

    # ---------------------------------------------------
    # TODO: If an article always convert fail. Need a special treatment.

    raw_html = content.get('content') or ''
    if not raw_html:
        # 区分抓取失败与真空页面：
        # - 404/410（permanently_gone）或无错误信息的空内容 → 终态 skip；
        # - 带错误信息（4xx/5xx/网络异常）的空内容 → fetch_error 可重试，
        #   避免瞬时故障被治理层记成 SKIPPED 后永久不再抓。
        errors = content.get('errors') or []
        if isinstance(errors, str):
            errors = [errors]
        if content.get('permanently_gone') or not errors:
            raise ProcessSkip('Got empty content at fetch.')
        raise ProcessProblem('fetch_error', article.link)

    text = raw_html
    for scrubber in scrubbers:
        text = scrubber(text)
        if not text:
            break
    if not text:
        raise ProcessSkip('Got empty content after scrub.')

    # --------------- Pack Fetched Data ---------------

    collected_data = CollectedData(
        UUID=str(uuid4()),
        token='-',  # Will be filled in submit_collected_data()

        title=article.title,
        authors=article.authors,
        content=text,
        pub_time=article.published,
        informant=article.link
    )

    return collected_data


def get_and_submit_article(group_path: str,
                           article: RssItem,
                           context: CrawlContext,
                           fetch_content: Callable[[str], FetchContentResult],
                           scrubbers: List[Callable[[str], str]]
                           ):
    with context.crawler_governor.transaction(article.link, group_path) as task:
        try:
            if content := context.check_get_cached_data(article.link):
                collected_data, group = content
                context.logger.info(f'[cache] Got data from cache: {article.link}')
            else:
                collected_data = fetch_process_article(article, fetch_content, scrubbers)

            try:
                task.save_file(collected_data.content, collected_data.title)
            except Exception as e:
                context.logger.info(f'Fail to save fetched data.')

            context.submit_collected_data(group_path, collected_data)

            task.success()

        except CrawlSession.Flow as e:
            raise   # Handle by context

        except Exception as e:
            context.handle_process_exception(task, e)


# ---------------------------------- Main process ----------------------------------

def feeds_craw_flow(flow_name: str,
                    feeds: Dict[str, str],
                    stop_event: threading.Event,
                    config: EasyConfig,
                    update_interval_s: int,

                    fetch_feed: Callable[[str], FeedData],
                    fetch_content: Callable[[str], FetchContentResult],
                    scrubbers: List[Callable[[str], str]],

                    context: CrawlContext
                    ):
    """
    A common feeds and their articles craw workflow. This workflow works in this sequence:
        fetch_feed -> for each feed: fetch_content -> apply scrubbers

    :param flow_name: The workflow name for logging and tracing.
    :param feeds: The feeds dict like:
                {
                    'feed name': 'feed link'
                }
    :param stop_event: The stop event to quit loop.
    :param config: The easy config. This function will get token from it.
    :param update_interval_s: The polling update interval in second.

    :param fetch_feed: The function to fetch feed. Function declaration:
                        fetch_feed(feed_url: str) -> dict
    :param fetch_content: The function to fetch web content by url. Function declaration:
                        fetch_content(article_link: str) -> dict
    :param scrubbers: The functions to process scrubbed text. Function declaration:
                        scrubber(text: str) -> str
    :param context: CrawlContext for data record and submission.

    :return: None
    """
    context.logger.info(f'starts work.')

    # ----------------------------------------- Process Cached Data -----------------------------------------

    # 提交上一轮遗留的缓存数据；返回本轮已消费（尝试过提交）的缓存链接集合，
    # 主循环据此跳过这些链接，保证每轮每条缓存数据只提交一次，不重复消费。
    submitted_cache_urls = context.submit_cached_data() or set()

    # ------------------------------------------- The Main Process ------------------------------------------

    # update_interval_s 传入值优先；为 None/0 时回落默认轮询周期
    with context.crawler_governor.schedule_pace(flow_name, update_interval_s or DEFAULT_FEEDS_CRAWL_LOOP_DURATION, stop_event):

        for feed_name, feed_url in { **feeds, "cached": "" }.items():

            if stop_event.is_set(): break
            if feed_name == 'cached': continue      # Placeholder for processing cached data.

            group_path = '/'.join([flow_name, feed_name])
            context.crawler_governor.register_group_metadata(group_path, feed_url)

            # ----------------------------------- Fetch and Parse feeds -----------------------------------

            context.logger.info(f'Processing feed: [{feed_name}] - {feed_url}')

            with context.crawler_governor.transaction(feed_url, group_path) as task:
                try:
                    result = fetch_feed(feed_url)
                    if result.fatal:
                        raise ProcessError(error_text = '\n'.join(result.errors))
                    task.success()
                except Exception as e:
                    context.logger.error(f"Process feed fail: {feed_url} - {str(e)}")
                    task.fail_temp(state_msg=str(e))
                    continue

            context.crawler_governor.start_round(group_path, len(result.entries))
            context.logger.info(f'Feed: [{feed_name}] process finished, found {len(result.entries)} articles.')

            # ----------------------------------- Process Articles in Feed ----------------------------------

            for article in result.entries:
                if not article.link:
                    context.logger.info(f'Got empty article link from feed: {feed_url}')
                    context.crawler_governor.reduce_round_step(group_path)
                    continue

                if article.link in submitted_cache_urls:
                    # 本轮开头已从缓存尝试提交过该链接（成功或已重新入缓存），
                    # 同轮不再重复抓取/重复提交，避免重复消费同一缓存数据。
                    context.crawler_governor.skip_round_step(group_path)
                    continue

                if not context.crawler_governor.should_crawl(article.link):
                    task.ignore(state_msg='Already fetched - Ignore.')
                    context.crawler_governor.skip_round_step(group_path)
                    continue

                context.logger.info(f'Processing article: {article.link}')
                get_and_submit_article(group_path, article, context, fetch_content, scrubbers)

                context.crawler_governor.wait_interval(1)

            context.crawler_governor.finish_round(group_path, 60 * 15)
