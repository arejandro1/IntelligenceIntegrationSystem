# IntelligenceHubWebService.py

import os
import re
import json
import hmac
import logging
import datetime
import secrets
import time
import dateutil
import threading
import traceback
import uuid
from functools import wraps
from typing import List, Tuple, Any, Dict, Optional
from dateutil import parser as date_parser
from flask import Flask, request, jsonify, session, redirect, url_for, render_template, abort, send_file, \
    make_response, Response, flash

from GlobalConfig import *
from MyPythonUtility.DictTools import DictPrinter
from prompts_v2x import ANALYSIS_PROMPT_TABLE as PROMPT_TABLE_V2
from Tools.CommonPost import common_post
from Tools.RequestTracer import RequestTracer
from Tools.DateTimeUtility import get_aware_time, ensure_timezone_aware, time_str_to_datetime
from ServiceComponent.UserManager import UserManager
from MyPythonUtility.ArbitraryRPC import RPCService
from ServiceComponent.RSSPublisher import RSSPublisher, FeedItem
from ServiceComponent.PostManager import generate_html_from_markdown
from ServiceComponent.IntelligenceDistributionPageRender import get_intelligence_statistics_page
from ServiceComponent.IntelligenceHubDefines_v2 import APPENDIX_VECTOR_SCORE, APPENDIX_TOTAL_SCORE
from ServiceComponent.RateStatisticsPageRender import get_statistics_page
from ServiceComponent.IntelligenceVectorDBEngine import IntelligenceVectorDBEngine
from IntelligenceHub import CollectedData, IntelligenceHub, ProcessedData, APPENDIX_TIME_ARCHIVED
from Tools.PerformanceLogger import get_performance_logger, PerformanceLogger
from Tools.RateLimiter import SlidingWindowRateLimiter, TimedSemaphore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Python 3.12 已移除 distutils，提供兼容实现
def _strtobool(value):
    """兼容 distutils.util.strtobool 的布尔字符串解析。"""
    value = str(value).strip().lower()
    if value in ('y', 'yes', 't', 'true', 'on', '1'):
        return 1
    if value in ('n', 'no', 'f', 'false', 'off', '0'):
        return 0
    raise ValueError(f"invalid truth value {value!r}")
strtobool = _strtobool


CSRF_SESSION_KEY = '_iis_csrf_token'


def get_csrf_token() -> str:
    """获取当前 Session 的 CSRF Token；Token 本身不包含业务数据。"""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_token_is_valid() -> bool:
    """同时支持普通表单字段和 AJAX Header。"""
    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
    return bool(expected and supplied and hmac.compare_digest(str(expected), str(supplied)))


def csrf_protected(f):
    """保护依赖浏览器 Session 的写操作。"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if csrf_token_is_valid():
            return f(*args, **kwargs)

        payload = {
            'error': 'CSRF validation failed',
            'code': 'CSRF_FAILED'
        }
        if request.is_json or request.accept_mimetypes.best == 'application/json':
            return jsonify(payload), 403
        abort(403, description='页面已过期，请刷新后重试。')

    return decorated_function


VECTOR_MAX_TOP_N = 50
VECTOR_DEFAULT_SCORE_THRESHOLD = 0.6

# 未登录用户默认限制（可在构造时通过 public_search_limits 覆盖）
DEFAULT_PUBLIC_SEARCH_LIMITS = {
    "mongo_max_per_page": 20,
    "mongo_max_page": 5,
    "mongo_default_window_days": 30,
    "vector_max_per_page": 10,
    "vector_max_top_n": 20,
    "vector_max_page": 2,
    "vector_allow_fulltext": False,
    "vector_allow_similar": False,
    "vector_min_score_threshold": 0.6,
    "vector_default_window_days": 30,
    "requests_per_minute": 10,
    "max_concurrent_vector": 5,
}

# 搜索模式白名单：与前端 static/js/intelligence_search.js 实际发送值保持一致。
# 搜索分派逻辑是 else→mongo，任何未列出的取值（如 'Mongo'、空串）必须拒绝（400），
# 否则会绕过公开模式的全部限流与钳制。
VALID_SEARCH_MODES = frozenset({'mongo', 'vector_text', 'vector_similar'})

# 登录限流器键数上限：超出时淘汰最旧键，防止海量 IP+用户名组合撑爆内存（内存 DoS）
LOGIN_RATE_LIMIT_MAX_KEYS = 10000

# 深度健康检查的 original_queue 积压告警阈值
DEEP_HEALTH_QUEUE_BACKLOG_THRESHOLD = 1500


def _parse_query_datetime(text: str) -> datetime.datetime:
    """解析搜索等入参时间字符串。

    CONTRACT-3/8：date_parser 解析出的 naive datetime 一律按 UTC 处理
    （replace tzinfo=utc），与 MongoDBAccess 的 naive=UTC 存储约定对齐，
    保证 Web 层产出的查询条件均为 aware datetime。
    """
    dt = date_parser.parse(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def to_bool(value, default=False):
    """Safely convert various types to boolean.

    Handles:
    - Native bool values
    - Strings: 'true', 'false', 'yes', 'no', 'on', 'off', '1', '0'
    - Integers: 1 = True, 0 = False
    - None: returns default

    Args:
        value: Value to convert
        default: Default if conversion fails

    Returns:
        Converted boolean value
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    try:
        # Handle string representations
        return bool(strtobool(str(value).strip().lower()))
    except (ValueError, AttributeError):
        return default


# def exclude_raw_data(result: List[dict]):
#     summary_result = []
#     for data in result:
#         # In v2, we extract those fields to ArchivedDataExtraFields
#         _uuid = data.pop('UUID', None)
#         appendix = data.pop('APPENDIX', None)
#         informant = data.pop('INFORMANT', None)
#
#         # Compatible with v1 analysis result
#         if 'TAXONOMY' not in data:
#             data['TAXONOMY'] = 'N/A'
#
#         clean_data = ProcessedData.model_validate(data).model_dump(exclude_unset=True, exclude_none=True)
#
#         if _uuid: clean_data['UUID'] = _uuid
#         if appendix: clean_data['APPENDIX'] = appendix
#         if informant: clean_data['INFORMANT'] = informant
#
#         summary_result.append(clean_data)
#     return summary_result


# 获取模型中定义的所有合法字段名集合
VALID_FIELDS = set(ProcessedData.model_fields.keys())

# 游客端只需要摘要检索与阅读所需的字段。不要直接复用 ProcessedData 的
# 全部字段，否则 EVENT_TEXT、内部评分和分析原因会随公开详情接口返回。
PUBLIC_INTELLIGENCE_FIELDS = {
    'TAXONOMY',
    'SUB_CATEGORY',
    'EVENT_TITLE',
    'EVENT_BRIEF',
    'TIME',
    'LOCATION',
    'GEOGRAPHY',
    'PEOPLE',
    'ORGANIZATION',
    'IMPACT',
}


def build_public_intelligence_summary(data: dict) -> dict:
    """返回游客可见的情报摘要，并且不修改数据库查询结果。"""
    if not isinstance(data, dict):
        return {}

    public_data = {
        key: value
        for key, value in data.items()
        if key in PUBLIC_INTELLIGENCE_FIELDS and value is not None
    }

    # UUID、来源和归档时间是列表/详情定位及基本溯源所必需的非敏感字段。
    if data.get('UUID'):
        public_data['UUID'] = data['UUID']
    if data.get('INFORMANT'):
        public_data['INFORMANT'] = data['INFORMANT']

    appendix = data.get('APPENDIX')
    if isinstance(appendix, dict):
        public_appendix = {}
        for key in (
            '__TIME_PUB__',
            '__TIME_ARCHIVED__',
        ):
            if appendix.get(key) is not None:
                public_appendix[key] = appendix[key]
        if public_appendix:
            public_data['APPENDIX'] = public_appendix

    return public_data

def exclude_raw_data(result: List[dict]):
    summary_result = []
    for data in result:
        # 查询结果可能来自缓存或被后续逻辑复用，不能在这里原地 pop。
        data = dict(data)
        _uuid = data.pop('UUID', None)
        appendix = data.pop('APPENDIX', None)
        informant = data.pop('INFORMANT', None)

        if 'TAXONOMY' not in data:
            data['TAXONOMY'] = 'N/A'

        # 核心清洗逻辑：只保留定义在 VALID_FIELDS 中，并且值不为 None 的数据
        # 这一步完全等价于 model_dump(exclude_unset=True, exclude_none=True) 的清洗效果，且0报错风险
        clean_data = {
            k: v for k, v in data.items()
            if k in VALID_FIELDS and v is not None
        }

        # 补回前面抽出的特殊字段
        if _uuid: clean_data['UUID'] = _uuid
        if appendix: clean_data['APPENDIX'] = appendix
        if informant: clean_data['INFORMANT'] = informant

        summary_result.append(clean_data)
    return summary_result


def normalize_prompt_version(raw: str) -> int:
    """
    从 raw 中提取第一个连续数字段作为版本号（int）。
    e.g. 'v20' -> 20, 'prompt_v20-beta' -> 20, '20' -> 20
    """
    if raw is None:
        raise ValueError("prompt_version is required")

    m = re.search(r'(\d+)', str(raw))
    if not m:
        raise ValueError(f"invalid prompt_version: {raw}")

    return int(m.group(1))


def post_collected_intelligence(url: str, data: CollectedData, timeout=10) -> dict:
    """
    Post collected intelligence to IntelligenceHub (/collect).
    :param url: IntelligenceHub url (without '/collect' path).
    :param data: Collector data.
    :param timeout: Timeout in second
    :return: Requests response or {'status': 'error', 'reason': 'error description'}
    """
    if not isinstance(data, CollectedData):
        return {'status': 'error', 'reason': 'Data must be CollectedData format.'}
    return common_post(f'{url}/collect', data.model_dump(exclude_unset=True), timeout)


def post_processed_intelligence(url: str, data: ProcessedData, timeout=10) -> dict:
    # 未启用骨架：当前 Web 服务未注册 /processed 接收路由，
    # 本函数与 WebServiceAccessManager.check_processor_token / processor_tokens
    # 仅为后续扩展保留，暂无调用方，勿删除（Hub 侧 submit_archived_data 不动）。
    """
    Post processed data to IntelligenceHub (/processed).
    :param url: IntelligenceHub url (without '/processed' path).
    :param data: Processed data.
    :param timeout: Timeout in second
    :return: Requests response or {'status': 'error', 'reason': 'error description'}
    """
    if not isinstance(data, ProcessedData):
        return {'status': 'error', 'reason': 'Data must be ProcessedData format.'}
    return common_post(f'{url}/processed', data.model_dump(exclude_unset=True), timeout)


