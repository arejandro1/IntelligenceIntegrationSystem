import os.path
import sys
import time
import uuid
import atexit
import signal
import socket
import logging
import datetime
import threading
import traceback
from flask import Flask
from typing import Optional, Tuple
from pathlib import Path
from functools import partial

from AIClientCenter.AIClientManagerBackend import AIDashboardService
from GlobalConfig import *
from IntelligenceHub import IntelligenceHub
from Tools.MongoDBAccess import MongoDBStorage
from Tools.SystemMonitorService import MonitorAPI
from Tools.SystemdWatchdog import is_watchdog_enabled, notify_ready, notify_alive, notify_stopping
from VectorDB.VectorDBClient import VectorDBClient
from MyPythonUtility.easy_config import EasyConfig
from ServiceComponent.UserManager import UserManager
from ServiceComponent.RSSPublisher import RSSPublisher
from AIClientCenter.AIClientManager import AIClientManager
from AIClientCenter.ClientStateSQLiteLogger import ClientStateSQLiteLogger
from MyPythonUtility.proc_utils import find_processes, kill_processes
from IntelligenceHubWebService import IntelligenceHubWebService, WebServiceAccessManager
from PyLoggingBackend import setup_logging, backup_and_clean_previous_log_file, limit_logger_level, LoggerBackend
from Tools.PerformanceLogger import setup_performance_logging, PERF_LOGGER_NAME

# SECRET_KEY 配置化：优先从配置文件读取，否则使用随机值并警告
# 生产环境建议在 config.json 中设置固定的 secret_key（至少32字符）
_configured_secret_key = None  # 将在 run() 函数中从 config 读取

def get_flask_app_with_config():
    """创建并配置 Flask 应用，支持动态设置 SECRET_KEY。"""
    app = Flask(__name__)

    # 如果配置了 secret_key 则使用，否则使用随机值（每次重启会话失效）
    app.secret_key = _configured_secret_key if _configured_secret_key else str(uuid.uuid4())

    app.permanent_session_lifetime = datetime.timedelta(days=7)
    app.config.update(
        # SESSION_COOKIE_SECURE=True,  # 仅通过HTTPS发送（生产环境必须）
        SESSION_COOKIE_HTTPONLY=True,  # 防止JavaScript访问（安全）
        SESSION_COOKIE_SAMESITE='Lax'  # 防止CSRF攻击
    )
    return app

wsgi_app = get_flask_app_with_config()

# create_wsgi_app() 引导标志：防止多 worker/多分支重复初始化 Hub（进程内单次引导）
_wsgi_app_bootstrapped = False


logger = logging.getLogger(__name__)

self_path = os.path.dirname(os.path.abspath(__file__))


def show_intelligence_hub_statistics_forever(hub: IntelligenceHub):
    prev_statistics = {}
    # 随 hub.shutdown_flag 退出，配合优雅关闭（线程本身也设为 daemon 兜底）
    while not hub.shutdown_flag.is_set():
        if hub.statistics != prev_statistics:
            logger.info(f'Hub queue size: {hub.statistics}')
            prev_statistics = hub.statistics
        time.sleep(2)


def build_ai_client_manager(config: EasyConfig, legacy_ai_active: Optional[bool] = None):
    try:
        from _config.ai_client_config import AI_CLIENTS, AI_CLIENT_LIMIT
    except Exception:
        # 回退陷阱防护：配置缺失时绝不能静默使用 example 占位配置跑真实 AI 分析。
        # P0-3 修复：只有"旧 AI Worker 实际运行"才要求 ai_client_config。
        # - 旧 Worker 运行（api_enabled 且非 dry_run 且非 drainer 模式）且缺配置
        #   → fail-fast 拒绝启动；
        # - OffPeakDrainer 模式使用 DeepSeekClient（密钥来自 DEEPSEEK_API_KEY），
        #   不依赖旧多厂商 ai_client_config → 允许回退 example 并告警；
        # - 只采集/DRY_RUN 模式 → 允许回退 example 并告警。
        # 批次一修复：布尔开关改用 parse_bool_or 严格解析（杜绝 bool("false") 为真），
        # 非法值（如 "flase"、2）fail-fast，不静默反向。
        from ServiceComponent.ConfigParsing import InvalidConfigValueError, parse_bool_or
        cw_cfg = config.get('intelligence_hub.china_watch', {}) or {}
        try:
            api_enabled = parse_bool_or(cw_cfg.get('api_enabled'), False,
                                        'china_watch.api_enabled')
            api_dry_run = parse_bool_or(cw_cfg.get('api_dry_run'), True,
                                        'china_watch.api_dry_run')
            use_drainer = parse_bool_or(cw_cfg.get('use_off_peak_drainer'), False,
                                        'china_watch.use_off_peak_drainer')
        except InvalidConfigValueError as e:
            raise RuntimeError(f"china_watch 布尔开关解析失败，拒绝启动（fail-fast）：{e}") from e
        if legacy_ai_active is None:
            legacy_ai_active = api_enabled and not api_dry_run and not use_drainer

        if legacy_ai_active:
            logger.critical(
                "\n"
                "==================== AI Client 配置缺失，拒绝启动 ====================\n"
                "无法导入 _config.ai_client_config，且旧 AI Worker 处于运行模式。\n"
                "继续使用 example 占位配置会产生失败的 AI 调用并污染数据，因此拒绝启动。\n"
                "修复方法：\n"
                "  - 复制 _config/ai_client_config_example.py 为 _config/ai_client_config.py\n"
                "  - 填入真实 API Key / 模型地址后重启；\n"
                "  - 或切换到 OffPeakQueueDrainer 模式（仅需 DEEPSEEK_API_KEY）/\n"
                "    将 api_enabled 设为 false（只采集模式）。\n"
                "=====================================================================\n"
            )
            raise RuntimeError(
                "AI client config missing while legacy AI worker active; refusing to start.")

        logger.warning(
            "无法导入 _config.ai_client_config，回退到 example 占位配置。"
            "当前旧 AI Worker 未运行（drainer/DRY_RUN/只采集模式），"
            "占位配置不会产生真实 AI 调用；如需旧 Worker 请创建 _config/ai_client_config.py。"
        )
        from _config.ai_client_config_example import AI_CLIENTS, AI_CLIENT_LIMIT

    sqlite_db_file = config.get('ai_client_center.sqlite_db_file', 'ai_client_state_log.db')
    heartbeat_interval_sec = config.get('ai_client_center.heartbeat_interval_sec', 30)
    heartbeat_grace_sec = config.get('ai_client_center.heartbeat_grace_sec', 120)

    state_logger = ClientStateSQLiteLogger(
        sqlite_db_path=os.path.join(DATA_PATH, sqlite_db_file),
        run_id=str(time.time()),
        heartbeat_interval_sec=heartbeat_interval_sec,
        heartbeat_grace_sec=heartbeat_grace_sec
    )
    state_logger.start()

    client_manager = AIClientManager(state_logger=state_logger)

    logger.info(f"AI clients count: {len(AI_CLIENTS)}).")
    logger.info(f"AI group limit count: {len(AI_CLIENT_LIMIT)}).")

    for client in AI_CLIENTS.values():
        client_manager.register_client(client)

    for group, limit in AI_CLIENT_LIMIT.items():
        client_manager.set_group_limit(group, limit)

    return client_manager


