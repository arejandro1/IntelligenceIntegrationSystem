import time
import calendar
import datetime
import logging
from typing import List, Dict, Optional, Any, Union
from pydantic import BaseModel, Field, validator, field_validator, model_validator

logger = logging.getLogger(__name__)


# ==========================================
# Phase 1: Input Data (Before LLM Processing)
# ==========================================

class CollectedData(BaseModel):
    """
    Data collected from raw sources (crawlers, API, RSS, etc.).
    Passed as context to the LLM.
    """
    UUID: str = Field(
        ...,
        min_length=1,
        description="[MUST]: Unique identifier for the message/article."
    )

    token: str = Field(
        ...,
        min_length=1,
        description="[MUST]: Validation token for the endpoint/source."
    )

    source: str | None = Field(
        None,
        description="(Optional): Origin platform (e.g., 'Twitter', 'BBC')."
    )

    target: str | None = Field(
        None,
        description="(Optional): Intended recipient or channel."
    )

    prompt: str | None = Field(
        None,
        description="(Optional): Custom prompt override for this specific item."
    )

    title: str = Field(
        ...,
        min_length=1,
        description="[MUST]: Raw title of the article."
    )

    authors: list[str] = Field(
        default_factory=list,
        description="(Optional): List of authors found in metadata."
    )

    content: str = Field(
        ...,
        min_length=10,
        description="[MUST]: The main text body to be analyzed by LLM."
    )

    pub_time: Optional[Any] = Field(
        None,
        description="(Optional): Original publish time."
    )

    collect_time: datetime.datetime | None = Field(
        default_factory=datetime.datetime.now,
        description="(Optional): Content collect time."
    )

    # 建议：如果 informant 是 URL，可以用 AnyHttpUrl 增强验证
    informant: str = Field(
        ...,
        min_length=1,
        description="[MUST]: The specific source URL or informant ID."
    )

    temp_data: dict = Field(
        default_factory=dict,
        description="(Optional): Temporary data for data management."
    )

    class Config:
        # 可选：如果传入的数据包含 extra 字段，是否允许（默认忽略，设为 forbid 会报错）
        extra = "ignore"

    @field_validator('pub_time', mode='before')
    @classmethod
    def convert_any_time_format(cls, v):
        """处理各种时间格式，并强制返回 aware datetime（UTC）。
        解析失败时原样放行（交由下游决定），但 naive datetime 必须补上时区，
        避免下游混合 naive/aware 做时间比较时抛 TypeError。"""
        if v is None:
            return None
        if isinstance(v, datetime.datetime):
            if v.tzinfo is None:
                # naive datetime：按 UTC 补时区（不偏移数值，保持入库时间戳稳定，
                # 下游统一按 UTC 比较）
                return v.replace(tzinfo=datetime.timezone.utc)
            return v
        if isinstance(v, (int, float)):
            # 时间戳按 UTC 解释
            return datetime.datetime.fromtimestamp(v, tz=datetime.timezone.utc)
        if isinstance(v, time.struct_time):
            # RSS published_parsed 是 UTC struct_time，
            # time.mktime 会按本地时区解释（东八区偏 8 小时），改用 calendar.timegm。
            return datetime.datetime.fromtimestamp(calendar.timegm(v), tz=datetime.timezone.utc)
        if isinstance(v, str):
            parsed: datetime.datetime | None = None
            try:
                parsed = datetime.datetime.fromisoformat(v.replace('Z', '+00:00'))
            except ValueError:
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y %H:%M:%S']:
                    try:
                        parsed = datetime.datetime.strptime(v, fmt)
                        break
                    except ValueError:
                        # 多格式逐个尝试解析链：本格式不匹配换下一格式（有意继续）
                        continue
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                return parsed
            # 字符串无法解析：原样放行，不阻塞入库
            return v
        # 其他未知类型：原样放行
        return v


# ==========================================
# Phase 2: Processed Output (LLM Result)
# ==========================================

# RATE 评分的六个规范维度键（与 V25 prompt、IntelligenceScoringEngine 权重键一致）
RATE_CANONICAL_KEYS = (
    '影响广度', '影响深度', '新颖性与异常性',
    '演化与连锁潜力', '舆情及认知影响', '可行动性',
)