class WebServiceAccessManager:
    def __init__(self,
                 rpc_api_tokens: List[str],
                 collector_tokens: List[str],
                 processor_tokens: List[str],
                 user_manager: UserManager,
                 deny_on_empty_config: bool = False):
        # 安全加固：配置中的空字符串 token 一律拒绝（compare_digest('', '') 会误判通过），
        # 启动时发现空 token 打 warning。
        self.rpc_api_tokens = self._filter_empty_tokens(rpc_api_tokens, 'rpc_api')
        self.collector_tokens = self._filter_empty_tokens(collector_tokens, 'collector')
        # 未启用骨架：/processed 接收端未启用，processor token 仅为后续扩展保留。
        self.processor_tokens = self._filter_empty_tokens(processor_tokens, 'processor')
        self.user_manager = user_manager
        self.deny_on_empty_config = deny_on_empty_config

    @staticmethod
    def _filter_empty_tokens(tokens: List[str], name: str) -> List[str]:
        cleaned = [t for t in (tokens or []) if t]
        dropped = len(tokens or []) - len(cleaned)
        if dropped:
            logger.warning(
                f'{name} tokens 配置包含 {dropped} 个空字符串 token，已忽略（空 token 一律无效）。')
        return cleaned

    def check_rpc_api_token(self, token: str) -> bool:
        if not token:
            return False
        if not self.rpc_api_tokens:
            return not self.deny_on_empty_config
        return any(hmac.compare_digest(token, expected) for expected in self.rpc_api_tokens)

    def check_collector_token(self, token: str) -> bool:
        if not token:
            return False
        if not self.collector_tokens:
            return not self.deny_on_empty_config
        return any(hmac.compare_digest(token, expected) for expected in self.collector_tokens)

    def check_processor_token(self, token: str) -> bool:
        # 未启用骨架：/processed 接收端未启用，本方法暂无调用方。
        if not token:
            return False
        if not self.processor_tokens:
            return not self.deny_on_empty_config
        return any(hmac.compare_digest(token, expected) for expected in self.processor_tokens)

    def check_user_credential(self, username: str, password: str, client_ip) -> int or None:
        if self.user_manager:
            result, _ = self.user_manager.authenticate(username, password, client_ip)
            return result
        else:
            return 1 if not self.deny_on_empty_config else None

    @staticmethod
    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'logged_in' not in session or not session['logged_in']:
                # AJAX/JSON 请求不能被重定向到 HTML 登录页，否则前端会把登录页
                # 当作业务成功响应。页面请求仍保持浏览器友好的登录跳转。
                if request.is_json or request.accept_mimetypes.best == 'application/json':
                    return jsonify({
                        'error': 'Authentication required',
                        'code': 'AUTH_REQUIRED'
                    }), 401
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated_function


