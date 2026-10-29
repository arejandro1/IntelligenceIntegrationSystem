import datetime
import PyRSS2Gen
from typing import List
from pydantic import BaseModel

from Tools.DateTimeUtility import get_aware_time


class FeedItem(BaseModel):
    guid: str
    title: str
    link: str
    description: str | None = None
    pub_date: datetime.datetime | None = None


class RSSPublisher:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def generate_feed(self, channel_title: str, channel_link: str, channel_description: str, feed_items: List[FeedItem]) -> str:
        """
        Generate RSS XML content

        Args:
            channel_title: Channel title
            channel_link: Channel URL
            channel_description: Channel description
            feed_items: Feed items data
        Returns:
            RSS XML string
        """
        rss_items = [
            PyRSS2Gen.RSSItem(
                title=item.title,
                link=self.join_url(self.base_url, item.link),
                description=item.description,
                # P8: 默认 isPermaLink=True 但在 guid 是 UUID(非 URL)时语义错误,
                # 显式设为 False 表示这是不可点击的稳定标识符。
                guid=PyRSS2Gen.Guid(item.guid, isPermaLink=False),
                # P8: 统一转 UTC。PyRSS2Gen 会硬编码输出 "GMT", 若传入非 UTC
                # aware 时间,时区信息会被错误丢弃/误导。naive 时间补 UTC。
                pubDate=self._to_utc(item.pub_date) if item.pub_date else None
            ) for item in feed_items
        ]
        # P8: 统一转 UTC
        last_build = self._to_utc(get_aware_time())

        rss = PyRSS2Gen.RSS2(
            title=channel_title,
            link=self.join_url(self.base_url, channel_link),
            description=channel_description,
            lastBuildDate=last_build,
            items=rss_items
        )

        return rss.to_xml(encoding="utf-8")

    @staticmethod
    def _to_utc(dt: datetime.datetime) -> datetime.datetime:
        """
        P8: 将任意 datetime 规整为 UTC aware, 避免 PyRSS2Gen 硬编码 "GMT"
        标签时对非 UTC aware 时间的时区误标。
        - aware 非 UTC: 转 UTC(偏移换算)
        - naive: 视为 UTC 补时区(不拨数值)
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    @staticmethod
    def join_url(url_prefix: str, link: str):
        return url_prefix.removesuffix('/') + '/' + link.removeprefix('/')