def build_china_watch_guardrails(config: EasyConfig,
                                 mongodb_host: str, mongodb_port: int,
                                 mongodb_user: str, mongodb_pass: str) -> dict:
    """组装 china_watch M3 护栏组件（M3 接线核心）。

    组件链：互斥校验 → ApiTimePolicy（峰谷）→ BudgetGuard（预算）→
    PricingPolicyRegistry（价格）→ ApiUsageLedger（台账）→ DeepSeekModelRouter（分级路由）→
    RulePreFilter（预筛）→ PersistentQueue（AI 延期队列）→ DeepSeekClient（真实客户端）→
    OffPeakQueueDrainer（闲时消费者）。

    fail-fast（任一命中即抛错拒绝启动）：
    1. use_legacy_ai_worker 与 use_off_peak_drainer 同时为 true；
    2. api_enabled=true 且 api_dry_run=false 且 api_policy.effective_from 为空
       （规则：政策生效日期为空时不得进行正式调用）；
    3. 真实模式但 DEEPSEEK_API_KEY 环境变量缺失。

    off_peak_only=false 视为"禁止调用"而非"全天候放行"（第一阶段约束：
    DeepSeek 仅非高峰时段调用），构造 enabled=False 的时间策略并告警。

    返回组件字典；Drainer 的业务回调（invoke_handler/on_complete）由调用方
    在 Hub 构造后绑定（回调是 Hub 实例方法，避免循环依赖）。
    """
    from ServiceComponent.ConsumerMutex import ConsumerMutexError, validate_startup_config
    from ServiceComponent.ApiTimePolicy import ApiTimePolicy
    from ServiceComponent.BudgetGuard import BudgetGuard
    from ServiceComponent.PricingPolicyRegistry import ModelPricing, PricingPolicyRegistry
    from ServiceComponent.ApiUsageLedger import ApiUsageLedger, MongoLedgerStore
    from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
    from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
    from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer
    from ServiceComponent.PersistentQueue import MemoryQueueBackend, MongoQueueBackend, PersistentQueue
    from ServiceComponent.RulePreFilter import RulePreFilter
    from ServiceComponent.SourceRegistry import SourceRegistry
    from ServiceComponent.DeepSeekClient import DeepSeekClient
    from ServiceComponent.ConfigParsing import InvalidConfigValueError, parse_float_or

    cw_cfg = config.get('intelligence_hub.china_watch', {}) or {}
    # P0-3：布尔开关严格解析（validate_startup_config 内部使用 parse_bool_or，
    # 非法值抛 InvalidConfigValueError → ConsumerMutexError fail-fast），
    # 返回解析后的四个开关供下方复用，杜绝 bool("false") 为真。
    try:
        switches = validate_startup_config(cw_cfg)
    except ConsumerMutexError as e:
        logger.critical(f"china_watch 消费者互斥校验失败，拒绝启动：{e}")
        raise
    api_enabled = switches['api_enabled']
    api_dry_run = switches['api_dry_run']
    use_legacy = switches['use_legacy_ai_worker']
    use_drainer = switches['use_off_peak_drainer']

    # ---------- 2) 时间策略（峰谷 + Guard） ----------
    ap_cfg = config.get('intelligence_hub.api_policy', {}) or {}
    peak_windows_raw = ap_cfg.get('peak_windows', ['09:00-12:00', '14:00-18:00'])
    peak_windows = []
    for w in peak_windows_raw:
        parts = str(w).split('-')
        if len(parts) == 2 and all(':' in p for p in parts):
            peak_windows.append((parts[0], parts[1]))

    effective_from_raw = str(ap_cfg.get('effective_from', '') or '').strip()
    effective_from = None
    if effective_from_raw:
        try:
            effective_from = datetime.datetime.fromisoformat(effective_from_raw)
        except ValueError as e:
            raise RuntimeError(f"api_policy.effective_from 解析失败: {effective_from_raw!r}: {e}")

    off_peak_only = bool(ap_cfg.get('off_peak_only', True))
    if not off_peak_only:
        # 第一阶段约束：DeepSeek 仅非高峰调用。off_peak_only=false 不允许"全天候放行"，
        # fail-closed 处理为禁止调用并告警（采集/预筛/入队不受影响）。
        logger.critical(
            "api_policy.off_peak_only=false 与第一阶段约束（DeepSeek 仅非高峰时段调用）冲突，"
            "已按 fail-closed 处理：时间策略禁用，所有 AI 调用被拒绝。"
            "如需恢复请在配置中设为 true。")

    time_policy = ApiTimePolicy(
        timezone=str(ap_cfg.get('billing_timezone', 'Asia/Shanghai')),
        peak_windows=peak_windows,
        off_peak_start_guard_minutes=int(ap_cfg.get('off_peak_start_guard_minutes', 1)),
        off_peak_end_guard_minutes=int(ap_cfg.get('off_peak_end_guard_minutes', 10)),
        policy_version=str(ap_cfg.get('policy_version', '')),
        effective_from=effective_from,
        enabled=off_peak_only,
    )

    # 规则红线：政策生效日期为空时不得进行正式调用
    if api_enabled and not api_dry_run and effective_from is None:
        raise RuntimeError(
            "api_policy.effective_from 为空：按规则'政策生效日期为空时不得进行正式调用'，"
            "api_enabled=true 且 api_dry_run=false 的组合被拒绝。"
            "请在 intelligence_hub.api_policy.effective_from 填写 ISO 8601 生效日期。")

    # ---------- 3) 预算守卫（P0-3：数值严格解析，非法 fail-fast） ----------
    b_cfg = config.get('intelligence_hub.budget', {}) or {}

    def _budget_warn(kind: str, cost: float) -> None:
        logger.warning(f"[BudgetGuard] {kind} 阈值已触发，当前累计费用 {cost} 元。")

    try:
        budget_guard = BudgetGuard(
            daily_budget_cny=parse_float_or(b_cfg.get('daily_api_budget_cny'), 0.0,
                                            'budget.daily_api_budget_cny', minimum=0.0),
            monthly_budget_cny=parse_float_or(b_cfg.get('monthly_api_budget_cny'), 0.0,
                                              'budget.monthly_api_budget_cny', minimum=0.0),
            daily_call_limit=int(parse_float_or(b_cfg.get('daily_api_call_limit'), 0.0,
                                                'budget.daily_api_call_limit', minimum=0.0)),
            monthly_call_limit=int(parse_float_or(b_cfg.get('monthly_api_call_limit'), 0.0,
                                                  'budget.monthly_api_call_limit', minimum=0.0)),
            daily_warn_cny=parse_float_or(b_cfg.get('daily_api_warn_cny'), 0.0,
                                          'budget.daily_api_warn_cny', minimum=0.0),
            monthly_warn_cny=parse_float_or(b_cfg.get('monthly_api_warn_cny'), 0.0,
                                            'budget.monthly_api_warn_cny', minimum=0.0),
            warn_callback=_budget_warn,
            timezone=str(ap_cfg.get('billing_timezone', 'Asia/Shanghai')),
        )
    except InvalidConfigValueError as e:
        raise RuntimeError(f"budget 配置非法，拒绝启动（fail-closed）：{e}") from e

    # ---------- 4) 价格政策与费用台账 ----------
    pr_cfg = config.get('intelligence_hub.pricing', {}) or {}
    flash_model = str(pr_cfg.get('flash_model', 'deepseek-v4-flash'))
    pro_model = str(pr_cfg.get('pro_model', 'deepseek-v4-pro'))
    flash_pricing = ModelPricing(
        model_name=flash_model, is_flash=True,
        input_price_cny_per_1k=float(pr_cfg.get('flash_input_cny_per_1k', 0.001)),
        output_price_cny_per_1k=float(pr_cfg.get('flash_output_cny_per_1k', 0.002)))
    pro_pricing = ModelPricing(
        model_name=pro_model, is_flash=False,
        input_price_cny_per_1k=float(pr_cfg.get('pro_input_cny_per_1k', 0.003)),
        output_price_cny_per_1k=float(pr_cfg.get('pro_output_cny_per_1k', 0.006)))
    pricing = PricingPolicyRegistry(
        flash_model=flash_model, pro_model=pro_model,
        flash_pricing=flash_pricing, pro_pricing=pro_pricing,
        peak_multiplier=float(ap_cfg.get('peak_price_multiplier', 2.0)),
        off_peak_multiplier=float(ap_cfg.get('off_peak_price_multiplier', 1.0)),
        policy_version=str(ap_cfg.get('policy_version', '')),
        effective_from=effective_from)

    # ---------- 4b) 费用台账（P1-6：持久化 + 重启恢复） ----------
    # 真实模式必须持久化（否则重启后预算累计归零，存在超支风险）；
    # DRY_RUN/只采集允许内存台账（不产生真实费用）。
    ledger_store = None
    if api_enabled and not api_dry_run:
        try:
            ledger_db = MongoDBStorage(
                host=mongodb_host, port=mongodb_port,
                db_name='IntelligenceIntegrationSystem',
                username=mongodb_user, password=mongodb_pass,
                collection_name='api_usage_ledger')
            ledger_store = MongoLedgerStore(ledger_db.collection)
            logger.info("API usage ledger: MongoDB persistent backend ready (api_usage_ledger).")
        except Exception as e:
            raise RuntimeError(
                f"真实 API 模式要求费用台账持久化，MongoDB 后端初始化失败，"
                f"拒绝启动（禁止静默使用易失的内存台账）：{e}") from e
    ledger = ApiUsageLedger(store=ledger_store,
                            timezone=str(ap_cfg.get('billing_timezone', 'Asia/Shanghai')))
    # P1-6：从台账恢复当日/当月累计（重启后预算不归零）
    if ledger_store is not None:
        budget_guard.restore_from_ledger(ledger)

    # ---------- 5) 分级路由 ----------
    model_router = DeepSeekModelRouter(
        time_policy, pricing, budget_guard, ledger,
        api_enabled=api_enabled, api_dry_run=api_dry_run)

    # ---------- 6) 规则预筛（Hub 侧唯一权威） ----------
    prefilter = None
    pf_cfg = config.get('intelligence_hub.prefilter', {}) or {}
    if pf_cfg.get('enabled', True):
        sources_yaml = os.path.join(self_path, 'sources', 'china_watch_sources.yaml')
        try:
            registry = SourceRegistry(sources_yaml)
            prefilter = RulePreFilter(registry, pf_cfg)
            logger.info(f"RulePreFilter enabled: {registry.count()} sources registered "
                        f"({len(registry.get_enabled())} enabled).")
        except Exception as e:
            raise RuntimeError(f"预筛初始化失败（fail-closed，拒绝以无预筛状态启动）: {e}")
    else:
        logger.warning("prefilter.enabled=false：规则预筛已关闭，数据未经预筛直接入队（仅建议调试使用）。")

    # ---------- 7) AI 延期队列（MongoDB 持久化；P1-5：不可用即拒绝启动） ----------
    persistent_queue = None
    if use_drainer:
        try:
            db_queue = MongoDBStorage(
                host=mongodb_host, port=mongodb_port,
                db_name='IntelligenceIntegrationSystem',
                username=mongodb_user, password=mongodb_pass,
                collection_name='ai_delay_queue')
            persistent_queue = PersistentQueue(
                MongoQueueBackend(db_queue.collection), lease_seconds=600)
            logger.info("AI delay queue: MongoDB persistent backend ready (ai_delay_queue).")
        except Exception as e:
            # P1-5 修复：生产 Drainer 模式下持久化队列不可用时不得静默降级为
            # 易丢数据的内存队列——重启即丢全部待分析任务。改为 fail-fast。
            raise RuntimeError(
                f"AI 延期队列 MongoDB 后端初始化失败，use_off_peak_drainer=true "
                f"要求持久化队列，拒绝启动（禁止降级内存队列）：{e}") from e

    # ---------- 8) 真实 DeepSeek 客户端（密钥仅从环境变量注入，不写日志） ----------
    real_client = None
    if api_enabled and not api_dry_run:
        api_key = os.getenv('DEEPSEEK_API_KEY', '').strip()
        if not api_key:
            raise RuntimeError(
                "真实 API 模式（api_enabled=true 且 api_dry_run=false）需要环境变量 "
                "DEEPSEEK_API_KEY（.env / systemd Environment / compose env 注入），当前缺失，拒绝启动。")
        real_client = DeepSeekClient(api_key=api_key)
        logger.info("DeepSeekClient ready for real API calls (key from env, not logged).")

    # ---------- 9) 闲时 Drainer（回调由调用方在 Hub 构造后绑定） ----------
    drainer = None
    if use_drainer:
        drainer = OffPeakQueueDrainer(
            router=model_router,
            mock_client=MockDeepSeekClient(),
            drain_interval_seconds=int(ap_cfg.get('queue_drain_interval_seconds', 30)),
            max_per_minute=int(ap_cfg.get('max_starts_per_minute', 2)),
            persistent_queue=persistent_queue,
            real_client=real_client)
        # P1-6 / 批次二：逐尝试护栏与记账回调由 Drainer 统一注入
        # （含每分钟限流——每次真实 HTTP 尝试含内部重试都独立限流计数；
        # 预算检查尊重已取得的调用名额，不会在发送前误拒）。
        # 注意：不要在此处用 model_router.make_attempt_guard 覆盖，
        # 否则会丢失限流与逐尝试记账。
        drainer.attach_real_client_guards()

    # ---------- 10) 推送服务（P1-7：持久化 push_tasks 队列，仅生产端入队） ----------
    push_notifier = None
    push_cfg = config.get('intelligence_hub.push_notifier', {}) or {}
    try:
        if bool(push_cfg.get('enabled', False)) and str(push_cfg.get('webhook_url', '')).strip():
            push_db = MongoDBStorage(
                host=mongodb_host, port=mongodb_port,
                db_name='IntelligenceIntegrationSystem',
                username=mongodb_user, password=mongodb_pass,
                collection_name='push_tasks')
            from ServiceComponent.PushNotifier import PushNotifier
            push_notifier = PushNotifier(
                queue=PersistentQueue(MongoQueueBackend(push_db.collection), lease_seconds=120),
                config=push_cfg)
            logger.info("PushNotifier ready (producer side; consumer runs in notifier container).")
        else:
            logger.info("PushNotifier 未启用（intelligence_hub.push_notifier.enabled=false 或 webhook_url 为空）。")
    except Exception as e:
        # 推送是低优先级附加功能：初始化失败降级为禁用并告警，不阻断主链路
        logger.warning(f"PushNotifier 初始化失败，推送功能禁用（不影响采集/分析/归档）：{e}")
        push_notifier = None

    # ---------- 11) 磁盘守卫（P1-8：70/85/95 三级降级） ----------
    disk_guard = None
    disk_cfg = config.get('intelligence_hub.disk_guard', {}) or {}
    if bool(disk_cfg.get('enabled', True)):
        try:
            from ServiceComponent.DiskSpaceGuard import DiskSpaceGuard
            watch_path = str(disk_cfg.get('watch_path', '')).strip() or DATA_PATH
            if not os.path.isabs(watch_path):
                # 批次四修复：相对路径以项目根为基准解析。
                # 原实现以 DATA_PATH 为基准，把示例默认值 "_data" 拼成
                # "_data/_data"（不存在），检测必然失败且显示为正常。
                watch_path = os.path.normpath(os.path.join(self_path, watch_path))
            disk_guard = DiskSpaceGuard(
                watch_path=watch_path,
                warn_percent=float(disk_cfg.get('warn_percent', 70.0)),
                stop_push_percent=float(disk_cfg.get('stop_push_percent', 85.0)),
                stop_collect_percent=float(disk_cfg.get('stop_collect_percent', 95.0)))
            logger.info("DiskSpaceGuard ready (watch=%s, 70/85/95 thresholds).", watch_path)
        except Exception as e:
            logger.warning(f"DiskSpaceGuard 初始化失败（降级策略不可用，不影响主链路）：{e}")
            disk_guard = None

    return {
        'api_enabled': api_enabled,
        'api_dry_run': api_dry_run,
        'use_legacy_ai_worker': use_legacy,
        'use_off_peak_drainer': use_drainer,
        'time_policy': time_policy,
        'budget_guard': budget_guard,
        'pricing': pricing,
        'ledger': ledger,
        'model_router': model_router,
        'prefilter': prefilter,
        'persistent_queue': persistent_queue,
        'real_client': real_client,
        'drainer': drainer,
        'push_notifier': push_notifier,
        'disk_guard': disk_guard,
    }