class IntelligenceHubWebService:
    def __init__(self, *,
                 intelligence_hub: IntelligenceHub,
                 access_manager: WebServiceAccessManager,
                 rss_publisher: RSSPublisher,
                 public_search_limits: Optional[Dict[str, Any]] = None,
                 monitor_api=None,
                 config: Optional[Any] = None):

        # ---------------- Parameters ----------------

        self.intelligence_hub = intelligence_hub
        self.cluster_cache_ttl_sec = 15.0
        self._cluster_response_cache = {}
        self._cluster_response_cache_lock = threading.RLock()
        self.access_manager = access_manager
        self.rss_publisher = rss_publisher
        self.wsgi_app = None
        self.monitor_api = monitor_api
        # 全局配置（供 trusted_proxies 等读取）。Startup 批次会通过构造参数或
        # set_config() 注入；未注入时各读取点（如 _get_client_ip）回退默认值，保证不炸。
        self.config = config

        # /api/sources 5 分钟内存缓存（CONTRACT-4：响应 JSON 结构不变）
        self._sources_cache_ttl_sec = 300.0
        self._sources_cache = {'created_at': 0.0, 'payload': None}
        self._sources_cache_lock = threading.Lock()

        # ---------------- Public Search Limits ----------------

        self.public_search_limits = dict(DEFAULT_PUBLIC_SEARCH_LIMITS)
        if public_search_limits:
            self.public_search_limits.update(public_search_limits)

        self._vector_search_rate_limiter = SlidingWindowRateLimiter(
            window_sec=60.0,
            max_requests=self.public_search_limits.get("requests_per_minute", 10)
        )
        # 公开 Mongo 搜索速率限制（防滥用拖库）
        self._public_mongo_rate_limiter = SlidingWindowRateLimiter(
            window_sec=60.0,
            max_requests=self.public_search_limits.get("mongo_requests_per_minute", 30)
        )
        # 登录暴力破解限制（按 IP+用户名）
        self._login_rate_limiter = SlidingWindowRateLimiter(
            window_sec=60.0,
            max_requests=10
        )
        # 人工评分提交限制（按 IP）
        self._manual_rating_rate_limiter = SlidingWindowRateLimiter(
            window_sec=60.0,
            max_requests=30
        )
        # 采集端速率限制：按 IP 每分钟最多 100 次请求
        self._collect_rate_limiter = SlidingWindowRateLimiter(
            window_sec=60.0,
            max_requests=100
        )
        self._vector_search_concurrency = TimedSemaphore(
            self.public_search_limits.get("max_concurrent_vector", 5)
        )
        self._perf_logger: PerformanceLogger = get_performance_logger()

        # ---------------- RPC Service ----------------

        # 安全加固：RPC 方法白名单。仅放行 Web 前端实际使用的安全方法，
        # 阻断 shutdown / aggregate / 所有 '_' 开头内部方法等危险调用。
        self.rpc_service = RPCService(
            rpc_stub=self.intelligence_hub,
            token_checker=self.access_manager.check_rpc_api_token,
            error_handler=self.handle_error,
            allowed_apis=[
                "get_intelligence",
                "query_intelligence",
                "vector_search_intelligence",
                "get_intelligence_summary",
                "count_documents",
                # P2 修复：移除 4 个必然失败的白名单项：
                # - get_recommendations: recommendations_manager 属性从未赋值，调用即 AttributeError
                # - statistics: 是 @property，getattr 拿到 dict 不可调用，RPC 必然 TypeError
                # - get_query_engine / get_statistics_engine: 返回引擎对象无法 JSON 序列化
                # 以上方法如有调用方，修复实现后再加回白名单。
                "submit_intelligence_manual_rating",
            ]
        )

        # ------------- Other Components -------------

        self.request_tracer = None
        # daemon=True：自我续约的 Timer 线程不阻碍主进程退出
        conn_dump_timer = threading.Timer(30.0, self.dump_request_connection_periodically)
        conn_dump_timer.daemon = True
        conn_dump_timer.start()

    def set_monitor_api(self, monitor_api):
        """Set the MonitorAPI instance so dashboard can reuse system/process/thread stats."""
        self.monitor_api = monitor_api

    def set_config(self, config):
        """注入全局配置（Startup 批次调用）。未注入时读取点回退默认值。"""
        self.config = config

    def _check_login_rate(self, key: str) -> bool:
        """带键数上限的登录限流检查。

        限流键为 IP+用户名 组合，攻击者可构造海量组合撑爆 _records 内存，
        因此在写入前将键数钳制在 LOGIN_RATE_LIMIT_MAX_KEYS 以内，超出淘汰最旧键。
        """
        limiter = self._login_rate_limiter
        with limiter._lock:
            if key not in limiter._records and len(limiter._records) >= LOGIN_RATE_LIMIT_MAX_KEYS:
                # dict 保持插入顺序，首个键即最旧键
                oldest_key = next(iter(limiter._records))
                limiter._records.pop(oldest_key, None)
        return limiter.is_allowed(key)

    def _deep_health_check(self):
        """深度健康检查（/maintenance/ping?deep=1，供 Launcher 探活重启用）。

        P0-1 修复：按实际启用的运行模式检查对应消费者，
        等待非高峰时段/预算耗尽/主动禁用 AI 不再被误判为故障。

        探活端点分工（进程存活 vs 业务就绪）：
        - /maintenance/ping（无 deep）：进程存活探活。Flask 能应答即返回
          200，供 Docker HEALTHCHECK / 外部监控使用，不做业务检查；
        - /maintenance/ping?deep=1：业务就绪探活。检查消费者线程、
          后处理线程、MongoDB 可用性，异常返回 503 供 Launcher 决定重启。

        检查项（全部本地操作，短超时，不调用任何付费 API）：
        1. 消费者（按模式）：legacy→AI 线程存活；drainer→消费线程存活；
           dry_run/collect_only→无消费者是预期状态（跳过）；
        2. post_process 线程（归档必需，任何模式都必须存活）；
        3. MongoDB ping（短超时；客户端构造时已带 serverSelectionTimeoutMS）；
        4. 队列积压（超过阈值说明消费能力异常）；
        5. 信息项：AI 调用闸门状态与向量功能状态（不参与健康判定）。
        """
        hub = self.intelligence_hub
        checks = {}
        healthy = True

        # ---- 1) 消费者检查（按运行模式） ----
        try:
            mode = hub.consumer_mode()
        except Exception:
            mode = None
        if not isinstance(mode, str):
            # 兼容旧实例/测试 stub：按属性推断
            api_enabled = bool(getattr(hub, 'api_enabled', False))
            api_dry_run = bool(getattr(hub, 'api_dry_run', True))
            use_drainer = bool(getattr(hub, 'use_off_peak_drainer', False))
            if api_enabled and use_drainer and getattr(hub, 'off_peak_drainer', None) is not None:
                mode = 'off_peak_drainer'
            elif api_enabled and not api_dry_run:
                mode = 'legacy_ai_worker'
            elif api_enabled and api_dry_run:
                mode = 'dry_run'
            else:
                mode = 'collect_only'
        checks['mode'] = mode

        if mode == 'legacy_ai_worker':
            threads = getattr(hub, '_analysis_threads', None) or []
            alive = sum(1 for t in threads if t.is_alive())
            checks['ai_workers'] = {'total': len(threads), 'alive': alive}
            if not threads or alive < len(threads):
                healthy = False
        elif mode == 'off_peak_drainer':
            drainer = getattr(hub, 'off_peak_drainer', None)
            alive = bool(drainer is not None and drainer.is_running())
            # 等待非高峰/预算耗尽不是故障（信息项见 ai_call_gate）
            checks['off_peak_drainer'] = {'alive': alive}
            if not alive:
                healthy = False
        else:
            # collect_only / dry_run：无 AI 消费者是预期状态（P0-1 修复核心）
            checks['ai_workers'] = {'total': 0, 'alive': 0, 'expected': 'none'}

        # ---- 2) 后处理线程（归档链路必需） ----
        ppt = getattr(hub, 'post_process_thread', None)
        ppt_alive = bool(ppt is not None and ppt.is_alive())
        checks['post_process_thread'] = {'alive': ppt_alive}
        if not ppt_alive:
            healthy = False

        # ---- 3) MongoDB 可用性（短超时 ping，复用已建连接） ----
        mongo_ok = True
        mongo_err = None
        db_cache = getattr(hub, 'mongo_db_cache', None)
        if db_cache is not None:
            try:
                client = getattr(db_cache, 'client', None)
                if client is not None:
                    client.admin.command('ping', serverSelectionTimeoutMS=3000)
                else:
                    mongo_ok = False
                    mongo_err = 'mongo client unavailable'
            except Exception as e:
                mongo_ok = False
                mongo_err = str(e)
        else:
            mongo_ok = False
            mongo_err = 'mongo_db_cache not configured'
        checks['mongodb'] = {'ok': mongo_ok, 'error': mongo_err}
        if not mongo_ok:
            healthy = False

        # ---- 4) 队列积压 ----
        try:
            backlog = hub.original_queue.qsize()
        except Exception:
            backlog = -1
        checks['queues'] = {
            'original_queue': backlog,
            'backlog_threshold': DEEP_HEALTH_QUEUE_BACKLOG_THRESHOLD,
        }
        if backlog < 0 or backlog > DEEP_HEALTH_QUEUE_BACKLOG_THRESHOLD:
            healthy = False

        # ---- 5) 信息项（不参与健康判定） ----
        # AI 调用闸门：等待非高峰/预算耗尽/DRY_RUN/禁用均为预期状态
        try:
            gate = hub.ai_call_gate_status()
            # 仅接受全部值为 JSON 基本类型的 dict（防 stub/MagicMock 序列化失败）
            if isinstance(gate, dict) and all(
                    isinstance(v, (str, bool, int, float, type(None)))
                    for v in gate.values()):
                checks['ai_call_gate'] = gate
            else:
                checks['ai_call_gate'] = {'reason': 'unknown'}
        except Exception:
            checks['ai_call_gate'] = {'reason': 'unknown'}
        # 向量功能四态（P2-9）：disabled/initializing/degraded 不影响核心
        # Mongo 查询链路，不参与健康判定
        try:
            vs = hub.vector_status
            checks['vector_status'] = vs if isinstance(vs, str) else 'unknown'
        except Exception:
            checks['vector_status'] = 'unknown'

        payload = {
            'status': 'ok' if healthy else 'unhealthy',
            'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'checks': checks,
        }
        return jsonify(payload), (200 if healthy else 503)

    @staticmethod
    def _serialize_for_json(obj):
        """Recursively convert Enum (and other non-JSON-serializable) values to JSON-safe types."""
        from enum import Enum
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: IntelligenceHubWebService._serialize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [IntelligenceHubWebService._serialize_for_json(v) for v in obj]
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return obj

    # ---------------------------------------------------- Routers -----------------------------------------------------

    def register_routers(self, app: Flask):

        self.wsgi_app = app
        self.request_tracer = RequestTracer(app)

        # --------------------------------------------------- Config --------------------------------------------------

        class CustomJSONEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, datetime.datetime):
                    # CONTRACT-3：API JSON 中 datetime 序列化为带偏移的 ISO 8601，
                    # 前端按 ISO 解析（原 strftime 无时区信息）。
                    return obj.isoformat()
                # TODO: Add more data type support.
                return super().default(obj)

        # Flask 3.x 兼容：使用 json_provider_class 替代已弃用的 json_encoder
        try:
            from flask.json.provider import DefaultJSONProvider

            class CustomJSONProvider(DefaultJSONProvider):
                def default(self, obj):
                    if isinstance(obj, datetime.datetime):
                        # CONTRACT-3：API JSON 中 datetime 序列化为带偏移的 ISO 8601，
                        # 前端按 ISO 解析（原 strftime 无时区信息）。
                        return obj.isoformat()
                    return super().default(obj)

            app.json = CustomJSONProvider(app)
            logger.info("使用 Flask 3.x 兼容的 json_provider_class")
        except ImportError:
            # Flask 2.x 兼容：回退到 json_encoder
            app.json_encoder = CustomJSONEncoder
            logger.info("使用 Flask 2.x 兼容的 json_encoder（已弃用）")

        # -------------------------------------------------- Security --------------------------------------------------

        @app.before_request
        def refresh_session():
            # 仅登录用户标记 session.modified 以维持会话滑动续期
            # （配合登录时设置的 session.permanent + Flask 默认
            # SESSION_REFRESH_EACH_REQUEST=True，7 天滑动续期语义不变）。
            # 匿名请求不再强制写 Session，避免每个响应都携带
            # Set-Cookie 破坏缓存语义。
            if session.get('logged_in', False):
                session.modified = True

        @app.after_request
        def add_security_headers(response):
            # 基础安全响应头统一在此设置
            response.headers.setdefault('X-Content-Type-Options', 'nosniff')
            response.headers.setdefault('X-Frame-Options', 'DENY')
            response.headers.setdefault('Referrer-Policy', 'no-referrer')
            # 暂不加 Content-Security-Policy：当前模板使用 CDN 资源与内联脚本，
            # 直接启用会破坏页面渲染，待前端批次改造（外链脚本/nonce）后再启用。
            return response

        @app.context_processor
        def inject_security_context():
            return {
                'csrf_token': get_csrf_token(),
                'is_logged_in': bool(session.get('logged_in', False)),
            }

        @app.route('/login', methods=['GET', 'POST'])
        def login():
            if request.method == 'POST':
                # P1 修复：client_ip 统一走可信代理白名单解析，不直接采信可伪造的 XFF 头
                client_ip = _get_client_ip()

                if not csrf_token_is_valid():
                    abort(403, description='页面已过期，请刷新后重试。')

                username = request.form.get('username', '').strip()
                password = request.form.get('password', '')
                # 日志注入防护：username 写入日志前过滤 CR/LF
                log_username = re.sub(r'[\r\n]', '', username)
                if not username or not password:
                    flash('请输入用户名和密码', 'error')
                    flash(username, 'username')
                    return redirect(url_for('login'))

                # 登录限流：同一 IP+用户名 每分钟最多 10 次尝试（键数有上限，防内存 DoS）
                if not self._check_login_rate(f"{client_ip}:{username}"):
                    logger.warning(f"Login rate limit exceeded - IP: {client_ip}, Username: {log_username}")
                    flash('尝试次数过多，请稍后再试', 'error')
                    flash(username, 'username')
                    return redirect(url_for('login'))

                user_id = self.access_manager.check_user_credential(username, password, client_ip)

                if user_id:
                    session['logged_in'] = True
                    session['user_id'] = user_id
                    session['username'] = username
                    session['login_ip'] = client_ip
                    session.permanent = True
                    return redirect(url_for('show_post', article='index'))
                else:
                    logger.info(f"Login fail - IP: {client_ip}, Username: {log_username}")
                    # CONTRACT-6：登录失败重定向不再把 username 放 URL query，
                    # 改用 flash（category 'username'）传递，login 模板由前端批次同步读取。
                    flash('用户名或密码错误', 'error')
                    flash(username, 'username')
                    return redirect(url_for('login'))
            return render_template('login.html')

        @app.route('/logout', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def logout():
            session.clear()
            return redirect(url_for('login'))

        @app.route('/auth_check', methods=['GET'])
        def auth_check():
            ok = session.get("logged_in") is True and session.get("user_id") is not None
            status = 204 if ok else 401
            resp = make_response(("", status))
            resp.headers["Cache-Control"] = "no-store"
            return resp

        # ---------------------------------------------- Post and Article ----------------------------------------------

        @app.route('/')
        def index():
            return redirect(url_for('show_post', article='index')) \
                if session.get('logged_in') \
                else self.get_rendered_md_post('index_public') or abort(404)

        @app.route('/post/<path:article>')
        @WebServiceAccessManager.login_required
        def show_post(article):
            """
            Render a Markdown article as HTML with caching mechanism.

            Args:
                article: URL path of the Markdown file (relative to 'posts' directory)

            Returns:
                Rendered HTML template or 404 error
            """
            return self.get_rendered_md_post(article) or abort(404)

        # -------------------------------------------- API and Open Service --------------------------------------------

        @app.route('/api', methods=['POST'])
        @WebServiceAccessManager.login_required
        def rpc_api():
            try:
                response = self.rpc_service.handle_flask_request(request)
                return response
            except Exception as e:
                # RPC 派发异常：写日志（含堆栈）而非 print（stdout 不进日志文件）；
                # 对外返回结构化错误 + 500，不回显内部异常文本。
                # 成功路径的响应格式（handle_flask_request 返回的序列化字符串）保持不变；
                # 客户端 RPCProxy 对非 RpcMessage 响应校验失败后按既有路径返回空串，
                # 与旧"空 body + 200"时客户端拿到空串的行为等价。
                logger.error(f'rpc_api() dispatch fail: {e}', exc_info=True)
                return jsonify({'error': 'internal'}), 500

        @app.route('/collect', methods=['POST'])
        def collect_api():
            try:
                # 安全加固：添加速率限制，防止滥用
                client_ip = _get_client_ip()

                if not self._collect_rate_limiter.is_allowed(client_ip):
                    logger.warning(f'Collect API rate limit exceeded - IP: {client_ip}')
                    return jsonify({
                        'resp': 'rate_limit_exceeded',
                        'status': 'error',
                        'code': 'rate_limited',
                        'uuid': '',
                        'error': '请求过于频繁，请稍后再试'
                    }), 429

                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    return jsonify({
                        'resp': 'error',
                        'status': 'error',
                        'code': 'validation',
                        'uuid': ''
                    })

                # 安全加固：token 校验先于 UUID 等数据校验，
                # 防止未认证请求通过错误响应差异探测数据格式。
                collector_token = data.get('token', '')
                if not self.access_manager.check_collector_token(collector_token):
                    # token 失效返回 401，便于采集端识别并进入重试/告警路径
                    # 日志绝不记录 token 明文
                    logger.warning('Post intelligence with invalid token.')
                    return jsonify({
                        'resp': 'invalid token',
                        'status': 'error',
                        'uuid': data.get('UUID', '')
                    }), 401

                # UUID 存在性与格式校验，不合法按 validation 终态处理（CONTRACT-1）
                data_uuid = str(data.get('UUID', '') or '')
                try:
                    uuid.UUID(data_uuid)
                except (ValueError, AttributeError, TypeError):
                    return jsonify({
                        'resp': 'error',
                        'status': 'error',
                        'code': 'validation',
                        'uuid': data_uuid
                    })

                result = self.intelligence_hub.submit_collected_data(data)
                if result is True:
                    # 统一契约：同时返回 resp 与 status，兼容新旧采集端
                    return jsonify({
                        'resp': 'queued',
                        'status': 'ok',
                        'uuid': data.get('UUID', '')
                    })

                # IntelligenceHub.Error：按错误内容映射终态 code（CONTRACT-1），
                # 客户端批次据此识别 duplicate/validation/queue_full/internal。
                error_text = '; '.join(getattr(result, 'error_list', None) or [])
                if 'duplicated' in error_text:
                    code, http_status = 'duplicate', 200
                elif 'Queue full' in error_text:
                    code, http_status = 'queue_full', 503
                elif getattr(result, 'exception', None) is not None:
                    code, http_status = 'internal', 500
                else:
                    code, http_status = 'validation', 200
                return jsonify({
                    'resp': 'error',
                    'status': 'error',
                    'code': code,
                    'uuid': data.get('UUID', '')
                }), http_status
            except Exception as e:
                logger.error(f'collect_api() fail: {str(e)}')
                return jsonify({
                    'resp': 'error',
                    'status': 'error',
                    'code': 'internal',
                    'uuid': ''
                }), 500

        @app.get("/api/prompts/<prompt_version>")
        def get_prompt(prompt_version):
            if not session.get('logged_in', False):
                return jsonify({
                    'error': 'Authentication required',
                    'code': 'AUTH_REQUIRED'
                }), 401

            if not prompt_version:
                abort(400, "prompt_version required")

            try:
                version_digit = normalize_prompt_version(prompt_version)
            except ValueError as e:
                abort(400, str(e))

            prompt_text = PROMPT_TABLE_V2.get(version_digit)
            if prompt_text is None:
                prompt_text = f"[Prompt v{version_digit}] Not configured."

            resp = Response(prompt_text, content_type="text/plain; charset=utf-8")
            resp.headers["X-Prompt-Version-Normalized"] = str(version_digit)
            return resp

        @app.route('/api/intelligence/<string:intelligence_uuid>', methods=['GET'])
        def intelligence_viewer_json(intelligence_uuid: str):
            try:
                # 安全加固：检查登录状态，未登录仅返回脱敏数据
                is_logged_in = bool(session.get('logged_in', False))
                # 公开端点限流：未登录用户按 IP 限流（复用公开 Mongo 限流器）
                if not is_logged_in and not self._public_mongo_rate_limiter.is_allowed(_get_client_ip()):
                    return jsonify({"error": "请求过于频繁，请稍后再试。"}), 429
                intelligence = self.intelligence_hub.get_intelligence(intelligence_uuid)
                if not intelligence:
                    return jsonify({"error": "Intelligence not found"}), 404

                # 未登录用户仅返回游客白名单摘要，不能把完整 ProcessedData 直接公开。
                if not is_logged_in:
                    intelligence = build_public_intelligence_summary(intelligence)

                return jsonify({
                    "success": True,
                    "data": intelligence
                }), 200
            except Exception as e:
                logger.exception(f"intelligence_viewer_json error for UUID {intelligence_uuid}")
                return jsonify({"error": "Server error"}), 500

        @app.route('/manual_rate', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def submit_rating():
            try:
                # 人工评分提交限流：按 IP 每分钟最多 30 次
                if not self._manual_rating_rate_limiter.is_allowed(_get_client_ip()):
                    return jsonify({
                        'status': 'error',
                        'message': '提交过于频繁，请稍后再试'
                    }), 429

                data = request.get_json(silent=True) or {}
                _uuid = data.get('uuid')
                ratings = data.get('ratings')

                if not isinstance(_uuid, str) or not _uuid.strip():
                    return jsonify({
                        'status': 'error',
                        'message': 'uuid is required',
                        'code': 'INVALID_UUID'
                    }), 400
                if not isinstance(ratings, dict):
                    return jsonify({
                        'status': 'error',
                        'message': 'ratings must be an object',
                        'code': 'INVALID_RATINGS'
                    }), 400

                normalized_ratings = {}
                for key, value in ratings.items():
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        return jsonify({
                            'status': 'error',
                            'message': 'ratings must contain numeric values',
                            'code': 'INVALID_RATING_VALUE'
                        }), 400
                    if not 0 <= number <= 10:
                        return jsonify({
                            'status': 'error',
                            'message': 'rating values must be between 0 and 10',
                            'code': 'RATING_OUT_OF_RANGE'
                        }), 400
                    normalized_ratings[str(key)] = number

                self.intelligence_hub.submit_intelligence_manual_rating(_uuid.strip(), normalized_ratings)

                return jsonify({'status': 'success', 'message': 'Ratings saved'})
            except Exception:
                logger.exception('submit_rating failed')
                return jsonify({
                    'status': 'error',
                    'message': '评分保存失败，请稍后重试',
                    'code': 'RATING_SAVE_FAILED'
                }), 500

        # ---------------------------------------------------- Pages ---------------------------------------------------

        @app.route('/rssfeed.xml', methods=['GET'])
        def rssfeed_api():
            # 公开访问（与公开搜索同等暴露面），数据走公开字段白名单路径。
            try:
                is_logged_in = bool(session.get('logged_in', False))
                # 公开端点限流：未登录用户按 IP 限流（复用公开 Mongo 限流器），
                # 防止匿名高频请求 /rssfeed.xml?count=200 持续触发 MongoDB 查询。
                if not is_logged_in and not self._public_mongo_rate_limiter.is_allowed(_get_client_ip()):
                    return jsonify({'error': '请求过于频繁，请稍后再试。'}), 429

                count = request.args.get('count', default=100, type=int)
                threshold = request.args.get('threshold', default=6, type=int)
                # 公开端点钳制拉取上限，防止拖库
                count = max(1, min(count or 100, 200))
                # 公开端点钳制 threshold 下限为 4：防止未登录用户以极低阈值
                # 批量拉取大量低分数据；登录用户不受限制。
                if not is_logged_in:
                    threshold = max(threshold, 4)

                intelligences, _ = self.intelligence_hub.query_intelligence(
                    threshold=threshold, skip=0, limit=count)

                try:
                    # 仅输出游客白名单字段（不含 EVENT_TEXT 全文、内部评分等）
                    public_docs = [build_public_intelligence_summary(doc) for doc in intelligences]
                    rss_items = self._articles_to_rss_items(public_docs)
                    feed_xml = self.rss_publisher.generate_feed(
                        'IIS',
                        '/intelligences',
                        'IIS Processed Intelligence',
                        rss_items)
                    return Response(feed_xml, mimetype='application/rss+xml')
                except Exception:
                    logger.exception("Rss Feed API error")
                    return Response('RSS 生成失败，请稍后再试。', status=500)
            except Exception:
                logger.exception('rssfeed_api() error')
                return Response('RSS 生成失败，请稍后再试。', status=500)

        @app.route('/intelligences', methods=['GET'])
        def intelligences_view():
            return render_template('intelligence_list.html')

        @app.route('/recommendations', methods=['GET'])
        def intelligences_recommendations_page():
            # recommendations = self.intelligence_hub.get_recommendations()
            # return default_article_list_render(
            #     recommendations, offset=0, count=len(recommendations), total_count=len(recommendations))
            return ''

        @app.route('/intelligences/clusters', methods=['GET'])
        def intelligences_clusters_view():
            return render_template('intelligence_cluster_list.html')

        @app.route('/intelligences/search', methods=['GET'])
        def intelligences_search_page():
            is_public = not session.get('logged_in', False)
            return render_template(
                'intelligence_search.html',
                public_mode=is_public,
                public_limits=self.public_search_limits if is_public else {}
            )

        @app.route('/intelligence/graph/view', methods=['GET'])
        @WebServiceAccessManager.login_required
        def intelligence_graph_page():
            # 如果用户通过 URL 参数带了 UUID (例如从详情页跳转过来)，直接传给前端
            seed_uuid = request.args.get('uuid', '')
            return render_template('intelligence_graph.html', seed_uuid=seed_uuid)

        # ----------------------------------------------------------------------------------------

        def _get_client_ip() -> str:
            """从请求头中提取真实客户端 IP。

            P1-四 修复：仅当直连对端（remote_addr）在可信代理白名单内时才采信
            X-Forwarded-For / X-Real-IP 头；否则忽略可伪造的头，直接使用 remote_addr。
            白名单来源：intelligence_hub_web_service.trusted_proxies（list[str]），
            兼容旧的 service 子键；未配置时默认仅信任本机回环（同机反代场景）。
            """
            remote = request.remote_addr or 'unknown'
            cfg = getattr(self, 'config', None)
            trusted = None
            if cfg:
                trusted = cfg.get('intelligence_hub_web_service.trusted_proxies', None)
                if trusted is None:
                    trusted = cfg.get('intelligence_hub_web_service.service.trusted_proxies', None)
            if not isinstance(trusted, list):
                trusted = ['127.0.0.1', '::1']

            if remote in trusted:
                forwarded = request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                if forwarded:
                    return forwarded
                real_ip = request.headers.get('X-Real-IP', '').strip()
                if real_ip:
                    return real_ip
            return remote

        @app.route('/intelligences/query', methods=['GET', 'POST'])
        def intelligences_query_api():
            client_ip = _get_client_ip()
            is_logged_in = bool(session.get('logged_in', False))
            is_public = not is_logged_in
            perf_extra = {
                'client_ip': client_ip,
                'is_public': is_public,
                'path': '/intelligences/query',
            }

            try:
                # 1. 获取参数
                params = _get_combined_params()
                perf_extra['search_mode'] = params.get('search_mode', 'mongo')
                perf_extra['page'] = params.get('page', 1)
                perf_extra['per_page'] = params.get('per_page', 10)

                # 1.5 search_mode 白名单校验：非法取值（如 'Mongo'、空串）直接 400。
                # 分派逻辑是 else→mongo，不拦截会绕过下方全部公开限制。
                if params.get('search_mode') not in VALID_SEARCH_MODES:
                    return jsonify({'error': '不支持的 search_mode。'}), 400

                # 2. 应用未登录用户限制
                if is_public:
                    limit_err = _apply_public_search_limits(params, client_ip)
                    if limit_err:
                        self._perf_logger.record(
                            'search_query_rejected',
                            status='rejected',
                            error=limit_err,
                            extra=perf_extra,
                        )
                        return jsonify({'error': limit_err}), 429

                # 3. 全局向量搜索并发控制（仅对未登录用户生效）
                sem_acquired = False
                if is_public and params['search_mode'].startswith('vector'):
                    if not self._vector_search_concurrency.acquire(timeout=0):
                        self._perf_logger.record(
                            'search_query_rejected',
                            status='rejected',
                            error='Too many concurrent vector searches, please retry later.',
                            extra=perf_extra,
                        )
                        return jsonify({
                            'error': '服务器当前向量搜索压力过大，请稍后再试。',
                            'retry_after': 5,
                        }), 503
                    sem_acquired = True

                try:
                    # 4. 执行业务逻辑并记录性能
                    with self._perf_logger.timed('search_query', **perf_extra):
                        data = _perform_search_logic(params, is_public=is_public)

                    perf_extra['result_count'] = len(data.get('results', []))
                    perf_extra['total'] = data.get('total', 0)
                    return jsonify(data)

                finally:
                    if sem_acquired:
                        self._vector_search_concurrency.release()

            except ValueError as e:
                # 参数解析类错误（如非法时间格式）返回 400，不回显原始输入
                return jsonify({'error': '参数格式错误，请检查时间等参数格式。'}), 400
            except Exception as e:
                logger.exception("intelligences_query_api error")
                self._perf_logger.record(
                    'search_query',
                    status='error',
                    error=str(e),
                    extra=perf_extra,
                )
                # 不回显内部异常文本，防止反射型 XSS 链（配合前端 showError 的 innerHTML 插入）
                return jsonify({
                    'error': '服务器内部错误，请稍后再试或联系管理员。'
                }), 500

        def _apply_public_search_limits(p: Dict[str, Any], client_ip: str) -> Optional[str]:
            """
            对未登录用户应用搜索限制。返回错误信息或 None。

            游客模式统一仅允许按 Archive Time（归档时间）搜索，忽略 Publish Time，
            避免 Publish Time 与 Archive Time 双重过滤导致结果为空。

            P0 修复：限流、强制归档窗口、per_page 钳制与页码上限移出 mode 分支，
            对所有模式无条件执行。调用前已经过 search_mode 白名单校验，
            此处只需区分 mongo / vector 两个族。
            """
            limits = self.public_search_limits
            mode = p.get('search_mode', 'mongo')
            is_vector = mode.startswith('vector')

            # 速率限制（所有模式无条件执行）：向量与 Mongo 模式分别限流
            if is_vector:
                if not self._vector_search_rate_limiter.is_allowed(client_ip):
                    return (
                        f"未登录用户向量搜索过于频繁，"
                        f"每分钟最多 {limits.get('requests_per_minute', 10)} 次，请稍后再试。"
                    )
            else:
                if not self._public_mongo_rate_limiter.is_allowed(client_ip):
                    return (
                        f"未登录用户普通搜索过于频繁，"
                        f"每分钟最多 {limits.get('mongo_requests_per_minute', 30)} 次，请稍后再试。"
                    )

            # 游客模式禁止使用 Publish Time，统一使用 Archive Time
            p['start_time'] = ''
            p['end_time'] = ''

            # 强制最近 N 天归档窗口（所有模式无条件执行，忽略游客传入值）。
            # 输出带偏移的 ISO 8601，解析侧保留时区，避免丢失本地时区信息。
            window_days = limits.get(
                'vector_default_window_days' if is_vector else 'mongo_default_window_days', 30)
            now = get_aware_time()
            start = now - datetime.timedelta(days=window_days)
            p['archive_start_time'] = start.isoformat()
            p['archive_end_time'] = now.isoformat()

            # per_page 钳制与页码上限（所有模式无条件执行）
            if is_vector:
                max_per_page = limits.get('vector_max_per_page', 10)
                max_page = limits.get('vector_max_page', 2)
                mode_label = '向量搜索'
            else:
                max_per_page = limits.get('mongo_max_per_page', 20)
                max_page = limits.get('mongo_max_page', 5)
                mode_label = '普通搜索'
            if p.get('per_page', 10) > max_per_page:
                p['per_page'] = max_per_page
            if p.get('page', 1) > max_page:
                return f"未登录用户{mode_label}仅支持前 {max_page} 页，请登录后查看更多。"

            # 向量模式附加限制
            if is_vector:
                # 禁止相似推荐
                if mode == 'vector_similar' and not limits.get('vector_allow_similar', False):
                    return "未登录用户暂不支持相似推荐，请登录后使用。"

                # 禁止全文库
                if p.get('in_fulltext') and not limits.get('vector_allow_fulltext', False):
                    p['in_fulltext'] = False
                    p['in_summary'] = True

                # 强制最小相似度阈值
                min_threshold = limits.get('vector_min_score_threshold', 0.6)
                if p.get('score_threshold_min', 0) < min_threshold:
                    p['score_threshold_min'] = min_threshold

                # 限制召回总量
                max_top_n = limits.get('vector_max_top_n', 20)
                requested_top_n = min(p['page'] * p['per_page'], VECTOR_MAX_TOP_N)
                p['_effective_top_n'] = min(requested_top_n, max_top_n)

            return None

        def _get_combined_params() -> Dict[str, Any]:
            """
            统一参数解析与清洗器。
            优先级：URL Query (GET) > JSON Body > Form Data。

            Returns:
                Dict[str, Any]: 包含清洗和类型转换后的参数字典。

            Param Details:
                -------------------- 模式选择 (Mode) --------------------
                search_mode (str): 搜索策略，默认为 'mongo'。
                    - 'mongo': [普通搜索] 仅使用 MongoDB 字段精准/模糊筛选。
                    - 'vector_text': [文本向量] 根据 'keywords' 进行自然语言语义搜索。
                    - 'vector_similar': [相似推荐] 根据 'reference' (UUID) 寻找内容相似的文章。

                --------------------- 通用 (General) ---------------------
                page (int): 页码，默认为 1。
                per_page (int): 每页数量，默认为 10 (最大限制 100)。
                keywords (str): 搜索文本/关键词。
                    - Mongo模式: 视底层实现用于文本匹配。
                    - Vector模式: 作为语义搜索的 Embedding 输入。
                start_time (str): 时间下限 (ISO 8601 格式字符串)。
                end_time (str): 时间上限 (ISO 8601 格式字符串)。
                    - Mongo模式: 对应数据库中的 period 或 archived_time。
                    - Vector模式: 对应向量库 metadata 中的 timestamp。
                threshold (float): 通用评分/过滤阈值，默认为 0。

                --------------------- Mongo 专属 ------------------------
                peoples (List[str]): 人物实体列表 (逗号分隔自动转列表)。
                locations (List[str]): 地点实体列表。
                organizations (List[str]): 组织实体列表。
                    * 逻辑说明: 单字段内为 OR 关系 (包含任一即匹配)，字段间为 AND 关系。

                --------------------- Vector 专属 -----------------------
                in_summary (bool): 是否在摘要库中召回，默认为 True。
                in_fulltext (bool): 是否在全文库中召回，默认为 False。
                score_threshold (float): 向量相似度截断阈值，默认为 0.5。
                reference (str): 目标文章 UUID。仅在 'vector_similar' 模式下必填。
                """
            combined = {}

            # 1. 基础数据源获取
            # 如果是 POST，尝试获取 Body
            if request.method == 'POST':
                json_data = request.get_json(silent=True)
                if json_data:
                    combined.update(json_data)
                else:
                    combined.update(request.form)

            # 2. URL 参数覆盖 (优先级最高，保证分享链接的有效性)
            # request.args 是 ImmutableMultiDict，转为 dict
            combined.update(request.args.to_dict())

            def _split(v: Any) -> List[str]:
                if not v: return []
                if isinstance(v, list): return v
                return [x.strip() for x in v.split(',') if x.strip()]

            # 3. 参数构造
            # 兼容旧版 threshold / score_threshold
            threshold_min = float(combined.get('threshold_min', combined.get('threshold', 0)))
            threshold_max = float(combined.get('threshold_max', 10))
            score_threshold_min = float(combined.get('score_threshold_min', combined.get('score_threshold', VECTOR_DEFAULT_SCORE_THRESHOLD)))
            score_threshold_max = float(combined.get('score_threshold_max', 1.0))

            params = {
                'search_mode': combined.get('search_mode', 'mongo'),
                'page': max(int(combined.get('page', 1)), 1),
                'per_page': max(int(combined.get('per_page', 10)), 1),
                # 保持原始字符串，在具体逻辑中再转 datetime，避免在此处 crash
                'start_time': combined.get('start_time', ''),
                'end_time': combined.get('end_time', ''),
                'archive_start_time': combined.get('archive_start_time', ''),
                'archive_end_time': combined.get('archive_end_time', ''),
                'keywords': combined.get('keywords', '').strip(),  # 去除首尾空格

                # Mongo 筛选字段
                'peoples': _split(combined.get('peoples', '')),
                'locations': _split(combined.get('locations', '')),
                'organizations': _split(combined.get('organizations', '')),
                'geography': _split(combined.get('geography', '')),
                'informant_domains': _split(combined.get('informant_domains', '')),
                'threshold_min': threshold_min,
                'threshold_max': threshold_max,

                # Vector 字段
                'in_summary': to_bool(combined.get('in_summary'), default=True),
                'in_fulltext': to_bool(combined.get('in_fulltext'), default=False),
                'score_threshold_min': score_threshold_min,
                'score_threshold_max': score_threshold_max,
                'reference': combined.get('reference', ''),
            }

            # 限制每页数量范围，防止 per_page=0 拖全表或超限请求
            if params['per_page'] > 100:
                params['per_page'] = 100

            return params

        def _perform_search_logic(params: Dict[str, Any], is_public: bool = False) -> Dict[str, Any]:
            mode = params['search_mode']

            if mode.startswith('vector'):
                results, total = _do_vector_search(params)
            else:
                results, total = _do_mongo_search(params)

            if not results:
                return {'results': [], 'total': 0}
            else:
                if is_public:
                    summary_result = [build_public_intelligence_summary(article) for article in results]
                else:
                    summary_result = exclude_raw_data(results)
                return {'results': summary_result, 'total': total}

        def _do_mongo_search(p: dict) -> Tuple[List[dict], int]:
            """走 Mongo 过滤"""
            query = {}
            if p['start_time'] and p['end_time']:
                # 使用 dateutil 解析，兼容 Python 3.10（fromisoformat 不支持空格分隔）；
                # naive 结果一律按 UTC 处理（CONTRACT-3/8）
                query['period'] = (
                    _parse_query_datetime(p['start_time']),
                    _parse_query_datetime(p['end_time'])
                )
            if p['archive_start_time'] and p['archive_end_time']:
                query['archive_period'] = (
                    _parse_query_datetime(p['archive_start_time']),
                    _parse_query_datetime(p['archive_end_time'])
                )
            for field in ('locations', 'peoples', 'organizations', 'geography', 'keywords', 'informant_domains'):
                if p[field]:
                    query[field] = p[field]

            if p.get('threshold_min', 0) > 0:
                query['threshold'] = p['threshold_min']
            if p.get('threshold_max', 10) < 10:
                query['threshold_max'] = p['threshold_max']

            skip = (p['page'] - 1) * p['per_page']
            return self.intelligence_hub.query_intelligence(
                skip=skip, limit=p['per_page'], **query)

        def _do_vector_search(p: dict) -> Tuple[List[dict], int]:
            """走向量召回 + 内存分页"""

            text = ''
            if p['search_mode'] == 'vector_text':
                text = p.get('keywords', '')
            elif p['search_mode'] == 'vector_similar':
                if ref_uuids := p.get('reference', ''):
                    intelligence = self.intelligence_hub.get_intelligence(ref_uuids)
                    if intelligence:
                        text = IntelligenceVectorDBEngine.build_search_text(intelligence, 'summary')
            if not text:
                return [], 0

            top_n = p.get('_effective_top_n') or min(p['page'] * p['per_page'], VECTOR_MAX_TOP_N)
            vector_kwargs = {
                'text': text,
                'in_summary': p['in_summary'],
                'in_fulltext': p['in_fulltext'],
                'top_n': top_n,
                'score_threshold': p.get('score_threshold_min', VECTOR_DEFAULT_SCORE_THRESHOLD),
                'score_threshold_max': p.get('score_threshold_max', 1.0),
            }

            if p['start_time'] and p['end_time']:
                # 使用 dateutil 解析，兼容 Python 3.10；naive 结果一律按 UTC 处理（CONTRACT-3/8）
                vector_kwargs['event_period'] = (
                    _parse_query_datetime(p['start_time']),
                    _parse_query_datetime(p['end_time'])
                )
            if p['archive_start_time'] and p['archive_end_time']:
                vector_kwargs['archive_period'] = (
                    _parse_query_datetime(p['archive_start_time']),
                    _parse_query_datetime(p['archive_end_time'])
                )

            raw: List[Tuple[str, float, dict]] = self.intelligence_hub.vector_search_intelligence(**vector_kwargs)

            # 分页
            start = (p['page'] - 1) * p['per_page']
            end = start + p['per_page']
            page_items = raw[start:end]

            uuids = []
            score_map = {}
            for doc_id, score, _ in page_items:
                uuids.append(doc_id)
                score_map[doc_id] = score
            articles = self.intelligence_hub.get_intelligence(uuids)

            for article in articles:
                doc_id = article.get('UUID')
                article.setdefault('APPENDIX', {})[APPENDIX_VECTOR_SCORE] = score_map.get(doc_id, 0.0)

            articles.sort(key=lambda x: x['APPENDIX'][APPENDIX_VECTOR_SCORE], reverse=True)

            return articles, len(raw)

        # --------------------------------------------------------------------------------------------

        @app.get("/api/clusters/latest")
        def api_clusters_latest():
            try:
                limit = max(1, min(int(request.args.get("limit", 200)), 200))
                client_sort_by = request.args.get("sort_by", "time")
                desc = request.args.get("desc", "1") in ("1", "true", "True", "yes", "Y", "y")
                source = request.args.get("source", "online").lower().strip()
                is_public = not session.get('logged_in', False)
                # 未登录用户忽略 refresh=1（仅登录用户可强制刷新缓存）
                refresh = (request.args.get("refresh", "0") in ("1", "true", "True", "yes", "Y", "y")) \
                    and not is_public
                cache_key = ("clusters_latest", is_public, source, limit, client_sort_by, desc)

                # 公开端点限流：未登录用户按 IP 限流（复用公开 Mongo 限流器）
                if is_public and not self._public_mongo_rate_limiter.is_allowed(_get_client_ip()):
                    return jsonify({'error': '请求过于频繁，请稍后再试。'}), 429

                hub = self.intelligence_hub
                agg = getattr(hub, "aggregation_engine_summary", None)
                if not agg:
                    return jsonify({"error": "Aggregation engine not configured"}), 501

                if not refresh:
                    with self._cluster_response_cache_lock:
                        now = time.time()
                        for key, item in list(self._cluster_response_cache.items()):
                            if now - item["created_at"] > self.cluster_cache_ttl_sec:
                                self._cluster_response_cache.pop(key, None)
                        cached = self._cluster_response_cache.get(cache_key)
                        if cached and now - cached["created_at"] <= self.cluster_cache_ttl_sec:
                            payload = dict(cached["payload"])
                            payload["cache"] = {
                                "hit": True,
                                "ttl_sec": self.cluster_cache_ttl_sec,
                                "created_at": cached["created_at"],
                            }
                            return jsonify(payload), 200

                # 建立一个闭包透传 fetcher 给 engine
                def doc_fetcher(uuids):
                    return hub.get_intelligence(uuids, light_weight=True)

                doc_cleaner = (
                    (lambda docs: [build_public_intelligence_summary(doc) for doc in docs])
                    if is_public else exclude_raw_data
                )

                result = agg.build_rich_clusters_latest(
                    doc_fetcher=doc_fetcher,
                    doc_cleaner=doc_cleaner,
                    limit=limit,
                    descending=desc,
                    source=source,
                    # CONTRACT-7：路由解析的 sort_by 原样透传给引擎（引擎批次实现该形参后生效）
                    sort_by=client_sort_by
                )
                result["cache"] = {
                    "hit": False,
                    "ttl_sec": self.cluster_cache_ttl_sec,
                    "created_at": time.time(),
                }
                with self._cluster_response_cache_lock:
                    self._cluster_response_cache[cache_key] = {
                        "created_at": result["cache"]["created_at"],
                        "payload": result,
                    }

                return jsonify(result), 200

            except Exception as e:
                logger.exception("api_clusters_latest error")
                # 与搜索接口一致的策略：不回显内部异常文本
                return jsonify({"error": "服务器内部错误，请稍后再试。"}), 500

        @app.get("/api/clusters/<cluster_id>/members")
        def api_cluster_members(cluster_id: str):
            try:
                limit = max(1, min(int(request.args.get("limit", 100)), 500))
                offset = max(0, int(request.args.get("offset", 0)))
                sort_by = request.args.get("sort_by", "time")
                desc = request.args.get("desc", "1") in ("1", "true", "True")
                source = request.args.get("source", "online").lower().strip()
                refresh = request.args.get("refresh", "0") in ("1", "true", "True", "yes", "Y", "y")
                is_public = not session.get('logged_in', False)
                cache_key = ("cluster_members", is_public, source, cluster_id, offset, limit, sort_by, desc)

                # 公开端点限流：未登录用户按 IP 限流（复用公开 Mongo 限流器）
                if is_public and not self._public_mongo_rate_limiter.is_allowed(_get_client_ip()):
                    return jsonify({'error': '请求过于频繁，请稍后再试。'}), 429

                hub = self.intelligence_hub
                agg = getattr(hub, "aggregation_engine_summary", None)
                if not agg:
                    return jsonify({"error": "Aggregation engine not configured"}), 501

                if not refresh:
                    with self._cluster_response_cache_lock:
                        now = time.time()
                        for key, item in list(self._cluster_response_cache.items()):
                            if now - item["created_at"] > self.cluster_cache_ttl_sec:
                                self._cluster_response_cache.pop(key, None)
                        cached = self._cluster_response_cache.get(cache_key)
                        if cached and now - cached["created_at"] <= self.cluster_cache_ttl_sec:
                            payload = dict(cached["payload"])
                            payload["cache"] = {
                                "hit": True,
                                "ttl_sec": self.cluster_cache_ttl_sec,
                                "created_at": cached["created_at"],
                            }
                            return jsonify(payload), 200

                def doc_fetcher(uuids):
                    return hub.get_intelligence(uuids, light_weight=True)

                doc_cleaner = (
                    (lambda docs: [build_public_intelligence_summary(doc) for doc in docs])
                    if is_public else exclude_raw_data
                )

                result = agg.build_rich_cluster_members(
                    cluster_id=cluster_id,
                    doc_fetcher=doc_fetcher,
                    doc_cleaner=doc_cleaner,
                    offset=offset,
                    limit=limit,
                    sort_by=sort_by,
                    descending=desc,
                    source=source
                )
                result["source"] = source
                result["cache"] = {
                    "hit": False,
                    "ttl_sec": self.cluster_cache_ttl_sec,
                    "created_at": time.time(),
                }
                with self._cluster_response_cache_lock:
                    self._cluster_response_cache[cache_key] = {
                        "created_at": result["cache"]["created_at"],
                        "payload": result,
                    }

                return jsonify(result), 200

            except ValueError:
                # 簇不存在等参数类错误，不回显内部细节
                return jsonify({"error": "Cluster not found"}), 404
            except Exception as e:
                logger.exception("api_cluster_members error")
                # 与搜索接口一致的策略：不回显内部异常文本
                return jsonify({"error": "服务器内部错误，请稍后再试。"}), 500

        @app.route('/intelligence/<string:intelligence_uuid>', methods=['GET'])
        def intelligence_viewer_api(intelligence_uuid: str):
            return render_template('intelligence_detail.html', uuid=intelligence_uuid)

        # ---------------------------------------------- Management Pages ----------------------------------------------

        @app.route('/statistics/score_distribution.html', methods=['GET'])
        @WebServiceAccessManager.login_required
        def score_distribution_page():
            return get_statistics_page('/statistics/score_distribution')

        @app.route('/statistics/intelligence_statistics.html', methods=['GET'])
        @WebServiceAccessManager.login_required
        def intelligence_dashboard_page():
            return get_intelligence_statistics_page()

        @app.route('/maintenance/export_mongodb.html', methods=['GET'])
        @WebServiceAccessManager.login_required
        def export_mongodb_page():
            return render_template('export_mongodb.html')

        # 健康检查端点：供 Launcher ServerManager 与 Docker HEALTHCHECK 使用。
        # 不要求登录，避免健康检查进程无 Session 导致 401/重定向。
        # ?deep=1：深度健康检查（AI worker 线程存活 + 队列积压），异常返回 503 + JSON 细节。
        # 安全加固：deep 细节包含 AI 线程数、队列积压、Mongo 状态、AI 调用闸门原因等
        # 内部信息，仅放行两类来源——已登录 session；或请求来源 IP 为本机回环
        # （127.0.0.1/::1，Launcher 与同机运维探活即此场景）。其他来源静默降级为
        # 浅探活响应（不返回 403，避免向外部探测者泄露端点语义差异）。
        @app.route('/maintenance/ping', methods=['GET'])
        def maintenance_ping():
            if request.args.get('deep', '') in ('1', 'true', 'True'):
                client_ip = _get_client_ip()
                if session.get('logged_in', False) or client_ip in ('127.0.0.1', '::1'):
                    return self._deep_health_check()
                logger.warning(f'Deep health check denied for non-local client: {client_ip}')
            return jsonify({
                'status': 'ok',
                'time': datetime.datetime.now().isoformat(),
            })

    # ----------------------------------------------- Management Service -----------------------------------------------

        @app.route('/statistics/score_distribution', methods=['GET', 'POST'])
        @WebServiceAccessManager.login_required
        def get_score_distribution():
            try:
                # Get query parameters
                start_time_str = request.args.get('start_time')
                end_time_str = request.args.get('end_time')

                if not start_time_str or not end_time_str:
                    return jsonify({
                        "error": "Both start_time and end_time parameters are required"
                    }), 400

                # Convert to datetime objects
                # start_time = datetime.datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                # end_time = datetime.datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))

                start_time = ensure_timezone_aware(time_str_to_datetime(start_time_str))
                end_time = ensure_timezone_aware(time_str_to_datetime(end_time_str))

                stat_engine = self.intelligence_hub.get_statistics_engine()
                score_distribution = stat_engine.get_score_distribution(start_time, end_time)

                # Convert to array format for charting
                chart_data = [
                    {"score": score, "count": count}
                    for score, count in score_distribution.items()
                ]

                return jsonify({
                    "success": True,
                    "time_range": {
                        "start": start_time_str,
                        "end": end_time_str
                    },
                    "distribution": score_distribution,
                    "chart_data": chart_data,
                    "total_records": sum(score_distribution.values())
                })

            except ValueError:
                return jsonify({
                    "error": "Invalid time format. Please use ISO format (e.g., '2024-01-01T00:00:00Z')"
                }), 400
            except Exception as e:
                logger.error(f"Error processing request: {str(e)}")
                return jsonify({
                    "error": "Internal server error",
                    "message": str(e)
                }), 500

        @app.route('/statistics/intelligence_distribution/hourly', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_hourly_stats():
            """Get record counts grouped by hour for the specified time range"""
            start_time, end_time = self.get_time_range_params()

            stat_engine = self.intelligence_hub.get_statistics_engine()
            result = stat_engine.get_hourly_stats(start_time, end_time)

            return jsonify(result)

        @app.route('/statistics/intelligence_distribution/daily', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_daily_stats():
            """Get record counts grouped by day for the specified time range"""
            start_time, end_time = self.get_time_range_params()

            stat_engine = self.intelligence_hub.get_statistics_engine()
            result = stat_engine.get_daily_stats(start_time, end_time)

            return jsonify(result)

        @app.route('/statistics/intelligence_distribution/weekly', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_weekly_stats():
            """Get record counts grouped by week for the specified time range"""
            start_time, end_time = self.get_time_range_params()

            stat_engine = self.intelligence_hub.get_statistics_engine()
            result = stat_engine.get_weekly_stats(start_time, end_time)

            return jsonify(result)

        @app.route('/statistics/intelligence_distribution/monthly', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_monthly_stats():
            """Get record counts grouped by month for the specified time range"""
            start_time, end_time = self.get_time_range_params()

            stat_engine = self.intelligence_hub.get_statistics_engine()
            result = stat_engine.get_monthly_stats(start_time, end_time)

            return jsonify(result)

        @app.route('/statistics/intelligence_distribution/summary', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_stats_summary():
            """Get overall statistics for the specified time range"""
            start_time, end_time = self.get_time_range_params()

            stat_engine = self.intelligence_hub.get_statistics_engine()
            total_count, informant_stats = stat_engine.get_stats_summary(start_time, end_time)

            return jsonify({
                "total_count": total_count,
                "time_range": {
                    "start": start_time,
                    "end": end_time
                },
                "top_informants": informant_stats
            })

        @app.route('/statistics/intelligence_dashboard_data', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_intelligence_dashboard_data():
            """
            Return aggregated data for the IIS system dashboard.
            Includes hub state, AI client health, submission flow stats and system metrics.
            """
            try:
                now = get_aware_time()
                start_time = now - datetime.timedelta(hours=24)

                stat_engine = self.intelligence_hub.get_statistics_engine()
                hourly_distribution = stat_engine.get_hourly_stats(start_time, now)
                total_count, informant_stats = stat_engine.get_stats_summary(start_time, now)

                hub_stats = self.intelligence_hub.statistics
                ai_client_stats = {}
                if self.intelligence_hub.ai_client_manager:
                    ai_client_stats = self._serialize_for_json(
                        self.intelligence_hub.ai_client_manager.get_client_stats()
                    )
                    # get_client_stats() summary counts use Enum comparisons that may not match
                    # strings; recalculate from serialized client details for accurate dashboard.
                    clients = ai_client_stats.get('clients', [])
                    summary = ai_client_stats.setdefault('summary', {})
                    summary['available'] = sum(
                        1 for c in clients
                        if c.get('state', {}).get('status', '').lower() == 'available' and not c.get('state', {}).get('is_busy')
                    )
                    summary['busy'] = sum(1 for c in clients if c.get('state', {}).get('is_busy'))

                system_stats = {}
                process_stats = {}
                thread_stats = {}
                self_pid = os.getpid()
                if self.monitor_api and self.monitor_api.monitor:
                    try:
                        system_stats = self.monitor_api.monitor.get_system_stats()
                        process_stats = self.monitor_api.monitor.get_process_stats(self_pid) or {}
                        thread_stats = self.monitor_api.monitor.get_thread_stats(self_pid) or {}
                    except Exception as e:
                        logger.warning(f"Dashboard failed to read monitor stats: {e}")

                # Count active Python threads in this process for quick diagnostics
                active_python_threads = threading.active_count()

                return jsonify({
                    "timestamp": now.isoformat(),
                    "timestamp_formatted": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "hub": hub_stats,
                    "ai_clients": ai_client_stats,
                    "hourly_distribution": hourly_distribution,
                    "summary": {
                        "total_count": total_count,
                        "time_range": {"start": start_time.isoformat(), "end": now.isoformat()},
                        "top_informants": informant_stats
                    },
                    "system": system_stats,
                    "process": process_stats,
                    "threads": thread_stats,
                    "active_python_threads": active_python_threads,
                })
            except Exception as e:
                logger.error(f"Error generating dashboard data: {e}", exc_info=True)
                # 与搜索接口一致的策略：不回显内部异常文本
                return jsonify({"error": "服务器内部错误，请稍后再试。"}), 500

        # --------------------------- Entity Frequency Statistics ---------------------------

        @app.route('/api/sources', methods=['GET'])
        def api_sources():
            """获取所有数据源域名列表及文章数量。

            CONTRACT-4：响应 JSON 结构保持 {"domains": [...]} 不变，仅加 5 分钟内存缓存。
            """
            try:
                now = time.time()
                with self._sources_cache_lock:
                    cached = self._sources_cache
                    if (cached['payload'] is not None
                            and now - cached['created_at'] <= self._sources_cache_ttl_sec):
                        return jsonify(cached['payload'])

                engine = self.intelligence_hub.archive_db_query_engine
                domains = engine.get_source_domains(limit=500)
                payload = {"domains": domains}
                with self._sources_cache_lock:
                    self._sources_cache = {'created_at': now, 'payload': payload}
                return jsonify(payload)
            except Exception as e:
                logger.error(f"api_sources error: {e}", exc_info=True)
                # 与搜索接口一致的策略：不回显内部异常文本
                return jsonify({"error": "服务器内部错误，请稍后再试。"}), 500

        @app.route('/statistics/entity_frequency/page', methods=['GET'])
        def entity_frequency_page():
            """实体出现频率统计页面。未登录时进入 Public 模式，固定显示最近30天。"""
            is_public = not session.get('logged_in', False)
            return render_template('entity_frequency.html', public_mode=is_public)

        @app.route('/statistics/entity_frequency', methods=['GET'])
        def entity_frequency_api():
            """
            查询实体出现频率统计。
            未登录时固定返回最近30天数据，不可自定义时间范围。
            参数:
                entity_type: LOCATION | GEOGRAPHY | PEOPLE | ORGANIZATION
                start_time: ISO 格式开始时间（登录后可自定义）
                end_time: ISO 格式结束时间（登录后可自定义）
                granularity: day | week | month（默认 day）
                top_n: 默认 20，最大 50
                bottom_threshold: 默认 5
            """
            is_public = not session.get('logged_in', False)

            entity_type = request.args.get('entity_type', 'LOCATION')
            granularity = request.args.get('granularity', 'day')
            top_n = request.args.get('top_n', '20')
            bottom_threshold = request.args.get('bottom_threshold', '5')

            valid_types = {'LOCATION', 'GEOGRAPHY', 'PEOPLE', 'ORGANIZATION'}
            if entity_type not in valid_types:
                return jsonify({"error": f"Invalid entity_type. Must be one of {valid_types}"}), 400

            valid_granularities = {'day', 'week', 'month'}
            if granularity not in valid_granularities:
                return jsonify({"error": f"Invalid granularity. Must be one of {valid_granularities}"}), 400

            try:
                if is_public:
                    # Public 模式：固定近30天
                    end_time = get_aware_time()
                    start_time = end_time - datetime.timedelta(days=30)
                else:
                    start_str = request.args.get('start_time')
                    end_str = request.args.get('end_time')
                    if start_str:
                        start_time = dateutil.parser.parse(start_str)
                    else:
                        start_time = get_aware_time() - datetime.timedelta(days=30)

                    if end_str:
                        end_time = dateutil.parser.parse(end_str)
                    else:
                        end_time = get_aware_time()

                top_n = min(int(top_n), 50)
                bottom_threshold = int(bottom_threshold)
            except Exception as e:
                return jsonify({"error": f"Parameter parse error: {e}"}), 400

            # 时间范围最大 12 个月
            if (end_time - start_time).days > 366:
                return jsonify({"error": "Time range exceeds maximum limit of 12 months (366 days)."}), 400

            engine = self.intelligence_hub.entity_frequency_engine
            if not engine:
                return jsonify({"error": "EntityFrequencyEngine not initialized"}), 503

            try:
                result = engine.query_frequency(
                    entity_type=entity_type,
                    start_time=start_time,
                    end_time=end_time,
                    granularity=granularity,
                    top_n=top_n,
                    bottom_threshold=bottom_threshold,
                    allow_build=not is_public,
                )
                return jsonify(result)
            except Exception as e:
                logger.error(f"entity_frequency_api error: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        @app.route('/statistics/entity_frequency/trend', methods=['GET'])
        def entity_frequency_trend():
            """
            查询单个实体的趋势数据。
            未登录时固定返回最近30天数据，不可自定义时间范围。
            参数:
                entity_type, entity_name, start_time, end_time, granularity
            """
            is_public = not session.get('logged_in', False)

            entity_type = request.args.get('entity_type', '')
            entity_name = request.args.get('entity_name', '')
            granularity = request.args.get('granularity', 'day')

            if not entity_name:
                return jsonify({"error": "entity_name is required"}), 400

            valid_types = {'LOCATION', 'GEOGRAPHY', 'PEOPLE', 'ORGANIZATION'}
            if entity_type not in valid_types:
                return jsonify({"error": f"Invalid entity_type"}), 400

            try:
                if is_public:
                    end_time = get_aware_time()
                    start_time = end_time - datetime.timedelta(days=30)
                else:
                    start_str = request.args.get('start_time')
                    end_str = request.args.get('end_time')
                    start_time = dateutil.parser.parse(start_str) if start_str else get_aware_time() - datetime.timedelta(days=30)
                    end_time = dateutil.parser.parse(end_str) if end_str else get_aware_time()
            except Exception as e:
                return jsonify({"error": f"Parameter parse error: {e}"}), 400

            engine = self.intelligence_hub.entity_frequency_engine
            if not engine:
                return jsonify({"error": "EntityFrequencyEngine not initialized"}), 503

            try:
                result = engine.get_entity_trend(
                    entity_type=entity_type,
                    entity_name=entity_name,
                    start_time=start_time,
                    end_time=end_time,
                    granularity=granularity,
                    allow_build=not is_public,
                )
                return jsonify(result)
            except Exception as e:
                logger.error(f"entity_frequency_trend error: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        @app.route('/statistics/entity_frequency/build_cache/cancel', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def entity_frequency_build_cache_cancel():
            """取消正在进行的缓存构建任务。"""
            engine = self.intelligence_hub.entity_frequency_engine
            if not engine:
                return jsonify({"error": "EntityFrequencyEngine not initialized"}), 503
            engine.cancel_build()
            return jsonify({"status": "cancelled"})

        @app.route('/statistics/entity_frequency/build_cache', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def entity_frequency_build_cache():
            """
            手动触发实体频率缓存构建，返回 NDJSON 流式进度。
            """
            data = request.get_json() or {}
            start_str = data.get('start_time')
            end_str = data.get('end_time')

            engine = self.intelligence_hub.entity_frequency_engine
            if not engine:
                return jsonify({"error": "EntityFrequencyEngine not initialized"}), 503

            try:
                if start_str and end_str:
                    start_time = dateutil.parser.parse(start_str)
                    end_time = dateutil.parser.parse(end_str)
                else:
                    now = datetime.datetime.now(datetime.timezone.utc)
                    end_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    start_time = end_time - datetime.timedelta(days=1)

                if (end_time - start_time).days > 366:
                    return jsonify({"error": "Time range exceeds maximum limit of 12 months (366 days)."}), 400
            except Exception as e:
                return jsonify({"error": f"Parameter parse error: {e}"}), 400

            def generate():
                try:
                    for progress in engine.build_cache_with_progress(start_time, end_time):
                        yield json.dumps(progress) + "\n"
                        if progress.get("status") == "cancelled":
                            return
                except Exception as e:
                    logger.error(f"build_cache stream error: {e}", exc_info=True)
                    yield json.dumps({"status": "error", "message": str(e)}) + "\n"

            return Response(generate(), mimetype='application/x-ndjson')

        @app.route('/api/debug/trigger_aggregation', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def debug_trigger_aggregation():
            data = request.get_json() or {}
            start_str = data.get('start_time')
            end_str = data.get('end_time')

            time_range = None
            if start_str and end_str:
                start_ts = dateutil.parser.parse(start_str).timestamp()
                end_ts = dateutil.parser.parse(end_str).timestamp()
                time_range = (start_ts, end_ts)

            try:
                agg_engine = self.intelligence_hub.aggregation_engine_summary
                if not agg_engine:
                    return jsonify({"error": "Engine not ready"}), 503

                # 新增传入 fetcher 以便后台监控线程使用
                def doc_fetcher(uuids):
                    return self.intelligence_hub.get_intelligence(uuids, light_weight=True)

                job_id = agg_engine.trigger_offline(time_range=time_range, doc_fetcher=doc_fetcher)
                return jsonify({
                    "status": "success",
                    "job_id": job_id,
                    "mode": "explicit_time" if time_range else "auto_discovery"
                })
            except Exception:
                logger.exception("debug_trigger_aggregation error")
                # 与搜索接口一致的策略：不回显内部异常文本
                return jsonify({"error": "服务器内部错误，请稍后再试。"}), 500

        # --------------------------- Intelligence Graph ---------------------------

        @app.route('/intelligence/graph/build', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def trigger_graph_build():
            """
            触发一次异步的情报脉络图谱推演
            Body JSON: {"seed_uuid": "xxx", "max_depth": 3, "window_days": 7}
            """
            graph_engine = self.intelligence_hub.dynamic_graph_engine

            if graph_engine is None:
                return jsonify({
                    "error": "Service Unavailable",
                    "message": "Graph engine is not ready yet.",
                    "code": "ENGINE_INITIALIZING"
                }), 503

            data = request.get_json() or {}
            seed_uuid = data.get('seed_uuid')

            if not seed_uuid:
                return jsonify({"error": "Missing seed_uuid"}), 400

            try:
                max_depth = int(data.get('max_depth', 3))
                window_days = int(data.get('window_days', 7))
            except (TypeError, ValueError):
                return jsonify({
                    "error": "max_depth and window_days must be integers",
                    "code": "INVALID_GRAPH_OPTIONS"
                }), 400

            if not 1 <= max_depth <= 5 or not 1 <= window_days <= 30:
                return jsonify({
                    "error": "max_depth must be 1-5 and window_days must be 1-30",
                    "code": "GRAPH_OPTIONS_OUT_OF_RANGE"
                }), 400

            # 触发异步推演，毫秒级返回 thread_id
            try:
                thread_id = graph_engine.trigger_graph_build(
                    seed_uuid=seed_uuid,
                    max_depth=max_depth,
                    window_days=window_days
                )
                return jsonify({
                    "status": "success",
                    "thread_id": thread_id,
                    "message": "Graph building task submitted."
                }), 202
            except ValueError as e:
                # 同一 seed 正在推演（重复触发）属客户端冲突，返回 409 + 错误消息
                return jsonify({
                    "error": str(e),
                    "code": "GRAPH_BUILD_CONFLICT"
                }), 409
            except Exception:
                logger.exception("trigger_graph_build error")
                # 与搜索接口一致的策略：不回显内部异常文本
                return jsonify({"error": "服务器内部错误，请稍后再试。"}), 500

        @app.route('/intelligence/graph/status/<thread_id>', methods=['GET'])
        @WebServiceAccessManager.login_required
        def get_graph_status(thread_id):
            """
            前端轮询接口，获取推演状态与最终数据
            """
            graph_engine = self.intelligence_hub.dynamic_graph_engine

            if graph_engine is None:
                return jsonify({
                    "error": "Service Unavailable",
                    "message": "Graph engine is not ready yet.",
                    "code": "ENGINE_INITIALIZING"
                }), 503

            snapshot = graph_engine.get_snapshot(thread_id)

            if not snapshot:
                return jsonify({"error": "Thread ID not found"}), 404

            # 隐藏内部的 seed_uuid 等非必要字段，只吐出前端需要的核心状态和数据
            return jsonify({
                "thread_id": snapshot.get("thread_id"),
                "seed_uuid": snapshot.get("seed_uuid"),
                "status": snapshot.get("status"),  # PROCESSING, ANALYZING, COMPLETED, FAILED
                "created_at": snapshot.get("created_at"),
                "nodes": snapshot.get("nodes", []),
                "edges": snapshot.get("edges", []),
                "llm_summary": snapshot.get("llm_summary"),
                "llm_evaluation_score": snapshot.get("llm_evaluation_score"),
                # CONTRACT-5：前端已读取该字段，补进白名单
                "llm_critique": snapshot.get("llm_critique")
            })

        @app.route('/maintenance/export_mongodb', methods=['POST'])
        @WebServiceAccessManager.login_required
        @csrf_protected
        def export_mongodb():
            """
            Handle export request.
            Supports:
            1. Mode: 'range' (specific start/end) or 'all' (entire db)
            2. Split: 'month', 'week', 'year', or None
            3. Target: 'archive', 'cache', or 'all'
            """
            try:
                # 1. Get parameters
                data = request.get_json()
                if not data:
                    return jsonify({'status': 'error', 'message': 'No JSON data received'}), 400

                mode = data.get('mode', 'range')  # 'range' or 'all'
                target = data.get('target', 'archive')
                split_by = data.get('splitBy') # 'month', 'week', 'year' or None (empty string)

                # Normalize split_by to None if it's an empty string or 'none'
                if not split_by or split_by == 'none':
                    split_by = None

                # 2. Prepare Date Range (Only if mode is 'range')
                start_dt = None
                end_dt = None

                if mode == 'range':
                    start_date_str = data.get('startDate')
                    end_date_str = data.get('endDate')
                    if not start_date_str or not end_date_str:
                        return jsonify({'status': 'error', 'message': 'startDate and endDate are required for range mode'}), 400
                    try:
                        start_dt = date_parser.parse(start_date_str)
                        end_dt = date_parser.parse(end_date_str)
                    except ValueError as e:
                        return jsonify({'status': 'error', 'message': f'Invalid date format: {str(e)}'}), 400

                generated_files = []
                messages = []
                export_root = os.path.abspath(EXPORT_PATH)

                def add_generated_file(file_path):
                    """仅返回 EXPORT_PATH 下的相对路径，避免泄露服务器绝对路径。"""
                    absolute_path = os.path.abspath(str(file_path))
                    if os.path.commonpath([export_root, absolute_path]) != export_root:
                        raise ValueError('Generated file is outside export directory')
                    relative_path = os.path.relpath(absolute_path, export_root)
                    generated_files.append(relative_path.replace(os.sep, '/'))

                # 3. Define Export Logic Helper
                def run_export(db_instance, sub_dir, time_field, prefix):
                    """Helper to execute export on a specific DB instance"""
                    if not db_instance:
                        messages.append(f"Skipped {prefix}: Database instance not initialized.")
                        return

                    full_dir = os.path.join(EXPORT_PATH, sub_dir)

                    # Dispatch based on mode
                    if mode == 'all':
                        # Call export_all
                        files = db_instance.export_all(
                            directory=full_dir,
                            split_by=split_by,
                            time_field=time_field,
                            add_timestamp=True
                        )
                        if files:
                            for file_path in files:
                                add_generated_file(file_path)
                            messages.append(f"Exported all {prefix} data ({len(files)} files)")
                        else:
                            messages.append(f"No data found in {prefix}")
                    else:
                        # Call export_by_time_range
                        output_file = db_instance.export_by_time_range(
                            start_dt=start_dt,
                            end_dt=end_dt,
                            directory=full_dir,
                            time_field=time_field,
                            file_prefix=prefix,
                            add_timestamp=True
                        )
                        if output_file:
                            add_generated_file(output_file)
                            messages.append(f"Exported {prefix} range to {os.path.basename(output_file)}")
                        else:
                            messages.append(f"No data found for {prefix} in range")

                # 4. Execute Exports based on Target

                # --- Export Archive DB ---
                if target in ['archive', 'all']:
                    time_field = f"APPENDIX.{APPENDIX_TIME_ARCHIVED}"
                    run_export(
                        db_instance=self.intelligence_hub.mongo_db_archive,
                        sub_dir='mongo_db_archive',
                        time_field=time_field,
                        prefix='intelligence_archived'
                    )

                # --- Export Cache DB ---
                if target in ['cache', 'all']:
                    cache_time_field = '__TIME_GOT__'
                    run_export(
                        db_instance=self.intelligence_hub.mongo_db_cache,
                        sub_dir='mongo_db_cache',
                        time_field=cache_time_field,
                        prefix='intelligence_cache'
                    )

                # 5. Return Result
                if generated_files:
                    return jsonify({
                        'status': 'success',
                        'message': '; '.join(messages),
                        'files': generated_files,
                        'count': len(generated_files)
                    })
                else:
                    return jsonify({
                        'status': 'warning',
                        'message': '; '.join(messages)
                    }), 200

            except Exception as e:
                logging.error(f"Export failed: {e}", exc_info=True)
                return jsonify({
                    'status': 'error',
                    'message': '导出失败，请稍后重试',
                    'code': 'EXPORT_FAILED'
                }), 500

        @app.route('/download/<path:filename>')
        @WebServiceAccessManager.login_required
        def download_file(filename):
            """Download exported file"""
            try:
                from werkzeug.utils import secure_filename

                # 逐段清洗相对路径，允许导出子目录，同时拒绝路径穿越。
                raw_path = str(filename).replace('/', os.sep).replace('\\', os.sep)
                safe_parts = []
                for part in raw_path.split(os.sep):
                    if not part or part == '.':
                        continue
                    if part == '..':
                        return jsonify({'status': 'error', 'message': 'Invalid file path'}), 400
                    safe_part = secure_filename(part)
                    if not safe_part:
                        return jsonify({'status': 'error', 'message': 'Invalid file path'}), 400
                    safe_parts.append(safe_part)

                if not safe_parts:
                    return jsonify({'status': 'error', 'message': 'Invalid file path'}), 400

                export_root = os.path.abspath(EXPORT_PATH)
                full_path = os.path.abspath(os.path.join(export_root, *safe_parts))
                if os.path.commonpath([export_root, full_path]) != export_root:
                    return jsonify({'status': 'error', 'message': 'Invalid file path'}), 400

                if not os.path.isfile(full_path):
                    return jsonify({
                        'status': 'error',
                        'message': 'File not found'
                    }), 404
                return send_file(
                    full_path,
                    as_attachment=True,
                    download_name=os.path.basename(full_path)
                )
            except FileNotFoundError:
                return jsonify({
                    'status': 'error',
                    'message': 'File not found'
                }), 404

    # ------------------------------------------------------------------------------------------------------------------

    def handle_error(self, error: str):
        # RPC 派发异常经 error_handler 回调进入此处：
        # 写日志而非 print，确保异常进入日志文件而非仅输出到 stdout。
        logger.error(f'Handle error in IntelligenceHubWebService: {error}')

    def _articles_to_rss_items(self, articles: dict | List[dict]) -> List[FeedItem]:
        # Using a fixed default time, combined with a unique UUID,
        # prevents RSS readers from mistakenly identifying article duplicates.
        default_date = datetime.datetime(1970, 1, 1)

        if not isinstance(articles, list):
            articles = [articles]
        try:
            rss_items = []
            for doc in articles:
                if 'UUID' not in doc:
                    logger.warning(f'Warning: archived data field missing.')
                    continue
                appendix = doc.get('APPENDIX') or {}
                # title：EVENT_TITLE 可能为 None（get 带默认值只在缺键时生效），
                # 依次回退 EVENT_BRIEF、占位标题。
                title = doc.get('EVENT_TITLE') or doc.get('EVENT_BRIEF') or '(无标题)'
                # pubDate 语义取舍：优先取原文发布时间(__TIME_PUB__)；
                # 缺失时回退归档时间(__TIME_ARCHIVED__)，保证 RSS 阅读器有新到排序依据。
                pub_date = (appendix.get('__TIME_PUB__')
                            or appendix.get(APPENDIX_TIME_ARCHIVED)
                            or default_date)
                rss_item = FeedItem(
                    guid=doc['UUID'],
                    title=title,
                    link=f"/intelligence/{doc['UUID']}",
                    # description 仅用公开摘要字段，不含 EVENT_TEXT 全文
                    description=doc.get('EVENT_BRIEF') or '',
                    pub_date=pub_date)
                rss_items.append(rss_item)
            return rss_items
        except Exception as e:
            logger.error(f"Article to rss items failed: {str(e)}")
            return []

    def get_rendered_md_post(self, post_name: str) -> str:
        try:
            # Sanitize input and construct safe file path
            safe_article = post_name.replace('..', '').strip('/')  # Prevent directory traversal
            md_file_path = os.path.join('posts', f"{safe_article}.md")

            # Generate HTML from Markdown（内部已做 realpath 前缀检查 + 内容消毒）
            rendered_html_path = generate_html_from_markdown(md_file_path)

            if rendered_html_path:
                # 安全加固：直出文件内容，避免 Jinja2 对 md 内容二次求值（SSTI）。
                # 生成的 HTML 已由 PostManager 消毒，不再经过 render_template。
                with open(rendered_html_path, "r", encoding="utf-8") as f:
                    return f.read()
            else:
                return ''
        except Exception as e:
            logger.error(f'Invalid post: {post_name}')
            return ''

    def get_time_range_params(self):
        """
        Extract and validate time range parameters from request
        Returns start_time and end_time as datetime objects
        """
        start_str = request.args.get('start')
        end_str = request.args.get('end')

        if start_str:
            start_time = dateutil.parser.parse(start_str)
        else:
            # Default to 24 hours ago if no start time provided
            start_time = get_aware_time() - datetime.timedelta(hours=24)

        if end_str:
            end_time = dateutil.parser.parse(end_str)
        else:
            # Default to current time if no end time provided
            end_time = get_aware_time()

        return start_time, end_time

    def dump_request_connection_periodically(self):
        if self.request_tracer:
            self.request_tracer.dump_long_running_requests()
        # daemon=True：自我续约的 Timer 线程不阻碍主进程退出
        timer = threading.Timer(30.0, self.dump_request_connection_periodically)
        timer.daemon = True
        timer.start()

