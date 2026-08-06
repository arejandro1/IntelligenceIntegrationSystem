# -*- coding: utf-8 -*-
"""
RulePreFilter 规则预筛引擎（M2）
职责：Hub 侧唯一权威预筛，输出三分类决策：
RULE_REJECTED / METADATA_ONLY / NEEDS_AI
规则可配置，不调用 AI，不产生网络请求。
"""
import hashlib
from typing import Dict, List

from ServiceComponent.ChinaWatchDataModels import PreFilterResult
from ServiceComponent.SourceRegistry import SourceRegistry


class RulePreFilter:
    """基于可配置规则的本地预筛引擎。"""

    def __init__(self, registry: SourceRegistry, rules_config: Dict):
        self.registry = registry
        self.reject_patterns: List[str] = rules_config.get('reject_patterns', [])
        self.metadata_only_patterns: List[str] = rules_config.get('metadata_only_patterns', [])
        self.min_content_length: int = rules_config.get('min_content_length', 10)
        self.require_title: bool = rules_config.get('require_title', True)

    @staticmethod
    def _compute_hash(title: str, content: str) -> str:
        """计算内容 SHA256 哈希（用于去重）"""
        raw = f"{title}|{content}".encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def screen(self, raw_data: Dict) -> PreFilterResult:
        """
        对单条原始数据执行三级预筛：
        Level 1 参数校验（UUID / source / content / title）
        Level 2 内容长度与标题要求
        Level 3 拒绝模式 / 元数据模式
        """
        uuid_val = str(raw_data.get('UUID', '') or '')
        source_id = str(raw_data.get('source', '') or '')
        title = str(raw_data.get('title', '') or '')
        content = str(raw_data.get('content', '') or '')

        content_hash = self._compute_hash(title, content)

        # Level 1: 参数校验（fail-closed）
        if not uuid_val:
            return PreFilterResult(
                original_uuid=uuid_val, source_id=source_id,
                decision='RULE_REJECTED', matched_rule='PARAM_UUID_MISSING',
                reason='缺少 UUID', content_hash=content_hash)
        if not source_id:
            return PreFilterResult(
                original_uuid=uuid_val, source_id=source_id,
                decision='RULE_REJECTED', matched_rule='PARAM_SOURCE_MISSING',
                reason='缺少 source', content_hash=content_hash)
        if not self.registry.exists(source_id):
            return PreFilterResult(
                original_uuid=uuid_val, source_id=source_id,
                decision='RULE_REJECTED', matched_rule='SOURCE_UNREGISTERED',
                reason=f'来源未注册: {source_id}', content_hash=content_hash)
        if not self.registry.is_enabled(source_id):
            return PreFilterResult(
                original_uuid=uuid_val, source_id=source_id,
                decision='RULE_REJECTED', matched_rule='SOURCE_DISABLED',
                reason=f'来源已停用: {source_id}', content_hash=content_hash)

        # Level 2: 标题与内容长度
        if self.require_title and not title.strip():
            return PreFilterResult(
                original_uuid=uuid_val, source_id=source_id,
                decision='RULE_REJECTED', matched_rule='TITLE_REQUIRED',
                reason='标题为空', content_hash=content_hash)
        if len(content) < self.min_content_length:
            return PreFilterResult(
                original_uuid=uuid_val, source_id=source_id,
                decision='METADATA_ONLY', matched_rule='CONTENT_TOO_SHORT',
                reason=f'内容长度不足 {self.min_content_length}', content_hash=content_hash)

        # Level 3: 拒绝模式（大小写不敏感）
        content_lower = content.lower()
        for pattern in self.reject_patterns:
            if pattern.lower() in content_lower:
                return PreFilterResult(
                    original_uuid=uuid_val, source_id=source_id,
                    decision='RULE_REJECTED', matched_rule=f'REJECT_PATTERN:{pattern}',
                    reason=f'命中拒绝模式: {pattern}', content_hash=content_hash)

        # Level 3b: 元数据模式
        for pattern in self.metadata_only_patterns:
            if pattern.lower() in content_lower:
                return PreFilterResult(
                    original_uuid=uuid_val, source_id=source_id,
                    decision='METADATA_ONLY', matched_rule=f'METADATA_PATTERN:{pattern}',
                    reason=f'命中元数据模式: {pattern}', content_hash=content_hash)

        # 通过全部规则 → 需 AI
        return PreFilterResult(
            original_uuid=uuid_val, source_id=source_id,
            decision='NEEDS_AI', reason='通过规则预筛', content_hash=content_hash)