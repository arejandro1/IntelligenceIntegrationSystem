# -*- coding: utf-8 -*-
"""
China Watch 统一数据模型（M2）
包含：SourceEntry 来源注册条目、PreFilterResult 预筛结果。
规则：函数和类遵守 PEP 8，注释使用简体中文。
"""
from typing import List, Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class CrawlerConcurrency(BaseModel):
    """来源级爬虫并发限制"""
    normal: int = Field(2, description="普通爬虫并发上限")
    playwright: int = Field(1, description="Playwright 并发上限")


class SourceEntry(BaseModel):
    """来源注册表条目"""
    source_id: str = Field(..., description="唯一来源标识（如 voa、bbc）")
    name: str = Field(..., description="来源显示名称")
    source_type: Literal["rss", "list_page", "api", "other"] = Field("rss", description="抓取方式")
    channel_urls: List[str] = Field(default_factory=list, description="频道 URL 列表")
    enabled: bool = Field(True, description="启用/停用状态")
    is_priority: bool = Field(False, description="重点来源（用于 Pro 复核）")
    crawler_task: Optional[str] = Field(None, description="关联爬虫任务模块名")
    concurrency: CrawlerConcurrency = Field(default_factory=lambda: CrawlerConcurrency())
    tags: List[str] = Field(default_factory=list, description="来源标签")
    # M3 接线新增：文章页（informant）域名列表，用于爬虫不传 source 字段时的反查兜底。
    # 不同来源之间不得重叠，否则反查视为歧义而拒绝（fail-closed）。
    informant_domains: List[str] = Field(default_factory=list, description="文章 URL 域名（预筛反查用）")


class PreFilterResult(BaseModel):
    """
    预筛结果（三分类）：
    - RULE_REJECTED：拒绝（敏感词/参数错误）→ 拒绝审计
    - METADATA_ONLY：仅元数据（无需 AI）→ 直接元数据归档
    - NEEDS_AI：需 AI 分析 → 进入 AI 延期队列（M3）
    """
    original_uuid: str = Field(..., description="原始数据 UUID")
    source_id: str = Field(..., description="来源标识")
    decision: Literal["RULE_REJECTED", "METADATA_ONLY", "NEEDS_AI"] = Field(..., description="预筛决策")
    matched_rule: Optional[str] = Field(None, description="匹配的规则 ID")
    reason: str = Field("", description="决策原因说明")
    content_hash: str = Field(..., description="内容 SHA256 哈希（去重）")
    screened_at: datetime = Field(default_factory=datetime.now, description="预筛时间")
    metadata: dict = Field(default_factory=dict, description="预筛附带元数据")