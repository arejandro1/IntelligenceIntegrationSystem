# -*- coding: utf-8 -*-
"""
SourceRegistry 来源注册表（M2）
职责：加载来源配置（YAML/JSON），提供来源查询与校验。
这是来源注册表的唯一数据源，业务代码不得写死来源列表。
"""
import json
import os
from typing import Dict, List, Optional

import yaml

from ServiceComponent.ChinaWatchDataModels import SourceEntry


class SourceRegistry:
    """来源注册表——唯一数据源。加载 YAML 或 JSON 配置。"""

    def __init__(self, config_path: str):
        self._config_path = config_path
        self._sources: Dict[str, SourceEntry] = {}
        self._load(config_path)

    def _load(self, path: str) -> None:
        """从 YAML/JSON 文件加载来源配置"""
        if not os.path.exists(path):
            raise FileNotFoundError(f"来源注册表文件不存在: {path}")

        ext = os.path.splitext(path)[1].lower()
        with open(path, 'r', encoding='utf-8') as f:
            if ext in ('.yaml', '.yml'):
                raw = yaml.safe_load(f)
            elif ext == '.json':
                raw = json.load(f)
            else:
                raise ValueError(f"不支持的配置格式: {ext}")

        sources_raw = raw.get('sources', []) if isinstance(raw, dict) else []
        for item in sources_raw:
            entry = SourceEntry(**item)
            if entry.source_id in self._sources:
                raise ValueError(f"来源重复注册: {entry.source_id}")
            self._sources[entry.source_id] = entry

    def get(self, source_id: str) -> Optional[SourceEntry]:
        """按 source_id 获取来源条目"""
        return self._sources.get(source_id)

    def get_enabled(self) -> List[SourceEntry]:
        """获取所有启用中的来源"""
        return [s for s in self._sources.values() if s.enabled]

    def get_priority(self) -> List[SourceEntry]:
        """获取启用中的重点来源（用于 Pro 复核）"""
        return [s for s in self._sources.values() if s.enabled and s.is_priority]

    def exists(self, source_id: str) -> bool:
        """来源是否存在"""
        return source_id in self._sources

    def is_enabled(self, source_id: str) -> bool:
        """来源是否启用"""
        entry = self._sources.get(source_id)
        return bool(entry and entry.enabled)

    def all_source_ids(self) -> List[str]:
        """所有来源 ID（含禁用）"""
        return list(self._sources.keys())

    def count(self) -> int:
        """来源总数"""
        return len(self._sources)

    def resolve_by_informant(self, url: str) -> Optional[SourceEntry]:
        """按文章 URL（informant）域名反查来源条目。

        M3 接线新增：现有 CrawlTasks 提交数据不携带 source 字段，
        预筛在 source 缺失时用本方法做域名反查兜底。

        匹配规则：netloc 与 informant_domains 精确相等，或为其子域
        （netloc == d 或 netloc.endswith('.' + d)）。
        多个来源同时命中视为歧义，返回 None（fail-closed，由调用方拒绝并告警）。
        """
        try:
            from urllib.parse import urlparse
            netloc = (urlparse(str(url)).netloc or '').lower().split(':')[0]
        except Exception:
            return None
        if not netloc:
            return None

        hits: List[SourceEntry] = []
        for entry in self._sources.values():
            for domain in entry.informant_domains:
                d = str(domain).lower().strip()
                if not d:
                    continue
                if netloc == d or netloc.endswith('.' + d):
                    hits.append(entry)
                    break
        return hits[0] if len(hits) == 1 else None