import re
from typing import Dict, List, Any

class IntelligenceScoringEngine:
    # 编辑倾向系数（硬编码提升为类常量）：
    # 本系统情报偏好向政治与安全倾斜（×1.15）、对社会与环境略降（×0.95），
    # 直接作用于归档总分。调整前请与分析团队确认，避免口径漂移。
    TAXONOMY_MULTIPLIERS = {
        "政治与安全": 1.15,
        "经济与金融": 1.05,
        "科技与网络": 1.00,
        "社会与环境": 0.95,
        "无情报价值": 0.00,
    }

    # 子事件编号列表模式：只数 "第一、第二、" 这类显式编号，
    # 不再用 "同时/第一" 等普通子串（会把"第一时间报道""同时代"误判为多事件汇编）。
    SUB_EVENT_PATTERN = re.compile(r'第[一二三四五六七八九十]+[、，,.\)]')
    SUB_EVENT_PENALTY_THRESHOLD = 5

    def __init__(self, config: Dict = None):
        self.config = config or self._get_default_config()
        self.weights = self.config["weights"]
        self.taxonomy_multipliers = self.config["taxonomy_multipliers"]

    def _get_default_config(self) -> Dict:
        return {
            "weights": {
                "影响深度": 3.0,
                "新颖性与异常性": 2.5,
                "演化与连锁潜力": 2.0,
                "影响广度": 1.5,
                "可行动性": 1.5,
                "舆情及认知影响": 0.5
            },
            "taxonomy_multipliers": dict(self.TAXONOMY_MULTIPLIERS),
            # P-review 修复：删除基于宽泛词的 recap_keywords
            # （"十大/观察/一周/本周/上周/年度" 会误杀 "二十大召开""观察员称""一周内"
            # 这类正常新闻标题）。回顾/总结类判定只保留 low_novelty_hints ——
            # V25 prompt 规则已要求 LLM 对回顾/汇编类在 REASON/TIPS 写出
            # "新增事实有限/总结类/回顾类" 等标记语，该信号比标题关键词可靠。
            "low_novelty_hints": [
                "新增事实有限", "无新增事实", "旧闻", "重复报道", "相似消息已覆盖",
                "总结类", "回顾类", "汇编类", "复述", "背景梳理"
            ]
        }

    def _safe_rate(self, rates: Dict, key: str):
        """取某维度评分。缺键或值为 None（LLM 判'无/无法判断'）时返回 None，
        由调用方按"不计入"处理（权重按有值维度归一化），而不是静默计 0。"""
        value = rates.get(key, None)
        if value is None:
            return None
        try:
            value = float(value)
        except Exception:
            return None
        return min(10.0, max(0.0, value))

    def _joined_text(self, data: Dict) -> str:
        parts = [
            data.get("EVENT_TITLE", ""),
            data.get("EVENT_BRIEF", ""),
            data.get("REASON", ""),
            data.get("TIPS", "")
        ]
        return " ".join(str(x) for x in parts if x)

    def _is_recap_like(self, data: Dict) -> bool:
        # 只依据 LLM 按 prompt 要求写出的低新颖性标记语判定（见 _get_default_config 注释）
        return self._is_low_novelty(data)

    def _is_low_novelty(self, data: Dict) -> bool:
        text = self._joined_text(data)
        return any(k in text for k in self.config["low_novelty_hints"])

    def _count_sub_events(self, data: Dict) -> int:
        text = str(data.get("EVENT_TEXT", "") or "")
        return len(self.SUB_EVENT_PATTERN.findall(text))

    def calculate_single(
        self,
        intelligence_data: Dict,
        similar_messages: List[Dict[str, Any]] = None
    ) -> float:
        taxonomy = intelligence_data.get("TAXONOMY", "")

        if taxonomy == "无情报价值":
            return 0.0

        rates = intelligence_data.get("RATE", {})

        # 1. 加权归一化到 0-10（None/缺键维度不计入权重，按有值维度归一化）
        raw_score = 0.0
        max_score = 0.0

        for dim, weight in self.weights.items():
            score = self._safe_rate(rates, dim)
            if score is None:
                continue
            raw_score += score * weight
            max_score += 10.0 * weight

        if max_score <= 0:
            return 0.0

        score = raw_score / max_score * 10.0

        # 2. 分类系数
        score *= self.taxonomy_multipliers.get(taxonomy, 1.0)

        # 3. 从模型现有字段推断内容形态
        is_recap_like = self._is_recap_like(intelligence_data)
        is_low_novelty = self._is_low_novelty(intelligence_data)

        novelty = self._safe_rate(rates, "新颖性与异常性")
        actionability = self._safe_rate(rates, "可行动性")
        evolution = self._safe_rate(rates, "演化与连锁潜力")

        # 4. 相似历史消息惩罚
        # P8 修复:similarity 可能为 None,直接 float(None) 会抛 TypeError,
        # 导致整条评分失败。用安全转换,非数值统一视为 0.0。
        similar_messages = similar_messages or []
        def _safe_float(v, default=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
        max_similarity = max(
            [_safe_float(x.get("similarity", 0.0)) for x in similar_messages],
            default=0.0
        )

        # 5. 回顾/总结/低新颖性惩罚
        if is_recap_like:
            score *= 0.35
            score = min(score, 3.0)

        if is_low_novelty:
            score *= 0.50
            score = min(score, 3.0)

        # 6. 高相似且低新颖性：强惩罚（维度为 None 时不触发，不凭空假设低分）
        if max_similarity >= 0.86 and novelty is not None and novelty <= 3:
            score *= 0.35
            score = min(score, 2.5)

        if max_similarity >= 0.92 and novelty is not None and novelty <= 2:
            score *= 0.25
            score = min(score, 2.0)

        # 7. 多事件汇编惩罚
        sub_event_count = self._count_sub_events(intelligence_data)
        if sub_event_count >= self.SUB_EVENT_PENALTY_THRESHOLD and novelty is not None and novelty <= 4:
            score *= 0.55
            score = min(score, 3.5)

        # 8. 低可行动性封顶
        if actionability is not None and actionability <= 2 and novelty is not None and novelty <= 3:
            score = min(score, 3.0)

        # 9. 无演化、无行动、无新颖性：归档级
        if (novelty is not None and novelty <= 2
                and actionability is not None and actionability <= 2
                and evolution is not None and evolution <= 3):
            score = min(score, 2.0)

        # 10. 边界
        return round(min(10.0, max(0.0, score)), 1)