# 键别名映射：LLM 输出的简写/英文维度名 → 中文规范键。
# 键统一按 strip 后精确匹配；英文另按 lower() 匹配（见 _canonical_rate_key）。
RATE_KEY_ALIASES = {
    # 中文简写/变体
    '新颖性': '新颖性与异常性',
    '异常性': '新颖性与异常性',
    '演化潜力': '演化与连锁潜力',
    '演化': '演化与连锁潜力',
    '连锁潜力': '演化与连锁潜力',
    '舆情影响': '舆情及认知影响',
    '舆情': '舆情及认知影响',
    '认知影响': '舆情及认知影响',
    '行动性': '可行动性',
    # 英文键（V21/V25 prompt 中的维度英文名）
    'impact scope': '影响广度',
    'impact severity': '影响深度',
    'novelty': '新颖性与异常性',
    'novelty & anomaly': '新颖性与异常性',
    'novelty and anomaly': '新颖性与异常性',
    'evolution potential': '演化与连锁潜力',
    'sentiment potential': '舆情及认知影响',
    'actionability': '可行动性',
}

# 表示"该维度无法评分"的标记语：映射为 None，评分引擎不计入权重（而非计 0）
RATE_NONE_TOKENS = ('无', '无法判断')


def _canonical_rate_key(key: str) -> str:
    """RATE 键别名归一：先精确匹配，再按小写匹配英文别名；都不是则原样返回。"""
    stripped = key.strip()
    if stripped in RATE_KEY_ALIASES:
        return RATE_KEY_ALIASES[stripped]
    lowered = stripped.lower()
    if lowered in RATE_KEY_ALIASES:
        return RATE_KEY_ALIASES[lowered]
    return stripped


# ProcessedData 文本字段长度上限（字符）。LLM 输出失控时截断兜底，
# 截断事实写入 APPENDIX.__FIELD_TRUNCATED__（若有 APPENDIX）并记日志。
PROCESSED_FIELD_MAX_LENGTH = {
    'EVENT_TITLE': 200,
    'EVENT_BRIEF': 1000,
    'IMPACT': 1000,
    'REASON': 1000,
    'TIPS': 1000,
}


