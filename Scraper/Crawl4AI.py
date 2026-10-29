# -*- coding: utf-8 -*-
# 实验性模块：当前未接入生产抓取链路，仅作技术验证保留。
import asyncio
from typing import List, Optional, TypedDict
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, JsonCssExtractionStrategy, CacheMode

from Scraper.ScraperBase import ScraperResult, ProxyConfig


async def _async_fetch(
        url: str,
        timeout_ms: int,
        proxy: Optional[ProxyConfig] = None,
        **kwargs
) -> ScraperResult:
    result = {"content": "", "errors": []}

    # proxy 允许为 None，按需组装 BrowserConfig 参数
    browser_config_kwargs = dict(
        user_agent_mode="random",
        user_agent_generator_config={"device_type": "desktop"},
        viewport={"width": 1280, "height": 720},
    )
    if proxy:
        browser_config_kwargs["proxy_config"] = {"server": proxy.get('http', '')}

    # 修复：原 schema 中 fields 为字面 Ellipsis（...），JsonCssExtractionStrategy 无法处理。
    # 给出可工作的最小合法 schema（提取正文文本），调用方可通过 kwargs['schema'] 覆盖。
    schema = kwargs.get('schema') or {
        "selector": "body",
        "fields": [{"name": "text", "type": "text"}]
    }

    try:
        browser_config = BrowserConfig(**browser_config_kwargs)
        run_config = CrawlerRunConfig(
            extraction_strategy=JsonCssExtractionStrategy(schema),
            cache_mode=CacheMode.BYPASS,
        )

        async with AsyncWebCrawler(browser_config=browser_config) as crawler:
            response = await crawler.arun(
                url=url,
                run_config=run_config
            )

            if response.success:
                result["content"] = response.markdown
            else:
                result["errors"].append(f"STATUS:{response.status_code} | {response.error}")

    except Exception as e:
        error_msg = f"CRITICAL:{str(e)}"
        result["errors"].append(error_msg)

    return result


def fetch_content(**kwargs) -> ScraperResult:
    return asyncio.run(_async_fetch(**kwargs))