def _probe_tcp_port(host: str, port: int, timeout: float = 2.0) -> bool:
    """短超时 TCP 连接探测端口是否就绪（用于 VectorDB 可用性检查）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_start_vector_db_service(config: EasyConfig, force_restart: bool = False):
    vector_enabled = config.get('intelligence_hub.vectordb.enabled', False)
    vector_db_port = config.get('intelligence_hub.vectordb.vector_db_port', 8001)
    vector_db_path = config.get('intelligence_hub.vectordb.vector_db_path', '')
    embedding_model_name = config.get('intelligence_hub.vectordb.embedding_model_name', '')
    vector_stores = config.get('intelligence_hub.vectordb.stores', [])

    vector_db_client = None
    if vector_enabled and vector_db_path and embedding_model_name:
        vector_db_path_abs = vector_db_path \
            if os.path.isabs(vector_db_path) \
            else os.path.join(DATA_PATH, vector_db_path)

        need_launch = False
        pids = find_processes('VectorDBBService.py')
        if pids:
            if force_restart:
                need_launch = True
                killed_count = kill_processes(pids)
                logger.info(f"Found running vector db service {', '.join(str(pids))}, killed {killed_count}.")
            else:
                logger.info(f"Found running vector db service {', '.join(str(pids))}, ignore.")
        else:
            need_launch = True

        if need_launch:
            # 不自动 spawn VectorDB 子进程：
            # 推荐启动方式是 `python -m VectorDB.VectorDBBService`（直接跑脚本路径会因
            # 顶层包导入失败），进程管理交由 systemd/人工负责。
            # 这里按端口短超时探测就绪状态：未就绪仅打 warning 并继续，
            # Hub 内向量化线程会自行无限重试连接，不阻塞主服务启动。
            if _probe_tcp_port('127.0.0.1', int(vector_db_port)):
                logger.info(
                    f"VectorDB port 127.0.0.1:{vector_db_port} is reachable "
                    f"(process not detected by name, likely started via `python -m` or supervisor).")
            else:
                logger.warning(
                    f"VectorDB 服务未就绪（127.0.0.1:{vector_db_port} 不可连接）。"
                    f"请用 `python -m VectorDB.VectorDBBService --db-path ... --model ...` 启动；"
                    f"向量化线程将在后台持续重试，不影响主服务启动。"
                )

        vector_db_client = VectorDBClient(f"http://localhost:{str(vector_db_port)}")

    return vector_db_client


def start_intelligence_hub_service(config) -> Tuple[IntelligenceHub, IntelligenceHubWebService, AIClientManager]:

    # ------------------------------- AI Service -------------------------------

    # 护栏（M1/M3）：先读 china_watch 配置块，判定旧 Worker 是否实际运行，
    # 再构建 AI 客户端管理器（P0-3：drainer/DRY_RUN/只采集模式不要求
    # _config/ai_client_config.py，避免新消费者对旧 AI 配置的不必要依赖）。
    cw_cfg_pre = config.get('intelligence_hub.china_watch', {}) or {}
    legacy_ai_active = bool(cw_cfg_pre.get('api_enabled', False)) \
        and not bool(cw_cfg_pre.get('api_dry_run', True)) \
        and not bool(cw_cfg_pre.get('use_off_peak_drainer', False))
    client_manager = build_ai_client_manager(config, legacy_ai_active=legacy_ai_active)
    client_manager.start_monitoring()

    # ------------------------------- Vector DB --------------------------------

    vector_db_client = check_start_vector_db_service(config)

    # ------------------------------- Core: IHub -------------------------------

    ai_analysis_thread = config.get('intelligence_hub.ai_analysis_thread', 1)
    ref_host_url = config.get('intelligence_hub_web_service.service.host_url', 'http://127.0.0.1:5000')

    # D1: MongoDB 配置键与 config_example.json 对齐（示例位于 intelligence_hub.mongodb.*）
    # 支持环境变量覆盖（Docker 部署用），优先级：环境变量 > config.json
    mongodb_host = os.getenv('MONGO_HOST', '') or config.get('intelligence_hub.mongodb.host', 'localhost')
    mongodb_port = int(os.getenv('MONGO_PORT', '') or config.get('intelligence_hub.mongodb.port', 27017))
    mongodb_user = os.getenv('MONGO_USER', '') or config.get('intelligence_hub.mongodb.user', '')
    mongodb_pass = os.getenv('MONGO_PASSWORD', '') or config.get('intelligence_hub.mongodb.password', '')

    # 护栏（M1/M3）：读取 china_watch 配置块并注入 Hub。
    # api_enabled=false 时 Hub 不启动 AI worker（只采集模式，不产生任何 AI 调用）。
    cw_cfg = config.get('intelligence_hub.china_watch', {}) or {}
    hub_api_enabled = bool(cw_cfg.get('api_enabled', False))
    hub_api_dry_run = bool(cw_cfg.get('api_dry_run', True))

    # M3 接线：组装全部护栏组件（互斥校验 fail-fast、峰谷、预算、分级路由、
    # 预筛、AI 延期队列、真实客户端与闲时 Drainer）。
    guardrails = build_china_watch_guardrails(
        config, mongodb_host, mongodb_port, mongodb_user, mongodb_pass)

    # Hub 行为参数：与 Hub 批次 __init__ kwargs 默认值保持一致（5 / 10000 / True），
    # 配置缺失也能跑；config_example.json 中有对应条目。
    hub_archive_retry_max = int(config.get('intelligence_hub.archive_retry_max', 5))
    hub_max_unarchived_load = int(config.get('intelligence_hub.max_unarchived_load', 10000))
    hub_similar_recall_enabled = bool(config.get('intelligence_hub.similar_recall_enabled', True))

    hub = IntelligenceHub(
        ref_url=ref_host_url,

        api_enabled=hub_api_enabled,
        api_dry_run=hub_api_dry_run,

        # M3 接线：护栏组件注入（峰谷/预算/路由/预筛/Drainer 与互斥开关）
        time_policy=guardrails['time_policy'],
        budget_guard=guardrails['budget_guard'],
        model_router=guardrails['model_router'],
        prefilter=guardrails['prefilter'],
        off_peak_drainer=guardrails['drainer'],
        use_legacy_ai_worker=guardrails['use_legacy_ai_worker'],
        use_off_peak_drainer=guardrails['use_off_peak_drainer'],

        # P1-7/P1-8 接线：推送（归档后入队）与磁盘守卫（70/85/95 降级）
        push_notifier=guardrails.get('push_notifier'),
        disk_guard=guardrails.get('disk_guard'),
        disk_guard_interval=int(config.get('intelligence_hub.disk_guard.interval_seconds', 300)),

        archive_retry_max=hub_archive_retry_max,
        max_unarchived_load=hub_max_unarchived_load,
        similar_recall_enabled=hub_similar_recall_enabled,

        vector_db_client=vector_db_client,

        db_cache=MongoDBStorage(
            host=mongodb_host,
            port=mongodb_port,
            db_name='IntelligenceIntegrationSystem',
            username=mongodb_user,
            password=mongodb_pass,
            collection_name='intelligence_cached',
            # P1-四 修复：cache 集合索引（UUID 查重 / 重启恢复扫描）
            indexes=[
                [('UUID', 1)],
                [(f'APPENDIX.{APPENDIX_ARCHIVED_FLAG}', 1), ('UUID', 1)],
            ]),

        db_archive=MongoDBStorage(
            host=mongodb_host,
            port=mongodb_port,
            db_name='IntelligenceIntegrationSystem',
            username=mongodb_user,
            password=mongodb_pass,
            collection_name='intelligence_archived',
            # P1-四 修复：archive 集合索引
            # - UUID：详情查询与查重（原为零索引全表扫描）
            # - APPENDIX.__TIME_ARCHIVED__：按归档时间排序的主查询路径
            # - INFORMANT：查重辅助
            indexes=[
                [('UUID', 1)],
                [(f'APPENDIX.{APPENDIX_TIME_ARCHIVED}', -1)],
                [('INFORMANT', 1), ('UUID', 1)],
            ]),

        db_low_value=MongoDBStorage(
            host=mongodb_host,
            port=mongodb_port,
            db_name='IntelligenceIntegrationSystem',
            username=mongodb_user,
            password=mongodb_pass,
            collection_name='intelligence_low_value'),

        db_recommendation=MongoDBStorage(
            host=mongodb_host,
            port=mongodb_port,
            db_name='IntelligenceIntegrationSystem',
            username=mongodb_user,
            password=mongodb_pass,
            collection_name='intelligence_recommendation'),

            ai_client_manager = client_manager
    )

    # M3 接线：绑定 Drainer 业务回调（回调是 Hub 实例方法，须在 Hub 构造后绑定）。
    # invoke_handler 负责构造 prompt、调用客户端与解析业务结果；
    # on_complete 负责把分析结果送入 processed_queue 走归档链路。
    # 批次二：estimate_messages_fn 提供与实际发送一致的预估输入
    # （prompt+正文）与输出上限（MAX_OUTPUT_TOKEN）；
    # 批次四：pause_predicate 在磁盘 >=95% 时暂停认领新任务与新增付费请求。
    if guardrails['drainer'] is not None:
        guardrails['drainer'].invoke_handler = hub._drainer_invoke_handler
        guardrails['drainer'].on_complete = hub._drainer_on_complete
        guardrails['drainer'].estimate_messages_fn = hub._drainer_estimate_messages
        # 批次三：归档最终确认回调——DELIVERED 后崩溃的项由对账重放补投
        guardrails['drainer'].archive_confirm_fn = hub._drainer_archive_confirmed
        disk_guard = guardrails.get('disk_guard')
        if disk_guard is not None:
            guardrails['drainer'].pause_predicate = (
                lambda dg=disk_guard: dg.should_stop_collect)

    hub.startup(ai_analysis_thread)

    # ----------------------- Main Service and Access Control -----------------------

    rpc_api_tokens = config.get('intelligence_hub_web_service.rpc_api.tokens', [])
    collector_tokens = config.get('intelligence_hub_web_service.collector.tokens', [])
    processor_tokens = config.get('intelligence_hub_web_service.processor.tokens', [])

    rss_base_url = config.get('intelligence_hub_web_service.rss.host_prefix', 'http://127.0.0.1:5000')
    public_search_limits = config.get('intelligence_hub_web_service.public_search', None)

    access_manager = WebServiceAccessManager(
        rpc_api_tokens=rpc_api_tokens,
        collector_tokens=collector_tokens,
        processor_tokens=processor_tokens,
        user_manager=UserManager(DEFAULT_USER_DB_PATH),
        deny_on_empty_config=True)

    hub_service = IntelligenceHubWebService(
        intelligence_hub = hub,
        access_manager=access_manager,
        # RSSPublisher 实例由 Web 服务的 /rssfeed.xml 路由使用（Web 批次已接通）
        rss_publisher=RSSPublisher(rss_base_url),
        public_search_limits=public_search_limits,
        # 注入全局配置：使 trusted_proxies 等读取点生效（Web 批次已支持 config 参数）
        config=config,
    )

    hub_service.register_routers(wsgi_app)

    # --------------------------------- End of Init ---------------------------------

    return hub, hub_service, client_manager


# ----------------------------------------------------------------------------------------------------------------------


# ------------------------------------- Path --------------------------------------

def build_dirs():
    # TODO: All not-project files will be put in this path. It's good for docker deployment.
    Path(LOG_PATH).mkdir(parents=True, exist_ok=True)
    Path(DATA_PATH).mkdir(parents=True, exist_ok=True)
    Path(CONFIG_PATH).mkdir(parents=True, exist_ok=True)
    Path(EXPORT_PATH).mkdir(parents=True, exist_ok=True)
    Path(PRODUCTS_PATH).mkdir(parents=True, exist_ok=True)


# -------------------------------------- Log --------------------------------------

IIS_LOG_FILE = os.path.join(LOG_PATH, 'iis.log')
HISTORY_LOG_FOLDER = os.path.join(LOG_PATH, 'history_log')


def config_log():
    backup_and_clean_previous_log_file(IIS_LOG_FILE, HISTORY_LOG_FOLDER)

    setup_logging(IIS_LOG_FILE)
    setup_performance_logging(os.path.join(LOG_PATH, 'perf.log'))

    # Disable 3-party library's log
    limit_logger_level("core")
    limit_logger_level("base")
    limit_logger_level("asyncio")
    limit_logger_level("pymongo")
    limit_logger_level("waitress")
    limit_logger_level("connectionpool")
    limit_logger_level("WaitressServer")
    limit_logger_level("proactor_events")

    limit_logger_level("urllib3")
    limit_logger_level("urllib3.connection")
    limit_logger_level("urllib3.connectionpool")
    limit_logger_level("urllib3.poolmanager")
    limit_logger_level("urllib3.response")
    limit_logger_level("urllib3.util.retry")

    # My modules
    limit_logger_level("Tools.RequestTracer")
    limit_logger_level("Tools.DateTimeUtility")
    limit_logger_level("PyLoggingBackend.LoggerBackend")
    limit_logger_level("AIClientCenter.AIServiceTokenRotator")


# --------------------------------- 优雅关闭 ---------------------------------

_shutdown_hook_fired = threading.Event()


def _install_shutdown_hooks(hub: IntelligenceHub):
    """注册 SIGTERM/SIGINT 信号处理与 atexit，触发 hub.shutdown() 优雅关闭。

    - 幂等：内部用 Event 保证 hub.shutdown() 只执行一次，atexit 与信号处理不会重复调用；
      Flask 的 teardown 回调不涉及 Hub 生命周期，不存在重复关闭路径。
    - signal 只能在主线程注册：Waitress/Flask 适配器在工作线程中 import 本模块，
      此时跳过信号注册（仅 atexit 生效），由外层 Launcher 负责信号与进程退出。
    """
    def _graceful_shutdown(reason: str):
        if _shutdown_hook_fired.is_set():
            return
        _shutdown_hook_fired.set()
        logger.info(f"开始优雅关闭 IntelligenceHub（{reason}）...")
        try:
            hub.shutdown()
        except Exception:
            logger.exception(f"Hub 关闭过程异常（{reason}）。")

    # atexit 兜底：正常退出 / sys.exit / Launcher 整进程退出时均会触发
    atexit.register(_graceful_shutdown, 'atexit')

    if threading.current_thread() is threading.main_thread():
        def _signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，进入优雅关闭流程。")
            _graceful_shutdown(f'signal {signum}')
            # 优雅关闭完成后以 0 退出：systemd 的 SIGTERM 停止视为正常结束
            sys.exit(0)

        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(_sig, _signal_handler)
            except Exception as e:
                logger.warning(f"注册信号处理失败 {_sig}: {e}")
    else:
        logger.info("当前非主线程，跳过 SIGTERM/SIGINT 注册（atexit 兜底仍生效）。")


def create_wsgi_app():
    """WSGI app 工厂：引导 Hub 并注册全部业务路由。

    修复 launcher 部署缺陷：Waitress/Flask/Gunicorn 分支此前只加载模块级 wsgi_app
    （仅有 /static 路由），业务路由注册在 run() 中、依赖 __main__ 保护才执行，
    导致 gunicorn 模式（含群晖容器部署）下 Web 服务全部 404。
    本工厂在 WSGI 加载路径上完成与 run() 相同的引导（Hub 组装、LoggerBackend、
    AI 客户端看板、MonitorAPI），保证非 __main__ 部署也具备完整路由。
    仅允许引导一次：重复调用直接返回已引导的 wsgi_app（防重复初始化）。
    """
    global _configured_secret_key, _wsgi_app_bootstrapped
    if _wsgi_app_bootstrapped:
        return wsgi_app
    _wsgi_app_bootstrapped = True

    build_dirs()
    config_log()

    config = EasyConfig(
        config_file=DEFAULT_CONFIG_FILE,
        config_file_alter=DEFAULT_ALTER_CONFIG_FILE)

    # SECRET_KEY 配置化（与 run() 保持一致）
    _configured_secret_key = config.get('intelligence_hub_web_service.service.secret_key', '')
    if _configured_secret_key:
        wsgi_app.secret_key = _configured_secret_key
    wsgi_app.config['SESSION_COOKIE_SECURE'] = bool(config.get(
        'intelligence_hub_web_service.session_cookie_secure',
        config.get('intelligence_hub_web_service.service.session_cookie_secure', False)))
    if _configured_secret_key and (_configured_secret_key.startswith('CHANGE_THIS')
                                   or len(_configured_secret_key) < 32):
        logger.critical(
            "SECRET_KEY 强度不足（占位值或长度 < 32 字符），Session 签名存在被伪造风险！"
            "请在 config.json 的 intelligence_hub_web_service.service.secret_key "
            "配置至少 32 字符的随机字符串。")

    logger.info("create_wsgi_app: bootstrapping hub and registering routes...")

    # 引导 Hub（与 run() 相同）：组装服务并注册全部业务路由
    ihub, ihub_service, client_manager = start_intelligence_hub_service(config)

    log_backend = LoggerBackend(monitoring_file_path=IIS_LOG_FILE, cache_limit_count=100000,
                                link_file_roots={
                                    'conversation': os.path.abspath('conversation')
                                },
                                project_root=PRJ_PATH,
                                with_logger_manager=True)
    log_backend.register_router(app=wsgi_app, wrapper=ihub_service.access_manager.login_required)

    client_manager_backend = AIDashboardService(client_manager)
    client_manager_backend.mount_to_app(
        app=wsgi_app,
        wrapper=ihub_service.access_manager.login_required,
        url_prefix='/monitor/ai-client-dashboard')

    def _resolve_thread_name(tid: int):
        """将原生线程 ID 解析为 Python 线程名（仅对本进程有效）。"""
        for t in threading.enumerate():
            if t.ident == tid:
                return t.name
        return None

    monitor_api = MonitorAPI(
        app=wsgi_app,
        wrapper=ihub_service.access_manager.login_required,
        prefix='/monitor',
        thread_name_resolver=_resolve_thread_name,
        update_interval_sec=config.get('monitor.update_interval_sec', 5.0),
        thread_stats_interval_sec=config.get('monitor.thread_stats_interval_sec', 10.0),
    )
    monitor_api.monitor.add_process(os.getpid())
    monitor_api.start()
    ihub_service.set_monitor_api(monitor_api)

    logger.info("create_wsgi_app: hub bootstrapped, routes registered.")
    return wsgi_app


def run():
    build_dirs()
    config_log()

    # --------------------------------- Config --------------------------------

    config = EasyConfig(
        config_file=DEFAULT_CONFIG_FILE,
        config_file_alter=DEFAULT_ALTER_CONFIG_FILE
    )

    # SECRET_KEY 配置化：从配置文件读取
    global _configured_secret_key
    _configured_secret_key = config.get('intelligence_hub_web_service.service.secret_key', '')

    # wsgi_app 在模块导入阶段已创建，此处必须显式回写配置，否则即便
    # config.json 配置了固定密钥，实际 Session 仍会继续使用启动时的随机值。
    if _configured_secret_key:
        wsgi_app.secret_key = _configured_secret_key

    # SESSION_COOKIE_SECURE 配置化：优先读顶层键（配置示例的规范位置），
    # 兼容旧的 service 子键；默认 False 保持纯 HTTP 部署兼容。仅在 HTTPS 部署时开启。
    wsgi_app.config['SESSION_COOKIE_SECURE'] = bool(config.get(
        'intelligence_hub_web_service.session_cookie_secure',
        config.get('intelligence_hub_web_service.service.session_cookie_secure', False)))

    if _configured_secret_key:
        # 强度检查：占位值（未修改的示例值）或过短密钥会让 Session 签名可被伪造。
        # 只打 CRITICAL 告警，不阻断启动（避免锁死现有部署）。
        if _configured_secret_key.startswith('CHANGE_THIS') or len(_configured_secret_key) < 32:
            logger.critical(
                "SECRET_KEY 强度不足（占位值或长度 < 32 字符），Session 签名存在被伪造风险！"
                "请立即在 config.json 的 intelligence_hub_web_service.service.secret_key "
                "配置至少 32 字符的随机字符串。"
            )
        else:
            logger.info("使用配置文件中的 SECRET_KEY，重启后 Session 将保持有效。")
    else:
        logger.warning(
            "未配置 SECRET_KEY，使用随机值。每次重启后所有用户 Session 将失效。\n"
            "建议在 config.json 中配置 intelligence_hub_web_service.service.secret_key。"
        )

    logger.info('Apply config: ')
    logger.info(config.dump_text())

    # -------------------------------- Service ---------------------------------

    ihub, ihub_service, client_manager = start_intelligence_hub_service(config)

    log_backend = LoggerBackend(monitoring_file_path=IIS_LOG_FILE, cache_limit_count=100000,
                                link_file_roots={
                                    'conversation': os.path.abspath('conversation')
                                },
                                project_root=PRJ_PATH,
                                with_logger_manager=True)
    log_backend.register_router(app=wsgi_app, wrapper=ihub_service.access_manager.login_required)

    client_manager_backend = AIDashboardService(client_manager)
    client_manager_backend.mount_to_app(
        app=wsgi_app,
        wrapper=ihub_service.access_manager.login_required,
        url_prefix='/monitor/ai-client-dashboard')

    # Monitor in the same process and the same service
    def _resolve_thread_name(tid: int):
        """将原生线程 ID 解析为 Python 线程名（仅对本进程有效）。"""
        for t in threading.enumerate():
            if t.ident == tid:
                return t.name
        return None

    monitor_update_interval = config.get('monitor.update_interval_sec', 5.0)
    monitor_thread_interval = config.get('monitor.thread_stats_interval_sec', 10.0)
    monitor_api = MonitorAPI(
        app=wsgi_app,
        wrapper=ihub_service.access_manager.login_required,
        prefix='/monitor',
        thread_name_resolver=_resolve_thread_name,
        update_interval_sec=monitor_update_interval,
        thread_stats_interval_sec=monitor_thread_interval,
    )
    self_pid = os.getpid()
    logger.info(f'Service PID: {self_pid}')
    monitor_api.monitor.add_process(self_pid)
    monitor_api.start()
    ihub_service.set_monitor_api(monitor_api)

    # --------------------------- systemd watchdog ---------------------------
    watchdog_enabled = config.get('intelligence_hub.systemd_watchdog.enabled', True)
    watchdog_interval = config.get('intelligence_hub.systemd_watchdog.interval_sec', 10)
    watchdog_stop_event = None

    def _watchdog_worker():
        """后台线程：定时向 systemd 发送 WATCHDOG=1。"""
        if not is_watchdog_enabled():
            logger.info("systemd watchdog not enabled (NOTIFY_SOCKET/WATCHDOG_USEC not set). Skipping.")
            return

        notify_ready()
        logger.info(f"systemd watchdog started, interval={watchdog_interval}s")
        while not watchdog_stop_event.is_set():
            notify_alive()
            watchdog_stop_event.wait(watchdog_interval)
        notify_stopping()

    if watchdog_enabled:
        watchdog_stop_event = threading.Event()
        threading.Thread(name='SystemdWatchdog', target=_watchdog_worker, daemon=True).start()
    else:
        logger.info("systemd watchdog is disabled by config.")

    # ------------------------------------------------------------------------

    # 优雅关闭接线：SIGTERM/SIGINT + atexit → hub.shutdown()
    _install_shutdown_hooks(ihub)

    # daemon=True：主线程退出时该统计线程不阻止进程结束（循环自身也检查 shutdown_flag）
    threading.Thread(name='ShowStatistics', daemon=True,
                     target=partial(show_intelligence_hub_statistics_forever, ihub)).start()

# 模块级直接 run() 的写法导致本模块无法被安全导入（import 即启动服务并可能
# sys.exit），也使单元测试无法导入 build_china_watch_guardrails 等组装函数。
# 加 __main__ 保护：直接运行行为不变，作为模块导入不再产生副作用。
if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        print(str(e))
        print(traceback.format_exc())
        # 启动失败必须以非零码退出，systemd Restart=on-failure 才能触发自动重启
        sys.exit(1)
    finally:
        # 在进程退出前通知 systemd（如果启用 watchdog），这是 no-op on Windows/non-systemd。
        try:
            from Tools.SystemdWatchdog import notify_stopping
            notify_stopping()
        except Exception as e:
            # 退出路径上的收尾通知失败不应阻断进程退出（Windows/非 systemd 环境本就是 no-op）
            logger.debug(f"notify_stopping() failed during shutdown: {e}")