class ProcessedData(BaseModel):
    """
    Structured intelligence data extracted and validated from LLM output.
    Design Philosophy: Fields are Optional to handle the "NonIntelligence" branching logic.
    """

    # --- Core Classification (Prompt: TAXONOMY, SUB_CATEGORY) ---
    TAXONOMY: str = Field(
        ...,
        description="High-level category (e.g., 'Politics', 'NonIntelligence'). Dynamic string."
    )

    SUB_CATEGORY: list[str] | None = Field(  # 建议使用 list[str]
        default_factory=list,
        description="Specific tags selected from the prompt list (e.g., ['International Relations']). Dynamic list."
    )

    # --- Analysis Reasoning (Prompt: REASON) ---
    REASON: str | None = Field(
        None,
        description="Explanation for the classification (Used for both Valuable and Non-Intelligent items)."
    )

    # --- Event Details (Prompt: EVENT_TITLE, EVENT_BRIEF, EVENT_TEXT) ---
    # 移除 min_length=1：NonIntelligence / 低价值分支常无事件标题，
    # 强制非空会让空标题数据整体走 error 分支，而非正常 drop。
    # 下游统一用 .get("EVENT_TITLE") or "" 兜底，空标题是合法状态。
    EVENT_TITLE: str | None = Field(
        None,
        description="Core title of the intelligence event (<20 chars)."
    )

    EVENT_BRIEF: str | None = Field(
        None,
        description="Factual summary of the event (<50 chars)."
    )

    EVENT_TEXT: str | None = Field(
        None,
        description="Detailed intelligence report/briefing generated by LLM (>2000 chars if content allows)."
    )

    # --- Entities & Context (Prompt: TIME, LOCATION, GEOGRAPHY, PEOPLE, ORGANIZATION) ---
    TIME: list[str] | None = Field(
        default_factory=list,
        description="Standardized event dates extracted from text (Format: YYYY-MM-DD)."
    )

    LOCATION: list[str] | None = Field(
        default_factory=list,
        description="Specific locations mentioned (Cities, Regions)."
    )

    GEOGRAPHY: str | None = Field(
        None,
        description="Country ISO code (e.g., 'CN', 'US') or English name."
    )

    PEOPLE: list[str] | None = Field(
        default_factory=list,
        description="Key individuals involved."
    )

    ORGANIZATION: list[str] | None = Field(
        default_factory=list,
        description="Key organizations or groups involved."
    )

    # --- Impact Analysis (Prompt: IMPACT, RATE, TIPS) ---
    IMPACT: str | None = Field(
        None,
        description="Brief description of the event's impact (<50 chars)."
    )

    # 宽松校验：LLM 常输出 "5.5"/"高"/null 等非标准值，严格 dict[str,int]
    # 会让整条 AI 分析结果被拒。这里在 before 阶段把可解析的数值归一到 int，
    # 中文等级映射到分数，无法解析的丢弃，最后 clamp 到 [0,10]。
    RATE: dict[str, Any] | None = Field(
        default_factory=dict,
        description="Scoring dictionary (1-10) for dimensions like Impact, Novelty, Actionability."
    )

    @field_validator('RATE', mode='before')
    @classmethod
    def _coerce_rate_dict(cls, v):
        """将 LLM 返回的各种松散评分格式归一为 dict[str,int|None]，并 clamp 到 [0,10]。
        - 数字字符串 / 浮点数 → int
        - 中文等级（高/中/低/强/弱）→ 8/5/3
        - '无' / '无法判断' → None（保留 None 语义：评分引擎对该维度不计入权重，而非计 0）
        - 键别名归一（如 '新颖性'→'新颖性与异常性'、英文维度名→中文全名，
          对照 V25 prompt 与 IntelligenceScoringEngine 的六个权重键）
        - 缺键时打 warning 日志，不再静默计 0
        - None / 空字符串 / 无法解析 → 丢弃该 key
        - None 输入 → 返回 {}"""
        if v is None:
            return {}
        if not isinstance(v, dict):
            # 整个 RATE 不是 dict（如 LLM 返回字符串）→ 宽松放行为空 dict
            return {}
        grade_map = {
            '高': 8, '强': 8, '严重': 9, '重大': 9, '极高': 10,
            '中': 5, '一般': 5, '普通': 5,
            '低': 3, '弱': 3, '轻微': 2, '极低': 1,
        }
        result: dict[str, Any] = {}
        for k, raw in v.items():
            if raw is None:
                continue
            key = _canonical_rate_key(str(k))
            if isinstance(raw, bool):
                # bool 是 int 子类，单独处理避免 True→1
                continue
            if isinstance(raw, str) and raw.strip() in RATE_NONE_TOKENS:
                # '无'/'无法判断'：保留 None 语义，评分引擎按有值维度归一化权重
                result.setdefault(key, None)
                continue
            score: float | None = None
            if isinstance(raw, (int, float)):
                score = float(raw)
            elif isinstance(raw, str):
                s = raw.strip()
                if not s:
                    continue
                if s in grade_map:
                    score = float(grade_map[s])
                else:
                    try:
                        score = float(s)
                    except ValueError:
                        # 非数值评分维度：按设计跳过该维度（不参与评分权重）
                        continue
            if score is None:
                continue
            # clamp 到 [0, 10]
            if score < 0:
                score = 0.0
            elif score > 10:
                score = 10.0
            # 别名与规范键同时出现时，优先保留已有的规范键数值
            if key not in result or result[key] is None:
                result[key] = int(round(score))
        # 缺键告警：下游评分引擎对缺键/None 维度不计入权重，这里留下可见痕迹
        missing = [d for d in RATE_CANONICAL_KEYS if d not in result]
        if result and missing:
            logger.warning(f'RATE 缺少维度键（按未评分处理，不计入权重）: {missing}')
        return result

    TIPS: str | None = Field(
        None,
        description="Analyst remarks or handling difficulties."
    )

    @model_validator(mode='before')
    @classmethod
    def _truncate_overlong_fields(cls, data):
        """文本字段长度兜底：EVENT_TITLE/EVENT_BRIEF/IMPACT/REASON/TIPS 超过
        PROCESSED_FIELD_MAX_LENGTH 上限时截断，防止 LLM 输出失控无约束入库。
        截断事实记录到 APPENDIX.__FIELD_TRUNCATED__（仅当输入已携带 APPENDIX dict，
        即归档流程；纯分析中间态没有 APPENDIX 时仅记日志）。"""
        if not isinstance(data, dict):
            return data
        truncated = []
        for field_name, limit in PROCESSED_FIELD_MAX_LENGTH.items():
            value = data.get(field_name)
            if isinstance(value, str) and len(value) > limit:
                data[field_name] = value[:limit]
                truncated.append(field_name)
        if truncated:
            appendix = data.get('APPENDIX')
            if isinstance(appendix, dict):
                appendix.setdefault(APPENDIX_FIELD_TRUNCATED, []).extend(truncated)
            logger.warning(f'ProcessedData 字段超长被截断: {truncated}')
        return data


# ==========================================
# Phase 3: Archival Storage (Database Schema)
# ==========================================

class ArchivedDataExtraFields(BaseModel):
    """
    Additional fields appended during the persistence layer (Database insertion).
    """
    UUID: str = Field(
        ...,
        min_length=1,
        description="[MUST] Unique identifier for the message/article."
    )

    INFORMANT: str = Field(
        ...,
        min_length=1,
        description="[MUST] The specific source URL or informant ID."
    )

    RAW_DATA: Dict[str, Any] | None = Field(
        None,
        description="The full raw original message."
    )

    SUBMITTER: str | None = Field(
        None,
        description="Where the intelligence comes from."
    )

    APPENDIX: Dict[str, Any] | None = Field(
        None,
        description="Any extra metadata, file attachments, or extended properties."
    )


class ArchivedData(ProcessedData, ArchivedDataExtraFields):
    """
    Final model representing a record stored in the database.
    Combines processed intelligence with system archival metadata.
    """
    pass


# ----------------------------------------------------------------------------------------------------------------------

APPENDIX_TIME_PUB           = '__TIME_PUB__'            # Timestamp of article published
APPENDIX_TIME_GOT           = '__TIME_GOT__'            # Timestamp of get from collector
APPENDIX_TIME_POST          = '__TIME_POST__'           # Timestamp of post to processor
APPENDIX_TIME_DONE          = '__TIME_DONE__'           # Timestamp of retrieve from processor
APPENDIX_TIME_ARCHIVED      = '__TIME_ARCHIVED__'

APPENDIX_ARCHIVED_FLAG      = '__ARCHIVED__'

APPENDIX_TOTAL_SCORE        = '__TOTAL_SCORE__'
APPENDIX_MANUAL_RATING      = '__MANUAL_RATING__'

APPENDIX_PROMPT_VERSION     = '__PROMPT_VERSION__'
APPENDIX_AI_SERVICE         = '__AI_SERVICE__'
APPENDIX_AI_MODEL           = '__AI_MODEL__'

# APPENDIX_LINK_UPSTREAM      = '__LINK_UPSTREAM__'
# APPENDIX_LINK_DOWNSTREAM    = '__LINK_DOWNSTREAM__'

APPENDIX_VECTOR_SCORE       = '__VECTOR_SCORE__'        # Temporary added when doing vector search

APPENDIX_TRANSLATED_REV     = "__TRANSLATED_REVISION__"

APPENDIX_ANALYSIS_WARNING   = '__ANALYSIS_WARNING__'    # AI 输出告警（如 output_truncated / json_repaired）
APPENDIX_FIELD_TRUNCATED    = '__FIELD_TRUNCATED__'     # 字段超长被截断的字段名列表
APPENDIX_ARCHIVED_RETRY     = '__ARCHIVE_RETRY_COUNT__' # E 标记数据重投次数（达到上限后不再加载）
APPENDIX_TRANSLATE_FAILED   = '__TRANSLATE_FAILED__'    # 翻译失败重试计数
APPENDIX_TRANSLATE_PARTIAL  = '__TRANSLATE_PARTIAL__'   # 部分字段翻译成功，留待重试
APPENDIX_TRANSLATE_DEAD     = '__TRANSLATE_DEAD__'      # 翻译死信标记（超过重试上限，backfill 跳过）

APPENDIX_CLUSTER_CURRENT    = '__CLUSTER_CURRENT__'     # 当前聚类归属（最近一次离线聚合写回，覆盖式）
APPENDIX_CLUSTER_HISTORY    = '__CLUSTER_HISTORY__'     # 聚类归属历史（按版本追加，长度受 history_keep 截断）


ARCHIVED_FLAG_DROP = 'D'
ARCHIVED_FLAG_ERROR = 'E'
ARCHIVED_FLAG_RETRY = 'R'
ARCHIVED_FLAG_ARCHIVED = 'A'
ARCHIVED_FLAG_SENSITIVE = 'S'
ARCHIVED_FLAG_DUPLICATED = 'U'
# M3 接线新增：预筛 METADATA_ONLY 数据仅元数据归档、不进 AI 队列的终态标记。
# _load_unarchived_data 只恢复无标记与 E 标记文档，M 标记不会被误恢复。
ARCHIVED_FLAG_METADATA = 'M'


VECTOR_DB_SUMMARY = 'intelligence_summary'
VECTOR_DB_FULL_TEXT = 'intelligence_full_text'
















