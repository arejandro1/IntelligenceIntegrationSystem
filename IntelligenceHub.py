import os
import uuid
import json
import queue
import random
import logging
import pymongo
import traceback
import threading
# P-review 修复：显式导入 time/datetime，不再依赖
# `from ServiceComponent.IntelligenceHubDefines_v2 import *` 的模块级泄漏。
import time
import datetime

from concurrent.futures import ThreadPoolExecutor

from attr import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from pymongo.errors import ConnectionFailure
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_result

from GlobalConfig import EXPORT_PATH, DATA_PATH
from ServiceComponent.DynamicGraphEngine import DynamicGraphEngine
from prompts_v2x import ANALYSIS_PROMPT_TABLE
from Tools.MongoDBAccess import MongoDBStorage
from VectorDB.VectorDBClient import VectorDBClient
from ServiceComponent.IntelligenceHubDefines_v2 import *
from MyPythonUtility.DictTools import check_sanitize_dict, DictPrinter
from AIClientCenter.AIClientManager import AIClientManager
from MyPythonUtility.AdvancedScheduler import AdvancedScheduler
from ServiceComponent.IntelligenceAnalyzerProxy import analyze_with_ai
from ServiceComponent.IntelligenceQueryEngine import IntelligenceQueryEngine
from ServiceComponent.IntelligenceScoringEngine import IntelligenceScoringEngine
from ServiceComponent.IntelligenceVectorDBEngine import IntelligenceVectorDBEngine
from Tools.PerformanceLogger import get_performance_logger
from ServiceComponent.IntelligenceStatisticsEngine import IntelligenceStatisticsEngine
from ServiceComponent.AsyncTranslationPatch import AsyncTranslationPatch, needs_translation
from ServiceComponent.IntelligenceAggregationEngine import IntelligenceAggregationEngine, generate_aggregation_plan
from ServiceComponent.IntelligenceEntityFrequencyEngine import EntityFrequencyEngine
from Tools.DateTimeUtility import Clock, time_str_to_datetime, get_aware_time, time_digit_list_to_datetime
from Tools.ProcessCotrolException import positioning_exception_context, ProcessSkip, PositioningException

# 批次一/批次二修复：统一 AI 调用控制与费用台账状态常量。
# DeepSeekModelRouter 惰性导入（模块顶层无重依赖），此处只引入轻量常量模块。
from ServiceComponent.ApiUsageLedger import STATUS_UNKNOWN_PRICE


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class IndexedQueue(queue.Queue):
    """带查重索引的线程安全队列（P 优化：队列查重 O(n) 锁内扫描 → O(1)）。

    在 queue.Queue 官方定制点 _put/_get（两者均在队列互斥锁 self.mutex 持有
    期间被 put()/get() 调用）中同步维护 UUID 与 informant 的计数索引，
    保证索引与队列内容始终一致，且与队列操作处于同一把锁内。

    查重语义与旧的"锁内逐项扫描比较"严格一致：
    - 键取自 item 的 'UUID' 与传入 informant_key 对应字段
      （'informant' 或 'INFORMANT'，两键名分命名空间索引，
      查询按调用方传入的键名匹配，不跨键名命中）的字符串原值
      （入队侧 submit 已把 informant 规范化写回，查询侧传规范化值；
      仅接受非空 str，避免类型强转引入旧扫描不存在的命中）；
    - UUID 或 informant 任一命中即视为重复；
    - 计数而非集合：同一键重复入队多条时，取出一条后其余条目仍可命中。
    """

    def __init__(self, maxsize: int = 0):
        super().__init__(maxsize=maxsize)
        # 键 -> 剩余出现次数；仅在持有 self.mutex 时读写
        self._dup_index: Dict[tuple, int] = {}

    @staticmethod
    def _extract_dup_keys(item: Any) -> List[tuple]:
        """提取查重索引键：UUID 与 informant/INFORMANT（原值，键名即命名空间）。"""
        if not isinstance(item, dict):
            return []
        keys = []
        uuid_value = item.get('UUID')
        if isinstance(uuid_value, str) and uuid_value:
            keys.append(('uuid', uuid_value))
        for field in ('informant', 'INFORMANT'):
            value = item.get(field)
            if isinstance(value, str) and value:
                keys.append((field, value))
        return keys

    def _put(self, item):
        # queue.Queue 官方定制点：调用时已持有 self.mutex（not_full 条件锁）
        super()._put(item)
        for key in self._extract_dup_keys(item):
            self._dup_index[key] = self._dup_index.get(key, 0) + 1

    def _get(self):
        # queue.Queue 官方定制点：调用时已持有 self.mutex（not_empty 条件锁）
        item = super()._get()
        for key in self._extract_dup_keys(item):
            remaining = self._dup_index.get(key, 0) - 1
            if remaining > 0:
                self._dup_index[key] = remaining
            else:
                self._dup_index.pop(key, None)
        return item

    def contains_identifier(self, target_uuid: str, target_informant: str,
                            informant_key: str = 'informant') -> bool:
        """O(1) 判定队列中是否存在 UUID 或 informant 任一命中的项。

        informant 按 informant_key 指定的键名命名空间匹配
        （'informant'/'INFORMANT' 不互通，与旧扫描 item.get(informant_key) 一致）。
        在队列互斥锁内查询，避免与并发 put/get 产生竞态。
        """
        with self.mutex:
            if target_uuid and ('uuid', target_uuid) in self._dup_index:
                return True
            if target_informant and (informant_key, target_informant) in self._dup_index:
                return True
            return False


class _DrainerClientAdapter:
    """把 DeepSeekClient/MockDeepSeekClient 适配为 analyze_with_ai 需要的客户端风格（M3 接线）。

    analyze_with_ai 只依赖 chat(messages, temperature, max_tokens)；
    Hub 侧 APPENDIX 追加还用到 get_api_base_url()/get_current_model()。
    DeepSeek 客户端返回的 OpenAI 风格响应（choices/usage）可直接透传给
    conversation_common_process 解析；last_response 缓存最近一次原始响应，
    供 OffPeakQueueDrainer 从 usage 中提取 token 计量记账。
    """

    def __init__(self, client: Any, model: str, base_url: str = 'deepseek-api'):
        self._client = client
        self._model = model
        self._base_url = base_url
        self.last_response: Optional[dict] = None

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0,
             max_tokens: Optional[int] = None) -> dict:
        resp = self._client.chat_completion(
            messages=messages, model=self._model,
            temperature=temperature, max_tokens=max_tokens)
        self.last_response = resp if isinstance(resp, dict) else None
        return resp

    def get_api_base_url(self) -> str:
        return self._base_url

    def get_current_model(self) -> str:
        return self._model


class _UsageCapturingClient:
    """包装任意 AI 客户端：缓存最近一次 chat() 的原始响应（含 usage）。

    批次一修复：旧 AI Worker 路径此前只检查预算而不记录消费。
    本包装器让 Hub 在分析完成后能从原始响应中提取 token 用量，
    记入与 Drainer/翻译共用的同一费用台账。
    透传其余属性/方法（get_api_base_url、validate_response 等）给被包装客户端。
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.last_response: Optional[dict] = None
        self.last_model: str = ''

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0,
             max_tokens: Optional[int] = None) -> dict:
        resp = self._inner.chat(messages=messages, temperature=temperature,
                                max_tokens=max_tokens)
        self.last_response = resp if isinstance(resp, dict) else None
        try:
            self.last_model = str(self._inner.get_current_model())
        except Exception:
            self.last_model = ''
        return resp

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class IntelligenceHub:
    @dataclass
    class Error:
        exception: Exception | None = None
        # P2 修复：可变默认值 [] 在 dataclass 中会跨实例共享（attr.dataclass 同样适用），
        # 一处 append 会污染所有后续实例的空列表。改用工厂函数。
        error_list: List[str] = field(factory=list)
        warning_list: List[str] = field(factory=list)

        def __bool__(self):
            return False

    class Exception(Exception):
        def __init__(self, name: str, message: str = '', *args, **kwargs):
            self.name = name
            self.msg = message
            self.args = args
            self.kwargs = kwargs

        def __str__(self):
            return f"[{self.name}]: {self.args}, {self.kwargs}"

    def __init__(self, *,
                 # 默认值仅供未显式传入时兜底；实际由 IntelligenceHubStartup 从
                 # intelligence_hub_web_service.service.host_url 注入。
                 ref_url: str = 'http://localhost:5000',
                 vector_db_client: Optional[VectorDBClient] = None,
                 db_cache: Optional[MongoDBStorage] = None,
                 db_archive: Optional[MongoDBStorage] = None,
                 db_low_value: Optional[MongoDBStorage] = None,
                 ai_client_manager: AIClientManager = None,
                 api_enabled: bool = False,
                 api_dry_run: bool = True,
                 time_policy=None,
                 budget_guard=None,
                 model_router=None,
                 prefilter=None,
                 off_peak_drainer=None,
                 use_legacy_ai_worker: bool = True,
                 use_off_peak_drainer: bool = False,
                 push_notifier=None,
                 disk_guard=None,
                 **kwargs):
        """
        Init IntelligenceHub.
        :param ref_url: The reference url for sub-resource url generation.
        :param vector_db_client: Vector DB for text RAG indexing.
        :param db_cache: The mongodb for caching collected data.
        :param db_archive: The mongodb for archiving processed data.
        :param db_low_value: The mongodb for saving low-value data.
        :param ai_client_manager: The openai-like client for data processing.
        :param kwargs: Extra but not key parameters.
        """

        # ---------------- Parameters ----------------

        self.reference_url = ref_url
        self.perf_logger = get_performance_logger()
        self.vector_db_client = vector_db_client
        self.mongo_db_cache = db_cache
        self.mongo_db_archive = db_archive
        self.mongo_db_low_value = db_low_value
        self.ai_client_manager = ai_client_manager

        # 护栏参数（M1/M3）：
        # - api_enabled=False 时不启动 AI 分析线程（只采集模式，不产生真实 API 调用）
        # - api_dry_run=True 时旧 Worker 进入仅采集模式，不消费 original_queue 调 AI
        # - time_policy 为 ApiTimePolicy 实例，控制非高峰才允许调用
        self.api_enabled = api_enabled
        self.api_dry_run = api_dry_run
        self.time_policy = time_policy
        # M3 接线新增护栏组件（均由 IntelligenceHubStartup 按 config 实例化注入）：
        # - budget_guard：BudgetGuard，预算耗尽时旧 Worker 停止取数调用
        # - model_router：DeepSeekModelRouter，Flash/Pro 分级路由与费用台账
        # - prefilter：RulePreFilter，Hub 侧唯一权威预筛（None 时保持旧行为）
        # - off_peak_drainer：OffPeakQueueDrainer，AI 延期队列闲时消费者
        # - use_legacy_ai_worker / use_off_peak_drainer：新旧消费者互斥开关
        #   （互斥校验由 Startup 调用 ConsumerMutex 完成，此处仅行为分派）
        self.budget_guard = budget_guard
        self.model_router = model_router
        self.prefilter = prefilter
        self.off_peak_drainer = off_peak_drainer
        self.use_legacy_ai_worker = bool(use_legacy_ai_worker)
        self.use_off_peak_drainer = bool(use_off_peak_drainer)
        # P1-7/P1-8 接线：推送服务（归档成功后产生推送任务）与磁盘守卫
        # （70/85/95 三级降级）。均由 IntelligenceHubStartup 组装注入；
        # None 时对应功能禁用，主链路不受影响。
        self.push_notifier = push_notifier
        self.disk_guard = disk_guard
        self.push_task_count = 0

        # 预筛三分类计数（经 statistics 暴露，供监控与验收）
        self.prefilter_rejected_count = 0
        self.prefilter_metadata_only_count = 0
        self.prefilter_needs_ai_count = 0

        # -------------- Queues Related --------------

        self.original_queue = IndexedQueue(maxsize=2000)         # Original intelligence queue
        self.processed_queue = IndexedQueue(maxsize=2000)        # Processed intelligence queue
        self.unarchived_queue = IndexedQueue()                   # Loaded unarchived data queue, lower priority than original_queue

        self.vectorize_queue = queue.Queue(maxsize=1000)        # Queue for vector DB

        self.archived_counter = 0
        self.drop_counter = 0
        self.error_counter = 0

        self.conversation_warning = 0
        self.conversation_error = 0
        self.conversation_total = 0

        # P-review 修复：vectorize_drop_count 原在 _index_archived_data 中首次引用，
        # __init__ 未初始化，队列满时 AttributeError。在此初始化并纳入 statistics。
        self.vectorize_drop_count = 0

        # 恢复/重试参数（可由 IntelligenceHubStartup 经 kwargs 注入 config 值，
        # 对应 intelligence_hub.archive_retry_max / max_unarchived_load / similar_recall_enabled）：
        self.archive_retry_max = int(kwargs.get('archive_retry_max', 5))
        self.max_unarchived_load = int(kwargs.get('max_unarchived_load', 10000))
        self.similar_recall_enabled = bool(kwargs.get('similar_recall_enabled', True))
        # P1-8：磁盘守卫检测间隔（intelligence_hub.disk_guard.interval_seconds）
        self.disk_guard_interval = float(kwargs.get('disk_guard_interval', 300.0))

        # 向量检索降级状态：最近一次引擎查询异常（None 表示健康），
        # 用于区分"无结果"与"服务故障"，经 statistics 暴露。
        self.last_vector_error: Optional[str] = None

        # AI 分析线程句柄（shutdown 时 join）
        self._analysis_threads: List[threading.Thread] = []

        # 相似消息召回线程池：对引擎 query 施加 5s 超时，
        # 向量服务慢/挂时绝不阻断 AI 分析主流程。
        self._similar_recall_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='SimilarRecall')

        # --------------- Components ----------------

        self.cache_db_query_engine = IntelligenceQueryEngine(self.mongo_db_cache)
        self.archive_db_query_engine = IntelligenceQueryEngine(self.mongo_db_archive)
        self.archive_db_statistics_engine = IntelligenceStatisticsEngine(self.mongo_db_archive)

        self.entity_frequency_engine = EntityFrequencyEngine(
            db_path=os.path.join(DATA_PATH, 'entity_frequency.db'),
            mongo_db_archive=self.mongo_db_archive,
        )

        self.vector_db_engine_summary: Optional[IntelligenceVectorDBEngine] = None
        self.vector_db_engine_full_text: Optional[IntelligenceVectorDBEngine] = None
        self.aggregation_engine_summary: Optional[IntelligenceAggregationEngine] = None

        self.scheduler = AdvancedScheduler(logger=logging.getLogger('Scheduler'))

        # Init when vector is ready.
        self.dynamic_graph_engine: Optional[DynamicGraphEngine] = None

        # ------------------ Loads ------------------

        self._load_unarchived_data()
        # self.intelligence_cache.load_cache()

        # ----------------- Threads -----------------

        self.lock = threading.Lock()
        self.shutdown_flag = threading.Event()

        self.post_process_thread = threading.Thread(name='PostProcessThread', target=self._post_process_worker, daemon=True)
        self.intelligence_vectorization_thread = threading.Thread(name='VectorizationThread', target=self._vectorization_thread, daemon=True)

        # ------------------ Tasks ------------------

        self._init_scheduler()

        # ------------ Translation Patch ------------

        # 批次一修复：后台翻译纳入统一 AI 调用控制。
        # - api_enabled=False 或 api_dry_run=True 时直接不启动翻译线程
        #   （翻译是归档后的增强功能，只采集/DRY_RUN 模式下没有真实调用的理由）；
        # - 真实模式下仍注入 call_gate（时段/预算/频率护栏）与 ledger_recorder
        #   （进入同一费用台账），防止翻译绕过预算限制。
        translation_call_allowed = bool(self.api_enabled and not self.api_dry_run)
        if translation_call_allowed:
            self.async_translation_patch = AsyncTranslationPatch(
                mongo_db_archive=self.mongo_db_archive,
                query_engine=self.archive_db_query_engine,
                ai_client_manager=ai_client_manager,
                shutdown_flag=self.shutdown_flag,
                on_patched=self._index_archived_data,  # 翻译后再触发 vectorize
                translated_revision="tr_patch_20260311",
                backfill_enabled=True,
                backfill_scan_limit_per_round=200,
                backfill_interval_sec=600,
                call_gate=self._translation_call_gate,
                ledger_recorder=self._record_translation_usage,
            )
            self.async_translation_patch.start()
        else:
            # 不创建实例：statistics 等处对 None 已有兜底
            self.async_translation_patch = None
            logger.info("AsyncTranslationPatch disabled (api_enabled=%s, api_dry_run=%s).",
                        self.api_enabled, self.api_dry_run)

        # 启动实体频率缓存预更新线程（最近30天）
        self._entity_freq_bootstrap_thread = threading.Thread(
            target=self._bootstrap_entity_frequency_cache,
            daemon=True,
            name="EntityFreqBootstrap"
        )
        self._entity_freq_bootstrap_thread.start()

        logger.info('***** IntelligenceHub init complete *****')

    # ----------------------------------------------------- Setups -----------------------------------------------------

    def _init_scheduler(self):
        self.scheduler.add_weekly_task(
            func=self._do_export_mongodb_weekly,
            task_id = 'export_mongodb_weekly_task',
            day_of_week='sun',
            use_new_thread=True
        )
        self.scheduler.add_monthly_task(
            func=self._do_export_mongodb_monthly,
            task_id = '_do_export_mongodb_monthly_task',
            day=1,
            use_new_thread=True
        )
        self.scheduler.add_hourly_task(
            func=self._do_run_summary_aggregation,
            task_id="summary_aggregation_hourly",
            use_new_thread=True
        )

        # P2 修复：移除 _do_translation_backfill 死任务注册（方法体后也已删除）。
        # 该方法引用 self.translation_patch（实际属性名为 async_translation_patch）
        # 且 backfill_archived 方法不存在，每小时空跑一次并吞掉 AttributeError。
        # AsyncTranslationPatch 已内置 backfill 线程（backfill_enabled=True），
        # 此注册完全多余。

        self.scheduler.add_hourly_task(
            func=self._do_build_entity_frequency_cache,
            task_id="entity_frequency_hourly",
            use_new_thread=True
        )

        self.scheduler.start_scheduler()

    def _load_unarchived_data(self):
        """Load unarchived data into a queue, compatible with both old and new archival markers.

        P-review 修复：
        - 恢复查询同时捞取「无标记」与「E(错误待重试)」文档，使 E 标记数据
          重启后能重新进入分析流程，闭环重试；
        - E 文档重投前在 cache 文档上累加 retry_count，达到 archive_retry_max
          后跳过并告警，避免坏数据无限重投；
        - 分批加载（游标 batch_size=500，按时间升序），总量上限
          max_unarchived_load，防止积压数据一次性撑爆内存。
        """
        if not self.mongo_db_cache:
            return

        try:
            # 兼容查询条件：同时支持旧版（顶层__ARCHIVED__）和新版（APPENDIX.__ARCHIVED__）。
            # 每种形态下都接受「标记不存在」或「标记为 E(待重试)」。
            query = {
                "$and": [
                    {"$or": [
                        # Old design: Flag is at root level
                        {APPENDIX_ARCHIVED_FLAG: {"$exists": False}},
                        {APPENDIX_ARCHIVED_FLAG: ARCHIVED_FLAG_ERROR},
                    ]},
                    {"$or": [
                        # New design: Flag is under "APPENDIX"
                        {f"APPENDIX.{APPENDIX_ARCHIVED_FLAG}": {"$exists": False}},
                        {f"APPENDIX.{APPENDIX_ARCHIVED_FLAG}": ARCHIVED_FLAG_ERROR},
                    ]},
                ]
            }

            loaded = 0
            skipped_retry_limit = 0
            cursor = (self.mongo_db_cache.collection
                      .find(query)
                      .sort('collect_time', pymongo.ASCENDING)
                      .batch_size(500))

            for doc in cursor:
                if loaded >= self.max_unarchived_load:
                    break

                appendix = doc.get('APPENDIX') or {}
                flag = doc.get(APPENDIX_ARCHIVED_FLAG, appendix.get(APPENDIX_ARCHIVED_FLAG))

                # E 标记文档：重投前累加 retry_count，超限跳过
                if flag == ARCHIVED_FLAG_ERROR:
                    retry_count = int(appendix.get(APPENDIX_ARCHIVED_RETRY, 0) or 0)
                    if retry_count >= self.archive_retry_max:
                        skipped_retry_limit += 1
                        logger.warning(
                            f"Unarchived doc {doc.get('UUID', '?')} retry_count={retry_count} "
                            f"reached limit {self.archive_retry_max}, skip reload.")
                        continue
                    retry_count += 1
                    try:
                        self.mongo_db_cache.update(
                            {'UUID': doc.get('UUID')},
                            {f'APPENDIX.{APPENDIX_ARCHIVED_RETRY}': retry_count})
                        appendix[APPENDIX_ARCHIVED_RETRY] = retry_count
                        doc['APPENDIX'] = appendix
                    except Exception as e:
                        logger.warning(f"Update retry_count fail for {doc.get('UUID', '?')}: {e}")

                doc['_id'] = str(doc['_id'])  # 转换ObjectId
                try:
                    self.unarchived_queue.put(doc, block=True, timeout=5)
                    loaded += 1
                except queue.Full:
                    logger.error("Queue full, failed to add document")
                    break

            # 超出上限/跳过的部分给出可见告警
            try:
                total_pending = self.mongo_db_cache.collection.count_documents(query)
            except Exception:
                total_pending = -1
            remaining = total_pending - loaded - skipped_retry_limit
            if remaining > 0:
                logger.warning(
                    f'Unarchived data truncated: loaded {loaded}, skipped {skipped_retry_limit} '
                    f'(retry limit), remaining {remaining} in cache db.')
            logger.info(f'Unarchived data loaded, item count: {loaded}')

        except pymongo.errors.PyMongoError as e:
            logger.error(f"Database operation failed: {str(e)}")

    # ----------------------------------------------- Startup / Shutdown -----------------------------------------------

    def startup(self, ai_analysis_thread: int):
        """
        ai_analysis_thread: The thread count of _ai_analysis_worker(), which should be less or equal to AI client count.

        护栏：api_enabled=False 时不启动 AI 分析线程（只采集模式，不产生任何 AI 调用）。
        post_process / vectorization 线程照常启动，保证已归档数据仍可被处理。

        M3 接线新增：
        - api_dry_run=True 视同只采集模式：不启动旧 AI Worker（fail-closed，
          修复原 api_dry_run 赋值后无任何代码读取、形同虚设的问题）。
        - use_off_peak_drainer=True 时由 Drainer 消费 AI 延期队列，
          旧 AI Worker 不启动（新旧消费者互斥，Startup 已做 fail-fast 校验）。
        - api_enabled=False 且 use_off_peak_drainer=True 是合法组合：
          Drainer 内部 router.can_call() 会因 api_enabled=False 拒绝真实调用，
          采集与入队照常，等待后续人工启用。
        - 批次一修复：旧、新消费者开关是独立开关——use_legacy_ai_worker=False
          且 use_off_peak_drainer=False 时，即使 api_enabled=true 也不启动
          任何分析消费者（原先只检查 use_off_peak_drainer，漏检旧开关，
          导致"总开关开+非模拟+双关"时旧线程仍被启动）。
        """
        # 消费者分派以显式开关为准（use_legacy_ai_worker / use_off_peak_drainer），
        # api_enabled/api_dry_run 只作为总闸（Fail-closed）叠加约束。
        start_legacy_worker = (
            self.use_legacy_ai_worker
            and self.api_enabled
            and not self.api_dry_run
            and not self.use_off_peak_drainer
        )

        if start_legacy_worker:
            self.start_analysis_threads(ai_analysis_thread)
        elif self.api_enabled and self.use_off_peak_drainer:
            logger.info("use_off_peak_drainer=true: 旧 AI Worker 未启动，由 OffPeakQueueDrainer 消费 AI 延期队列。")
        elif not self.use_legacy_ai_worker and not self.use_off_peak_drainer:
            logger.info("新旧 AI 消费者均未启用（use_legacy_ai_worker=false, "
                        "use_off_peak_drainer=false）：不启动任何分析消费者"
                        "（api_enabled=%s, api_dry_run=%s）。", self.api_enabled, self.api_dry_run)
        elif self.api_enabled and self.api_dry_run:
            logger.info("api_dry_run=true: AI 分析线程未启动（DRY_RUN 模式，数据保留在 cache 与队列）。")
        else:
            logger.info("api_enabled=false: AI 分析线程未启动（只采集模式）。")

        # M3 接线新增：启动闲时 Drainer（若注入）。互斥由上方分派与 Startup 双重保证。
        if self.off_peak_drainer is not None and self.use_off_peak_drainer:
            self.off_peak_drainer.start()
            logger.info("OffPeakQueueDrainer started.")

        self.post_process_thread.start()
        self.intelligence_vectorization_thread.start()

        # P1-8：磁盘守卫后台周期检测（70 告警 / 85 停推送 / 95 停采集）
        disk_guard = getattr(self, 'disk_guard', None)
        if disk_guard is not None:
            try:
                from ServiceComponent.DiskSpaceGuard import start_disk_guard_worker
                start_disk_guard_worker(
                    disk_guard,
                    interval_seconds=float(getattr(self, 'disk_guard_interval', 300.0)),
                    stop_event=self.shutdown_flag)
                # 启动时立即检测一次，避免首轮间隔内状态为空
                disk_guard.check()
                logger.info("DiskSpaceGuard started (watch=%s).", disk_guard.watch_path)
            except Exception as e:
                logger.warning("DiskSpaceGuard 启动失败（不影响主链路）：%s", e)

    def shutdown(self, timeout=10):
        logger.info("Intelligence hub shutting down...")

        # 设置关闭标志
        self.shutdown_flag.set()

        # Clear and persists unprocessed data. Put None to un-block all threads.
        self._clear_queues()

        # P-review 修复：补齐关停语义 —— join 全部工作线程（后处理/向量化/AI 分析）
        # 并关闭调度器。ident 为 None 的线程尚未 start，不能 join。
        for t in [self.post_process_thread,
                  self.intelligence_vectorization_thread,
                  *self._analysis_threads]:
            if t.ident is not None:
                t.join(timeout=timeout)

        # M3 接线新增：停止闲时 Drainer（先设停止事件再 join，释放租约交由持久化后端回收）
        if self.off_peak_drainer is not None:
            try:
                self.off_peak_drainer.stop()
            except Exception as e:
                logger.warning(f"OffPeakQueueDrainer stop fail: {e}")

        # M3 接线新增：关闭相似召回线程池
        try:
            self._similar_recall_executor.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"Similar recall executor shutdown fail: {e}")

        try:
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"Scheduler shutdown fail: {e}")

        # 清理资源
        self._cleanup_resources()
        logger.info("Intelligence hub has stopped.")

    def start_analysis_threads(self, thread_count):
        for i in range(thread_count):
            t = threading.Thread(target=self._ai_analysis_worker, name=f"AI-Worker-{i}", daemon=True, args=(i,))
            # 保存句柄供 shutdown() join，保证关停语义完整
            self._analysis_threads.append(t)
            t.start()
        logger.info(f"Started {thread_count} AI analysis threads.")

    # --------------------------------------- Shutdowns ---------------------------------------

    def _clear_queues(self):
        unprocessed = []
        with self.lock:
            while not self.original_queue.empty():
                item = self.original_queue.get()
                unprocessed.append(item)
                self.original_queue.task_done()
        # 保存到文件或数据库
        # self._save_to_file(unprocessed, 'pending_tasks.json')

    def _cleanup_resources(self):
        if self.mongo_db_cache:
            self.mongo_db_cache.close()

        if self.mongo_db_archive:
            self.mongo_db_archive.close()

    # ---------------------------------------------- Statistics and Debug ----------------------------------------------

    @property
    def statistics(self):
        return {
            'waiting_process': self.original_queue.qsize(),
            'unarchived_queue': self.unarchived_queue.qsize(),
            'post_process': self.processed_queue.qsize(),
            'vectorize_queue': self.vectorize_queue.qsize(),
            'archived': self.archived_counter,
            'dropped': self.drop_counter,
            'error': self.error_counter,
            'conversation_warning': self.conversation_warning,
            'conversation_error': self.conversation_error,
            'conversation_total': self.conversation_total,
            'vectorize_dropped': self.vectorize_drop_count,
            # 向量检索降级状态：非 None 表示最近一次引擎查询发生服务故障（区别于"无结果"）
            'vector_search_degraded': self.last_vector_error is not None,
            'last_vector_error': self.last_vector_error,
            # P2-9：向量功能四态（disabled/initializing/degraded/ok），
            # 与 /maintenance/ping?deep=1 暴露的一致
            'vector_status': self.vector_status,
            # M3 接线新增：预筛三分类计数与 AI 延期队列积压
            'prefilter_rejected': self.prefilter_rejected_count,
            'prefilter_metadata_only': self.prefilter_metadata_only_count,
            'prefilter_needs_ai': self.prefilter_needs_ai_count,
            'delay_queue_pending': (self.off_peak_drainer.queue_size()
                                    if self.off_peak_drainer is not None else 0),
            # P1-8：磁盘三级降级状态
            'disk_guard': self.disk_guard.stats() if self.disk_guard is not None else None,
            # P1-7：推送任务计数（含禁用状态下的跳过）
            'push_task_submitted': self.push_task_count,
            'push_stats': self.push_notifier.stats() if self.push_notifier is not None else None,
        }

    # ----------------------- P0-1 运行模式与闸门状态（供健康检查） -----------------------

    def consumer_mode(self) -> str:
        """当前生效的 AI 消费者模式（只读，不触发任何调用）。

        - legacy_ai_worker：旧 AI Worker 线程消费 original_queue；
        - off_peak_drainer：OffPeakQueueDrainer 消费 AI 延期队列；
        - dry_run / collect_only：无 AI 消费者是预期状态（非故障）。

        批次一修复：与 startup() 分派一致，legacy 判定必须检查
        use_legacy_ai_worker 显式开关（原先漏检，双关时误报 legacy 模式）。
        """
        if self.api_enabled and self.use_off_peak_drainer and self.off_peak_drainer is not None:
            return 'off_peak_drainer'
        if self.use_legacy_ai_worker and self.api_enabled \
                and not self.api_dry_run and not self.use_off_peak_drainer:
            return 'legacy_ai_worker'
        if self.api_enabled and self.api_dry_run:
            return 'dry_run'
        return 'collect_only'

    def ai_call_gate_status(self) -> dict:
        """AI 调用闸门状态汇总（信息性，不作为健康判定依据）。

        等待非高峰时段、预算耗尽、DRY_RUN、API 禁用均为预期状态，
        不得被健康检查误判为需要重启的故障（P0-1）。
        """
        info = {
            'mode': self.consumer_mode(),
            'api_enabled': self.api_enabled,
            'api_dry_run': self.api_dry_run,
        }
        router = self.model_router
        if router is None or not getattr(router, 'api_enabled', False):
            info['reason'] = 'api_disabled'
        elif getattr(router, 'api_dry_run', False):
            info['reason'] = 'dry_run'
        elif getattr(router, 'time_policy', None) is not None \
                and not router.time_policy.allowed_to_start_request():
            info['reason'] = 'off_peak_window'
        elif getattr(router, 'budget_guard', None) is not None \
                and not router.budget_guard.can_call():
            info['reason'] = 'budget_exhausted'
        else:
            info['reason'] = 'allowed' if router is not None else 'no_router'
        return info

    @property
    def vector_status(self) -> str:
        """P2-9：向量功能四态。

        - disabled：VectorDB 关闭（vector_db_client 为 None）；
        - initializing：已启用但引擎尚未就绪（集合未建/服务连接中）；
        - degraded：最近一次查询发生服务故障（last_vector_error 非 None）；
        - ok：正常（查询无结果是正常业务状态，不在此区分）。
        """
        if getattr(self, 'vector_db_client', None) is None:
            return 'disabled'
        engines = [getattr(self, 'vector_db_engine_summary', None),
                   getattr(self, 'vector_db_engine_full_text', None)]
        ready = [bool(e and getattr(e, 'is_ready', lambda: True)()) for e in engines]
        if getattr(self, 'last_vector_error', None) is not None:
            return 'degraded'
        if engines and not any(ready):
            return 'initializing'
        return 'ok'

    # ------------------------------------------------ Public Functions ------------------------------------------------

    # --------------------------------------- Data Submission ---------------------------------------

    def submit_collected_data(self, data: dict) -> True or Error:
        try:
            # P1-8：磁盘 >=95% 紧急降级——拒绝新采集（已有数据继续处理），
            # 核心查询与告警不受影响
            disk_guard = getattr(self, 'disk_guard', None)
            if disk_guard is not None and disk_guard.should_stop_collect:
                logger.critical("[DiskGuard] 磁盘 >=95%%，拒绝新采集提交（紧急降级）。")
                return IntelligenceHub.Error(
                    error_list=['Disk usage critical (>=95%), collection rejected.'])

            if self._check_duplication_in_unprocess_data(data):
                # 批次三修复：cache 查重命中不代表"已在处理"——
                # 缓存写入成功、分析入队前崩溃的数据会被永久拒绝（无法补投）。
                # Drainer 模式下检查 cache 文档状态：终态（A/M/D/S/U）才拒绝；
                # E（错误待重试）或无标记（崩溃断点）时允许补投，
                # 重复入队由队列幂等兜底（ENQUEUE_DUPLICATE 不视为错误）。
                if not self._allow_requeue_duplicated_cache_data(data):
                    return IntelligenceHub.Error(error_list=[f"Collected message duplicated {data.get('UUID', '')}."])

            validated_data, error_text = check_sanitize_dict(dict(data), CollectedData)

            if not isinstance(validated_data.get('collect_time', None), datetime.datetime):
                validated_data['collect_time'] = get_aware_time()
            validated_data[APPENDIX_TIME_POST] = get_aware_time()

            if error_text:
                return IntelligenceHub.Error(error_list=[error_text])

            # P-review 修复：token 仅用于提交校验，校验通过后立即剥离，
            # 确保不进 cache 文档、不进归档 RAW_DATA、不进导出文件。
            validated_data.pop('token', None)

            # P-review 修复：informant(URL) 规范化结果写回数据本身，
            # 使 cache 库存储值与查重查询值一致，同源同文的 URL 变体无法绕过去重。
            if validated_data.get('informant'):
                validated_data['informant'] = self._normalize_url(validated_data['informant'])

            # ---------------------- M3 PreFilter: Hub 侧唯一权威预筛 ----------------------
            # 三分类：RULE_REJECTED（拒绝审计）/ METADATA_ONLY（仅元数据归档，不进 AI 队列）/
            # NEEDS_AI（按消费者分派：Drainer 延期队列或旧 original_queue）。
            # prefilter 为 None 时（配置关闭）保持旧行为，直接进入原入队路径。
            if self.prefilter is not None:
                try:
                    pf = self.prefilter.screen(validated_data)
                except Exception as e:
                    # 预筛自身故障按 fail-closed 拒绝，不得流入 AI
                    logger.error(f"PreFilter screen exception: {e}")
                    with self.lock:
                        self.prefilter_rejected_count += 1
                    return IntelligenceHub.Error(error_list=[f"PreFilter internal error: {e}"])

                if pf.decision == 'RULE_REJECTED':
                    with self.lock:
                        self.prefilter_rejected_count += 1
                    logger.warning(f"[PreFilter] REJECT uuid={pf.original_uuid} "
                                   f"source={pf.source_id} rule={pf.matched_rule} reason={pf.reason}")
                    return IntelligenceHub.Error(
                        error_list=[f"PreFilter rejected: {pf.matched_rule}"])

                if pf.decision == 'METADATA_ONLY':
                    with self.lock:
                        self.prefilter_metadata_only_count += 1
                    logger.info(f"[PreFilter] METADATA_ONLY uuid={pf.original_uuid} "
                                f"rule={pf.matched_rule}")
                    # 仅元数据：入 cache 归档并打 M 终态标记，不进任何 AI 消费队列
                    self._cache_original_data(validated_data)
                    self._mark_cache_data_archived_flag(
                        validated_data.get('UUID', ''), ARCHIVED_FLAG_METADATA)
                    return True

                # NEEDS_AI：需 AI 分析
                with self.lock:
                    self.prefilter_needs_ai_count += 1
                if self.use_off_peak_drainer and self.off_peak_drainer is not None:
                    # 入 AI 延期队列（幂等：重复入队不产生重复 AI 调用）
                    self._cache_original_data(validated_data)
                    is_priority = False
                    try:
                        entry = self.prefilter.registry.get(pf.source_id)
                        is_priority = bool(entry and entry.is_priority)
                    except Exception as e:
                        # 优先级标记查询失败仅降级为"非重点"，不阻断入队主流程（有意忽略）
                        logger.debug(f"PreFilter registry priority lookup failed "
                                     f"for source={pf.source_id}: {e}")
                    # 批次三：入队结果区分新增/确定重复/存储故障。
                    try:
                        enqueue_result = self.off_peak_drainer.enqueue({
                            'content_hash': pf.content_hash,
                            'original_uuid': validated_data.get('UUID', ''),
                            'original_data': validated_data,
                            'source_id': pf.source_id,
                            'is_priority': is_priority,
                        })
                    except Exception as enqueue_err:
                        # 数据库连接错误等：fail-closed 返回错误（不报"重复"也不报"成功"），
                        # 爬虫可稍后重试提交；cache 已写入，补投路径不受阻。
                        logger.error(f"[PreFilter] delay-queue enqueue backend error "
                                     f"for {pf.original_uuid}: {enqueue_err}")
                        return IntelligenceHub.Error(
                            error_list=[f'Delay queue backend error: {enqueue_err}'])
                    if enqueue_result == 'duplicate':
                        # 幂等命中（content_hash 已存在），不视为错误
                        logger.info(f"[PreFilter] delay-queue idempotent hit for {pf.original_uuid}")
                    return True

            return self._enqueue_collected_data(validated_data)

        except Exception as e:
            logger.error(f"Submit collected data API exception: {str(e)}")
            return IntelligenceHub.Error(e, [str(e)])

    def submit_archived_data(self, data: dict) -> True or Error:
        try:
            if self._check_duplication_in_processed_data(data):
                return IntelligenceHub.Error(error_list=[f"Archived message duplicated {data.get('UUID', '')}."])

            validated_data, error_text = check_sanitize_dict(dict(data), ArchivedData)

            return IntelligenceHub.Error(error_list=[error_text]) \
                if error_text else self._enqueue_processed_data(validated_data)

        except Exception as e:
            logger.error(f"Submit archived data API exception: {str(e)}")
            return IntelligenceHub.Error(e, [str(e)])

    # -------------------------------------- Gets and Queries --------------------------------------

    def get_intelligence(self,
                         _uuid: Union[str, List[str]],
                         db: str = 'archive',
                         light_weight: bool = False
                         ) -> Union[dict, List[dict]]:
        if db == 'cache':
            query_engine = self.cache_db_query_engine
        else:
            query_engine = self.archive_db_query_engine
        return query_engine.get_intelligence(_uuid, light_weight=light_weight)

    def query_intelligence(self,
                           *,
                           db: str = 'archive',
                           period:      Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                           archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                           locations:   Optional[List[str]] = None,
                           peoples:     Optional[List[str]] = None,
                           organizations: Optional[List[str]] = None,
                           keywords: Optional[str] = None,
                           # P-review 修复：默认阈值从 4 改为 None（不过滤），
                           # 是否过滤及阈值由 Web/RPC 调用层自行决定，
                           # 避免 Hub 层隐式吞掉低分但合法的数据。
                           threshold: Optional[int] = None,
                           threshold_max: Optional[int] = None,
                           informant_domains: Optional[List[str]] = None,
                           geography: Optional[Union[str, List[str]]] = None,
                           skip: Optional[int] = 0,
                           limit: int = 100,
                           ) -> Tuple[List[dict], int]:
        if db == 'cache':
            query_engine = self.cache_db_query_engine
        else:
            query_engine = self.archive_db_query_engine
        result, total = query_engine.query_intelligence(
            period = period, archive_period=archive_period, locations = locations, peoples = peoples,
            organizations = organizations, keywords = keywords,
            threshold=threshold, threshold_max=threshold_max, informant_domains=informant_domains,
            geography=geography, skip=skip, limit=limit)
        return result, total

    def vector_search_intelligence(self,
                                   text: str,
                                   in_summary: bool = True,
                                   in_fulltext: bool = False,
                                   top_n: int = 10,
                                   score_threshold: float = 0.5,
                                   score_threshold_max: float = 1.0,
                                   event_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                                   archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                                   rate_threshold: Optional[float] = None,
                                   ) -> List[Tuple[str, float, dict]]:
        """
        Perform semantic search across both summary and full-text vector databases,
        returning deduplicated results with the best matches per document.

        This function enables intelligent hybrid search by querying either or both
        summary and full-text vector stores, then consolidates the results to ensure
        each document appears only once with its highest-scoring text chunk.

        Args:
            text: The query text for semantic search.
            in_summary: If True, search in the summary vector database. Defaults to True.
            in_fulltext: If True, search in the full-text vector database. Defaults to False.
            top_n: Maximum number of results to retrieve from each database. Defaults to 10.
            score_threshold: Minimum similarity score (0.0 to 1.0) for filtering results.
                             Defaults to 0.5.

        Returns:
            List[Tuple[str, float, str]]: A list of tuples, where each tuple contains:
                - str: The document identifier (doc_id)
                - float: The highest similarity score for that document (range: 0.0-1.0)
                - dict: Vector DB search result

            The list is not explicitly sorted, as the function preserves the document
            deduplication order. Each document appears only once, represented by its
            best-matching text chunk (highest similarity score).

        Notes:
            - If a document appears in both summary and full-text results, only the
              instance with the highest similarity score is retained.
            - Results below the score_threshold are filtered out at the database query level.
            - The function expects the underlying vector database query methods to return
              dictionaries containing at least "doc_id", "score", and "content" keys.
        """
        perf_ctx = {
            'text_len': len(text),
            'top_n': top_n,
            'score_threshold': score_threshold,
            'in_summary': in_summary,
            'in_fulltext': in_fulltext,
        }

        with self.perf_logger.timed('hub_vector_search', **perf_ctx):
            # 1. 线程安全快照 (Thread-Safe Snapshot)
            # 在锁内获取引用，后续只用这两个局部变量，不用担心 self.xxx 突然变 None
            with self.lock:
                engine_summary = self.vector_db_engine_summary
                engine_full = self.vector_db_engine_full_text

            summary_result = []
            fulltext_result = []

            # 2. 独立查询 (Best Effort Strategy)
            # 两个库各自 try/except:一个库抛异常不能中断另一个库的查询
            # P-review 修复：区分"无结果"与"服务故障"。引擎 query 抛异常（批次 D 后
            # IntelligenceVectorDBEngine.query 不再静默吞错）时记 last_vector_error
            # 并返回空，statistics 暴露 degraded 状态；查询成功则清除故障标记。
            if in_summary:
                if engine_summary:
                    try:
                        summary_result = engine_summary.query(
                            text, top_n, score_threshold,
                            event_period=event_period,
                            archive_period=archive_period,
                            rate_threshold=rate_threshold)
                        self.last_vector_error = None
                    except Exception as e:
                        logger.error(f"Summary vector search failed: {e}", exc_info=True)
                        summary_result = []
                        self.last_vector_error = f"summary: {e}"
                else:
                    logger.warning("Summary search requested but engine is not ready yet.")

            if in_fulltext:
                if engine_full:
                    try:
                        fulltext_result = engine_full.query(
                            text, top_n, score_threshold,
                            event_period=event_period,
                            archive_period=archive_period,
                            rate_threshold=rate_threshold)
                        self.last_vector_error = None
                    except Exception as e:
                        logger.error(f"Fulltext vector search failed: {e}", exc_info=True)
                        fulltext_result = []
                        self.last_vector_error = f"fulltext: {e}"
                else:
                    logger.warning("Fulltext search requested but engine is not ready yet.")

            # 如果两个都没查（或者都不可用），直接返回
            if not summary_result and not fulltext_result:
                return []

            # 3. 结果合并与去重 (Merge & Deduplicate)
            # 策略：同一文档 ID，保留分数最高的那个
            combined_results = summary_result + fulltext_result
            best_records = {}  # Format: {doc_id: (score, raw_result_dict)}

            for result in combined_results:
                doc_id = result.get("doc_id")  # 使用 .get 防止 key 不存在报错
                score = result.get("score", 0.0)

                if doc_id is None: continue
                if score > score_threshold_max: continue

                if doc_id not in best_records or score > best_records[doc_id][0]:
                    best_records[doc_id] = (score, result)

            # 4. 转换格式 -> [(doc_id, score, result_dict)]
            result_list = [
                (doc_id, val[0], val[1])
                for doc_id, val in best_records.items()
            ]

            # 5. 排序与截断 (Sort & Slice)
            # 合并后的结果必须重新按分数降序排列，并只取前 N 个
            result_list.sort(key=lambda x: x[1], reverse=True)

            return result_list[:top_n]

    def get_intelligence_summary(self) -> Tuple[int, str]:
        query_engine = self.archive_db_query_engine
        summary = query_engine.get_intelligence_summary()
        return summary["total_count"], summary["base_uuid"]

    def aggregate(self, pipeline: list) -> list:
        query_engine = self.archive_db_query_engine
        result = query_engine.aggregate(pipeline)
        return result

    def count_documents(self, _filter) -> int:
        query_engine = self.archive_db_query_engine
        result = query_engine.count_documents(_filter)
        return result

    # ------------------------------------------------ Directly Access ------------------------------------------------

    def get_query_engine(self) -> IntelligenceQueryEngine:
        return self.archive_db_query_engine

    def get_statistics_engine(self) -> IntelligenceStatisticsEngine:
        return IntelligenceStatisticsEngine(self.mongo_db_archive)

    # ---------------------------------------------------- Updates -----------------------------------------------------

    def submit_intelligence_manual_rating(self, _uuid: str, rating: dict):
        if not isinstance(rating, dict):
            return IntelligenceHub.Error(error_list=['Invalid rating'])

        self.mongo_db_archive.update(
            { 'UUID': _uuid },
            {f"APPENDIX.{APPENDIX_MANUAL_RATING}": rating})

        return True

    # ---------------------------------------------------- Workers -----------------------------------------------------

    @staticmethod
    def __is_retryable_error(result):
        """Only retry if it's NOT a permanent client-side / parse error."""
        if not isinstance(result, dict) or 'error' not in result:
            return False  # Not an error, or not a dict result we handle

        # Stop retrying if the error is a Client-Side Input Error (HTTP 400)
        if result.get('api_error_code') == 'HTTP_400':
            logger.error("Non-retryable input error (HTTP_400) detected. Stopping tenacity loop.")
            return False

        # 上下文超长错误：重试同样的请求必然再次失败，
        # 交由 _ai_analysis_worker 截断正文后单独重试一次。
        if result.get('api_error_code') == 'CONTEXT_LENGTH_EXCEEDED':
            logger.error("Context length exceeded. Stopping tenacity loop (will retry truncated once).")
            return False

        # P4 修复:JSON 解析类错误不可重试 —— 重试只会让 LLM 用同样的格式
        # 再次输出不可解析内容,白白烧 3 倍 token。
        if str(result.get('api_error_code', '')).startswith('PARSE_'):
            logger.error(f"Non-retryable parse error ({result.get('api_error_code')}) detected. "
                         f"Stopping tenacity loop to save tokens.")
            return False

        # Otherwise, continue retrying
        return True  # Retry on other errors (network, server)

    @retry(
        # The wait strategy: start at 1s, multiply by 2 each time, max out at 30s
        wait=wait_exponential(multiplier=1, min=1, max=30),
        # The stop condition: stop after max_retry attempts
        stop=stop_after_attempt(3),
        # The retry condition: retry if an exception occurs OR the result is an error
        retry = (retry_if_exception_type(Exception) | retry_if_result(__is_retryable_error))
    )
    def __robust_analyze_with_ai(self, original_data: dict, worker_index: int,
                                 similar_messages: Optional[List[dict]] = None):
        """
        A robust wrapper for the AI analysis function that will be automatically retried.
        similar_messages: 向量召回的相似历史消息，注入 prompt 并供评分引擎做相似度惩罚。
        """
        if self.shutdown_flag.is_set():
            return None
        prefix = f'AI Worker [{worker_index}]'
        client_user = f'IntelligenceHub-{worker_index}'

        # --------------------------- Wait until one AI client available ---------------------------

        # P-review 修复：ANALYSIS_PROMPT_TABLE 目前只有 V25 一个版本，直接取值；
        # 保留表结构，将来多版本时再恢复随机选择。
        selected_prompt_index = next(iter(ANALYSIS_PROMPT_TABLE))
        selected_prompt = ANALYSIS_PROMPT_TABLE[selected_prompt_index]

        retries = 0
        # P2 修复：增加 shutdown 检查。原 while True 在客户端全失效时
        # 全员死循环且无法优雅退出（关闭进程也要等 put None）。
        while not self.shutdown_flag.is_set():
            if ai_client := self.ai_client_manager.get_available_client(client_user):
                # 批次一修复：旧分析路径不能只检查预算而不记录消费。
                # 用捕获包装器缓存最近一次原始响应（含 usage），
                # 调用完成后进入同一费用台账（_record_legacy_analysis_usage）。
                capturer = _UsageCapturingClient(ai_client)
                try:
                    result = analyze_with_ai(capturer, selected_prompt, original_data,
                                             similar_messages=similar_messages)
                finally:
                    self.ai_client_manager.release_client(ai_client)
                self._record_legacy_analysis_usage(capturer.last_response,
                                                  str(original_data.get('UUID', '') or ''),
                                                  selected_prompt_index)

                # P-review 修复：AI 输出告警（截断/JSON修复）从结果中剥离出来并入 APPENDIX，
                # 随归档保留，便于后续审计与训练数据清洗。
                analysis_warnings = result.pop(APPENDIX_ANALYSIS_WARNING, None)

                result['APPENDIX'] = {
                    APPENDIX_PROMPT_VERSION: selected_prompt_index,
                    APPENDIX_AI_SERVICE: ai_client.get_api_base_url(),
                    APPENDIX_AI_MODEL: ai_client.get_current_model(),
                }
                if analysis_warnings:
                    result['APPENDIX'][APPENDIX_ANALYSIS_WARNING] = analysis_warnings
                    logger.warning(f"{prefix} analysis warnings for {original_data.get('UUID', '?')}: "
                                   f"{analysis_warnings}")

                self._process_appendix_time(original_data, result)
                break
            retries += 1
            if retries % 10 == 0:
                logger.warning(f"{prefix} Thread {threading.current_thread().name} waiting for AI client for {retries}s...")
            time.sleep(1 + random.random() * 0.5)
        else:
            # shutdown 触发时退出等待循环，直接放弃本次分析（不标记 ERROR，等待重启恢复）
            logger.warning(f"{prefix} Shutdown signaled while waiting for AI client. Abort analysis.")
            return None
        if retries:
            logger.info(f"{prefix} Analysis tries to get AI client for {retries} times.")

        # ------------------------------------------------------------------------------------------

        # Check warning and error for statistics
        # P-review 修复：多 worker 并发自增，计数器加锁保证统计准确。
        with self.lock:
            if 'error' in result:
                self.conversation_error += 1
            elif 'warning' in result:
                self.conversation_warning += 1
            self.conversation_total += 1

        time.sleep(1.5 + random.random() * 0.5)

        return result

    def _ai_call_allowed(self, prefix: str) -> bool:
        """旧 AI Worker 调用前的护栏汇总检查（M3 接线新增）。

        依次校验 DRY_RUN、峰谷时间窗口（含 Guard）与预算守卫；
        任一不满足返回 False 并记日志，调用方应把数据放回队列等待。
        """
        # 兜底短路：正常情况下 DRY_RUN 时 startup() 不会启动本线程，此处防御性拦截
        if self.api_dry_run:
            logger.info(f"{prefix} api_dry_run=true, AI call skipped (fail-closed).")
            return False
        if self.time_policy is not None and not self.time_policy.allowed_to_start_request():
            logger.info(f"{prefix} 当前处于高峰/Guard 窗口，暂停 AI 调用等待非高峰。")
            return False
        if self.budget_guard is not None and not self.budget_guard.can_call():
            logger.warning(f"{prefix} 预算已耗尽或达到调用次数上限，暂停 AI 调用。")
            return False
        return True

    def _ai_analysis_worker(self, worker_index: int = 0):
        prefix = f'AI Worker [{worker_index}]'

        if not self.ai_client_manager:
            logger.info(f'{prefix} **** NO AI API client - Thread QUIT ****')
            return

        # ------------------------------------ Analysis Main Loop ------------------------------------

        while not self.shutdown_flag.is_set():
            original_uuid = None
            original_data = None
            current_queue = None  # 用于记录当前数据来自哪个队列，以便正确 task_done
            is_sensitive_or_bad_request = False
            context_length_retried = False  # 上下文超长错误只截断重试一次

            try:
                try:
                    # 阻塞等待 1 秒，优先处理新数据
                    original_data = self.original_queue.get(block=True, timeout=1)
                    current_queue = self.original_queue
                except queue.Empty:
                    # ------------------- 2. 高优先级为空，尝试低优先级 -------------------
                    # 如果 original_queue 是空的，尝试从 unarchived_queue 拿
                    # 使用 block=False，因为刚才已经等了1秒了，这里快速检查即可
                    try:
                        original_data = self.unarchived_queue.get(block=False)
                        current_queue = self.unarchived_queue
                        logger.debug('Idle, process unarchived queue.')
                    except queue.Empty:
                        # 两个队列都空，进入下一次循环
                        continue

                # If there's no UUID...
                if not (original_uuid := str(original_data.get('UUID', '')).strip()):
                    original_data['UUID'] = original_uuid = str(uuid.uuid4())

                # ---------------------- M3 Guardrails: DRY_RUN / Time Window / Budget ----------------------
                # 数据取到后、调 AI 前依次校验；不满足时把数据放回原队列（保持优先级
                # 语义，put 的 unfinished 计数由本轮回合的 finally task_done 平衡），
                # 等待下一窗口，绝不产生高峰/超预算调用。
                if not self._ai_call_allowed(prefix):
                    try:
                        current_queue.put(original_data, block=True, timeout=5)
                    except queue.Full:
                        # 队列满：数据已在 cache 持久化，打 E 标记走重启恢复，不丢数据
                        self._mark_cache_data_archived_flag(original_uuid, ARCHIVED_FLAG_ERROR)
                        logger.warning(f"{prefix} Queue full while deferring for guardrail, "
                                       f"marked E for recovery: {original_uuid}")
                    self.shutdown_flag.wait(60)
                    continue

                # ---------------------- Check Duplication First Avoiding Wasting Token ----------------------

                if self._check_duplication_in_processed_data(original_data):
                    raise IntelligenceHub.Exception('drop', 'Article duplicated')

                # ---------------------- Similar Recall (best-effort, never blocks) ----------------------
                # P-review 修复：AI 分析前用向量引擎召回相似历史消息，
                # 注入 prompt 并供评分引擎做 0.86/0.92 相似度惩罚。
                # 任何异常/超时/未就绪都返回空列表，绝不阻断分析流程。
                similar_messages = self._recall_similar_messages(original_data)

                # ---------------------------------- AI Analysis with Retry ----------------------------------

                result = self.__robust_analyze_with_ai(original_data, worker_index,
                                                       similar_messages=similar_messages)

                is_error = not result or 'error' in result

                # P-review 修复：上下文超长不是敏感词。截断正文到 20000 字符后重试一次，
                # 仍失败才按普通错误处理（E 标记，走重启恢复重试）。
                if (is_error and not context_length_retried
                        and isinstance(result, dict)
                        and result.get('api_error_code') == 'CONTEXT_LENGTH_EXCEEDED'):
                    context_length_retried = True
                    logger.warning(
                        f"{prefix} Context length exceeded for {original_uuid}, "
                        f"truncate content to 20000 chars and retry once.")
                    original_data = dict(original_data)
                    original_data['content'] = str(original_data.get('content', ''))[:20000]
                    result = self.__robust_analyze_with_ai(original_data, worker_index,
                                                           similar_messages=similar_messages)
                    is_error = not result or 'error' in result

                if is_error:
                    # 检查是否是 HTTP_400 敏感词/请求参数错误
                    # result 应该包含 BaseAIClient 返回的 api_error_code
                    if isinstance(result, dict) and result.get('api_error_code') == 'HTTP_400':
                        is_sensitive_or_bad_request = True
                        error_msg = f"AI process failed: Permanent Bad Request (HTTP 400)."
                    else:
                        # 其他错误 (网络/服务器/JSON解析错/截断重试仍失败，tenacity已重试 3 次)
                        error_msg = f"AI process error after all retries."

                    # 将错误提升为 Exception，以便进入 finally 块并进行标记
                    raise ValueError(error_msg)

                # ----------------------- Check Analysis Result and Fill Other Fields ------------------------

                # ------------------- Check low value data -------------------

                # 20260111: In v2x prompt, AI will not extract UUID and informant from original message.
                # 20260324: Always set correct UUID and INFORMANT. Because the low value also need to be persisted.
                result['UUID'] = original_uuid
                result['INFORMANT'] = str(original_data.get('informant', '')).strip()

                if self._is_low_value_data(result):
                    # 20260112: Put it in processed queue, and save into intelligence_low_value
                    #           For model training: negative sample.
                    self.processed_queue.put(result)
                    raise IntelligenceHub.Exception('drop', 'Low value data.')

                # P-review 修复：解析异常与"无价值"分流。AI 返回非 dict 被包成
                # {'items': ...}，或缺 EVENT_TEXT 但又非"无情报价值"判定 →
                # PARSE_ERROR 类错误，抛异常走 E 标记重试，不得混进 low_value 负样本库。
                if 'items' in result or 'EVENT_TEXT' not in result:
                    raise ValueError(
                        'AI output parse error: missing EVENT_TEXT or wrapped in items. '
                        f'UUID={original_uuid}')

                # P2 修复：先经 Pydantic 归一化（RATE 中文等级→数值、时间补 UTC 等），
                # 再计算总分。原实现先对原始输出评分后归一化，导致页面 RATE 与
                # TOTAL_SCORE 两套口径，且中文等级一律记 0 分。
                validated_data, error_text = check_sanitize_dict(dict(result), ArchivedData)
                if error_text:
                    raise ValueError(error_text)

                scoring_engine = IntelligenceScoringEngine()
                total_score = scoring_engine.calculate_single(validated_data,
                                                              similar_messages=similar_messages)
                validated_data['APPENDIX'][APPENDIX_TOTAL_SCORE] = total_score
                result = validated_data

                # -------------------------------- Fill Extra Data and Enqueue --------------------------------

                validated_data['RAW_DATA'] = original_data
                validated_data['SUBMITTER'] = 'Analysis Thread'

                if not self._enqueue_processed_data(validated_data):
                    with self.lock:
                        self.error_counter += 1

            except IntelligenceHub.Exception as e:
                if e.name == 'drop':
                    with self.lock:
                        self.drop_counter += 1
                    self._mark_cache_data_archived_flag(original_uuid, ARCHIVED_FLAG_DROP)
            except Exception as e:
                with self.lock:
                    self.error_counter += 1
                logger.error(f"{prefix} Analysis error: {str(e)}")
                traceback.print_exc()

                # P-review 修复：关停过程中不打错误标记（此时失败多半由关停引起），
                # 数据保留在 cache 无标记状态，待下次启动由恢复流程重新加载。
                if self.shutdown_flag.is_set():
                    logger.info(f"{prefix} Shutdown in progress, skip error marking for {original_uuid}.")
                elif is_sensitive_or_bad_request:
                    # 如果是敏感词或坏请求，使用特殊标记，避免丢弃但隔离
                    self._mark_cache_data_archived_flag(original_uuid, ARCHIVED_FLAG_SENSITIVE)
                    logger.warning(
                        f"{prefix} Permanently Blocked: {original_uuid} marked BLOCKED due to HTTP 400 error.")
                else:
                    # 其他错误（网络、系统等）使用 ARCHIVED_FLAG_ERROR 标记
                    self._mark_cache_data_archived_flag(original_uuid, ARCHIVED_FLAG_ERROR)
            finally:
                if current_queue:
                    current_queue.task_done()

    def _post_process_worker(self):

        # -------------------------------------- Post process loop --------------------------------------

        while not self.shutdown_flag.is_set():
            data = None
            got_task = False
            try:
                data = self.processed_queue.get(block=True, timeout=1.0)
                got_task = True
                if data is None: break

                if self._check_duplication_in_db(data, 'INFORMANT', self.archive_db_query_engine):
                    raise ProcessSkip('duplication', 'Found duplication. Not archive this data.')

                if self._is_low_value_data(data):
                    # Low value data has been marked as ARCHIVED_FLAG_DROP in _ai_analysis_worker
                    if self.mongo_db_low_value:
                        with positioning_exception_context('low_value', 'Low-value data persists fail.'):
                            # P-review 修复：低值库入库前也按 informant 查重，
                            # 避免同一来源反复入库污染训练负样本。
                            if not self._check_low_value_duplication(data):
                                self.mongo_db_low_value.insert(data)
                            else:
                                logger.info(f"Low-value data duplicated in low_value db. Skip insert {data['UUID']}.")
                    raise ProcessSkip('low_value', 'Low value data. Not archive this data.')

                data['APPENDIX'][APPENDIX_TIME_ARCHIVED] = get_aware_time()

                with positioning_exception_context('archive', 'Archive fail.'):
                    self._archive_processed_data(data)

                with self.lock:
                    self.archived_counter += 1
                self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_ARCHIVED)
                # 曾因归档失败落盘的数据，重试成功后清理落盘文件
                self._remove_failed_archive_dump(data['UUID'])

                logger.info(f"Message {data['UUID']} archived.")

                # P1-7：归档成功后可靠地产生推送任务（写持久化队列）。
                # 推送失败不影响归档主链路；>=85% 磁盘降级时跳过（低优先级）。
                self._enqueue_push_task(data)

                # TODO: Call post processor plugins

                try:
                    if self.async_translation_patch and needs_translation(data):
                        # 新数据：高优先级异步翻译；先不向量化（等待翻译线程完成后 on_patched 再向量化）
                        self.async_translation_patch.enqueue_new(data["UUID"], reason="new_archived")
                    else:
                        # 原生中文：直接向量化
                        self._index_archived_data(data)
                except Exception as e:
                    logger.warning("AsyncTranslationPatch enqueue fail, fallback vectorize: %s", e)
                    self._index_archived_data(data)

            except queue.Empty:
                # 消费循环 1s 超时唤醒属正常节奏（检查停机标志），非错误
                continue

            except ProcessSkip as e:
                logger.info(str(e))

                with self.lock:
                    self.drop_counter += 1

                if data is not None:
                    if e.reason == 'duplication':
                        self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_DUPLICATED)
                    elif e.reason == 'low_value':
                        self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_DROP)
                    else:
                        logger.error('************* Should not reach here [C7F6] *************')
                else:
                    logger.error('************* Should not reach here [0E57] *************\n'
                                 'Data is None. It\'s impossible.')

            except PositioningException as e:
                if e.position == 'low_value':
                    logger.error(f'Low-value data persists fail.')
                    self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_DROP)
                elif e.position == 'archive':
                    # P-review 修复：关停中不打错误标记（失败多半由关停引起），
                    # 数据保持无标记，待下次启动恢复。
                    if self.shutdown_flag.is_set():
                        logger.info(f"Shutdown in progress, skip archive-error marking for {data.get('UUID')}.")
                        continue
                    with self.lock:
                        self.error_counter += 1
                    logger.error(f"Archived fail with exception: {str(e)}")
                    # 先落盘后打标记：归档 insert 失败的数据 JSON 落盘到
                    # _data/failed_archive/<uuid>.json，重试成功后删除（见上方归档成功路径）。
                    self._dump_failed_archive(data)
                    self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_ERROR)
                else:
                    logger.error('************* Should not reach here [EBEC] *************')

            except Exception as e:
                logger.error(f"Post process got unknown issue: {str(e)}")

            finally:
                if got_task:
                    self.processed_queue.task_done()

    def _vectorization_thread(self):
        """
        Worker thread that consumes data from the queue and upserts it to VectorDB.
        It waits indefinitely for the DB to be ready and is resilient to runtime errors.
        """
        # 1. Infinite wait for initial connection (Blocking until success or shutdown)
        if not self._wait_for_vector_db_ready():
            return  # Shutdown signaled

        logger.info("Vectorization thread started processing.")

        while not self.shutdown_flag.is_set():
            try:
                # 2. Get data with a timeout to allow checking shutdown_flag periodically
                try:
                    data = self.vectorize_queue.get(block=True, timeout=1.0)
                except queue.Empty:
                    # 消费循环 1s 超时唤醒属正常节奏（检查停机标志），非错误
                    continue

                if not data:
                    self.vectorize_queue.task_done()
                    continue

                # 3. Process data safely
                MAX_VECTORIZE_RETRIES = 3
                try:
                    retry_count = int(data.get('_vectorize_retry_count', 0))
                    uuid = data.get('UUID', 'unknown')
                    queue_size = self.vectorize_queue.qsize()

                    with self.perf_logger.timed(
                        'hub_vectorize_upsert',
                        uuid=uuid,
                        queue_size=queue_size,
                    ):
                        clock = Clock()

                        # Validation
                        archived_data = ArchivedData(**data)

                        # Upsert to Summary Engine
                        if self.vector_db_engine_summary:
                            self.vector_db_engine_summary.upsert(archived_data, data_type='summary')

                        # Upsert to FullText Engine
                        if self.vector_db_engine_full_text:
                            self.vector_db_engine_full_text.upsert(archived_data, data_type='full')

                        logger.debug(f"Message {archived_data.UUID} vectorized, time-spending: {clock.elapsed_ms()} ms")

                except Exception as e:
                    # 向量化失败不能静默丢数据:有限重投(指数退避)。
                    # 重试计数存到 data['_vectorize_retry_count'] 随重投传递,
                    # 避免 while 循环体重置计数导致无限重投。
                    retry_count += 1
                    if retry_count < MAX_VECTORIZE_RETRIES:
                        backoff = min(2 ** retry_count, 10.0)
                        logger.warning(
                            f"Vectorize failed for {data.get('UUID', 'unknown')} "
                            f"(attempt {retry_count}/{MAX_VECTORIZE_RETRIES}, backoff {backoff:.1f}s): {e}"
                        )
                        data['_vectorize_retry_count'] = retry_count
                        self.vectorize_queue.put(data)  # 重投队列末尾,等待下一轮
                        time.sleep(backoff)
                    else:
                        logger.error(
                            f"Vectorize failed permanently for {data.get('UUID', 'unknown')} "
                            f"after {MAX_VECTORIZE_RETRIES} attempts: {e}"
                        )
                        # 不再重投;数据仍在 Mongo archive,可人工重索引。

                finally:
                    self.vectorize_queue.task_done()

            except Exception as outer_e:
                # Catch-all for unexpected queue errors to prevent thread death
                logger.error(f"Critical error in vectorization loop: {outer_e}")
                time.sleep(1)  # Brief pause to prevent CPU spinning if queue is broken

    def _wait_for_vector_db_ready(self) -> bool:
        """
        Blocks indefinitely until the VectorDB service is ready and collections are created.

        Features:
        - Retries forever on connection failure.
        - Suppresses repetitive logs (prints failure only once).
        - Responds to shutdown_flag immediately.
        """
        if self.vector_db_client is None:
            logger.warning("Vector DB client is not configured, skipping init.")
            return False

        logger.info('Initializing Vector DB connection...')

        # Flag to ensure we don't spam logs during long downtimes
        log_suppressed = False

        while not self.shutdown_flag.is_set():
            try:
                # 1. Wait for service readiness
                # Use a short timeout so we can cycle back and check shutdown_flag
                try:
                    # Note: This might raise TimeoutError if not ready in 2s
                    self.vector_db_client.wait_until_ready(timeout=2.0, poll_interval=1.0)
                except TimeoutError:
                    if not log_suppressed:
                        logger.info("Vector DB not ready yet, waiting in background...")
                        log_suppressed = True
                    continue  # Retry loop

                # 2. Create Collections
                # If we reach here, the service is responding (HTTP 200)
                vector_db_summary = self.vector_db_client.create_collection(
                    name='intelligence_summary',
                    chunk_size=256,
                    chunk_overlap=30
                )

                vector_db_full_text = self.vector_db_client.create_collection(
                    name='intelligence_full_text',
                    chunk_size=512,
                    chunk_overlap=50
                )

                plan_spec = generate_aggregation_plan(profile="agglomerative_strict")
                aggregation_engine_summary = IntelligenceAggregationEngine(self.vector_db_client, plan_spec)
                aggregation_engine_summary.ensure_plan(overwrite=True)

                with self.lock:
                    self.vector_db_engine_summary = IntelligenceVectorDBEngine(vector_db_summary)
                    self.vector_db_engine_full_text = IntelligenceVectorDBEngine(vector_db_full_text)
                    self.aggregation_engine_summary = aggregation_engine_summary
                    self._init_graph_engine()

                self._bootstrap_aggregation_once()

                # 3. Success
                logger.info(f'Vector DB initialized successfully.')
                return True

            except Exception as e:
                # 4. Handle ANY error (Network, 500, etc.) without exiting
                # P-review 修复：原 `except (ConnectionError, Exception)` 冗余，
                # Exception 已覆盖 ConnectionError，收敛为单 Exception。
                if not log_suppressed:
                    logger.warning(f"Vector DB init failed ({e}). Retrying silently...")
                    log_suppressed = True

                # Sleep in small chunks to remain responsive to shutdown
                for _ in range(5):
                    if self.shutdown_flag.is_set():
                        return False
                    time.sleep(1)

                # Loop continues...

        logger.info("Vector DB init worker stopped due to shutdown signal.")
        return False

    def _init_graph_engine(self):
        self.dynamic_graph_engine = DynamicGraphEngine(
            mongo_db = self.mongo_db_archive,
            query_engine = self.archive_db_query_engine,
            vector_engine = self.vector_db_engine_summary,
            # ai_client = self.ai_client_manager,
            ai_client = None,
        )

    # ------------------------------------------------ Scheduled Tasks -------------------------------------------------

    def _do_export_mongodb_weekly(self):
        """
        Weekly export task.
        Triggered on Sunday. Exports the current ISO week's data.
        """
        try:
            now = datetime.datetime.now()
            logger.info(f'Export mongodb weekly start at: {now}')

            # 获取当前的 ISO 年份和周数
            # isocalendar() 返回 (year, week, weekday)
            iso_year, iso_week, _ = now.isocalendar()

            # 1. 导出 Archive 数据库 (按周)
            # 路径: {EXPORT_PATH}/mongo_db_archive/weekly_2023_W42_timestamp.json
            if self.mongo_db_archive:
                archive_dir = os.path.join(EXPORT_PATH, 'mongo_db_archive')
                self.mongo_db_archive.export_by_week(
                    year=iso_year,
                    week=iso_week,
                    directory=archive_dir,
                    time_field=f"APPENDIX.{APPENDIX_TIME_ARCHIVED}",
                    add_timestamp=True  # 定时任务建议加上时间戳，防止文件名冲突或覆盖
                )

            # 2. 导出 Cache 数据库 (按周)
            # 路径: {EXPORT_PATH}/mongo_db_cache/weekly_2023_W42_timestamp.json
            if self.mongo_db_cache:
                cache_dir = os.path.join(EXPORT_PATH, 'mongo_db_cache')
                # Cache 通常使用 created_at 或 timestamp
                self.mongo_db_cache.export_by_week(
                    year=iso_year,
                    week=iso_week,
                    directory=cache_dir,
                    time_field='created_at',
                    add_timestamp=True
                )

            logger.info(f'Export mongodb weekly finished at: {datetime.datetime.now()}')

        except Exception as e:
            logger.error(f"Weekly mongodb export failed: {e}", exc_info=True)

    def _do_export_mongodb_monthly(self):
        """
        Monthly export task.
        Triggered on the 1st day of the month. Exports the *PREVIOUS* month's data.
        """
        try:
            now = datetime.datetime.now()
            logger.info(f'Export mongodb monthly start at: {now}')

            # 计算上个月的年份和月份
            # 逻辑：当前日期(1号) 减去 1天 = 上个月最后一天
            last_day_prev_month = now.replace(day=1) - datetime.timedelta(days=1)
            target_year = last_day_prev_month.year
            target_month = last_day_prev_month.month

            logger.info(f"Targeting export for Year: {target_year}, Month: {target_month}")

            # 1. 导出 Archive 数据库 (按月)
            if self.mongo_db_archive:
                archive_dir = os.path.join(EXPORT_PATH, 'mongo_db_archive')
                self.mongo_db_archive.export_by_month(
                    year=target_year,
                    month=target_month,
                    directory=archive_dir,
                    time_field=f"APPENDIX.{APPENDIX_TIME_ARCHIVED}",
                    add_timestamp=True
                )

            # 2. 导出 Cache 数据库 (按月)
            if self.mongo_db_cache:
                cache_dir = os.path.join(EXPORT_PATH, 'mongo_db_cache')
                self.mongo_db_cache.export_by_month(
                    year=target_year,
                    month=target_month,
                    directory=cache_dir,
                    time_field='created_at',
                    add_timestamp=True
                )

            logger.info(f'Export mongodb monthly finished at: {datetime.datetime.now()}')

        except Exception as e:
            logger.error(f"Monthly mongodb export failed: {e}", exc_info=True)

    def _do_run_summary_aggregation(self):
        """
        Hourly task: trigger offline aggregation for summary plan.
        Persistence is handled by VectorDB service after offline completes.
        """
        try:
            if not self.aggregation_engine_summary:
                logger.warning("Aggregation engine not ready; skip hourly aggregation.")
                return

            time_range_to_use = None
            window_sec = 24 * 3600

            # 1. 委托 QueryEngine 发现最新时间
            latest_ts = self.archive_db_query_engine.get_latest_archive_timestamp()

            # 2. 判断是否需要回退时间窗口 (兼容老旧测试数据)
            if latest_ts:
                now_ts = datetime.datetime.now().timestamp()
                if (now_ts - latest_ts) >= window_sec:
                    logger.info(
                        f"No data in last 24h. Falling back to test data timeline based on timestamp: {latest_ts}")
                    time_range_to_use = (latest_ts - window_sec, latest_ts)

            # # ====== 测试专用代码段 ======
            # test_end_dt = datetime.datetime(2026, 2, 26)
            # test_start_dt = test_end_dt - datetime.timedelta(days=1)
            #
            # test_time_range = (test_start_dt.timestamp(), test_end_dt.timestamp())
            #
            # # 传入 time_range，VectorDB 就会放弃自动发现，严格执行这个区间
            # job_id = self.aggregation_engine_summary.trigger_offline(
            #     overrides=None,
            #     time_range=test_time_range
            # )
            # # =========================

            # 新增传入 fetcher 以便后台监控线程使用
            def doc_fetcher(uuids):
                return self.get_intelligence(uuids, light_weight=True)

            # 3. 触发 VectorDB 离线聚合
            # 如果近期有数据，time_range_to_use 仍为 None，底层自动取 [Now - 24h, Now]
            job_id = self.aggregation_engine_summary.trigger_offline(
                overrides=None,
                time_range=time_range_to_use,
                doc_fetcher=doc_fetcher
            )

            if job_id:
                logger.info(f"Triggered summary aggregation offline job: {job_id}")
            else:
                logger.warning("Triggered summary aggregation but got no job_id.")

        except Exception as e:
            logger.error(f"Hourly summary aggregation trigger failed: {e}", exc_info=True)

    def _bootstrap_aggregation_once(self):
        """
        Bootstrap aggregation after VectorDB is ready.
        It simply calls the smart hourly aggregation logic to perform the initial run.
        """
        logger.info("[AggregationBootstrap] Executing initial offline aggregation...")
        self._do_run_summary_aggregation()

    # ------------------------------------------------ Helpers ------------------------------------------------

    def _is_low_value_data(self, data: dict):
        # Simplify check low-value data
        # Strictly check:
        #  v1: Only has UUID field.
        #  v2: ｛　TAXONOMY: "无情报价值"　｝
        # P-review 修复：从"缺 EVENT_TEXT"改为按 TAXONOMY 判定。
        # 缺 EVENT_TEXT 但又非无价值判定属于解析异常，由调用方走 E 标记重试，
        # 不再混进 low_value 训练负样本库。
        return data.get('TAXONOMY') == '无情报价值'

    # ---------------------------- Duplication Check ----------------------------
    # TODO: Optimise informant check.

    @staticmethod
    def _normalize_url(url: str) -> str:
        """P2 修复：URL 规范化，消除同源同文的变体绕过去重。

        处理项：
        - scheme 与 host 小写（HTTP://Example.COM → http://example.com）
        - 去除跟踪参数（utm_*、fbclid、gclid、ref、spm 等）
        - 去除尾斜杠
        非 URL 字符串原样返回（informant 可能是站点名而非 URL）。
        """
        if not isinstance(url, str) or not url:
            return url or ''
        stripped = url.strip()
        if '://' not in stripped:
            return stripped.rstrip('/')

        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            parts = urlsplit(stripped)
            # 过滤跟踪参数
            tracking_prefixes = ('utm_', 'fbclid', 'gclid', 'spm', 'ref_', 'from_')
            kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if not any(k.lower().startswith(p) for p in tracking_prefixes)
                    and k.lower() not in ('ref', 'from', 'share', 'source')]
            normalized = urlunsplit((
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip('/') or '/',
                urlencode(kept),
                ''  # 丢弃 fragment
            ))
            return normalized
        except Exception:
            return stripped

    def _check_get_identifier(self, data: dict):
        target_uuid = str(data.get('UUID', '')).strip()
        target_informant = self._normalize_url(
            data.get('informant', '') or data.get('INFORMANT', ''))

        if not target_uuid:
            raise ValueError('No valid uuid.')
        if not target_informant:
            raise ValueError('No valid informant.')

        return target_uuid, target_informant

    def _check_duplication_in_queue(self, data: dict, informant_key: str, queue_to_check: queue.Queue):
        target_uuid, target_informant = self._check_get_identifier(data)
        # P 优化：注册的 IndexedQueue 走 O(1) 索引查询（索引在队列互斥锁内维护，
        # 不再逐项扫描阻塞入队/出队）；外部传入的裸 queue.Queue（如单元测试构造）
        # 保持旧的锁内扫描语义，兼容既有调用方。
        if isinstance(queue_to_check, IndexedQueue):
            return queue_to_check.contains_identifier(
                target_uuid, target_informant, informant_key)
        with queue_to_check.mutex:
            for item in queue_to_check.queue:
                if item.get('UUID') == target_uuid:
                    return True
                if target_informant and item.get(informant_key) == target_informant:
                    return True
        return False

    def _check_duplication_in_db(self, data: dict, informant_key: str, query_engine: IntelligenceQueryEngine):
        target_uuid, target_informant = self._check_get_identifier(data)
        conditions = {'UUID': target_uuid}
        if target_informant:
            conditions[informant_key] = target_informant
        # P-review 修复：删除 if/else 两分支相同的死代码；
        # 查重只需判定存在性，limit=1 避免无谓的数据传输。
        duplicated = bool(query_engine.common_query(conditions=conditions, operator="$or", limit=1))
        return duplicated

    def _check_duplication_in_unprocess_data(self, data: dict):
        return (self._check_duplication_in_queue(data, 'informant', self.original_queue) or
                self._check_duplication_in_queue(data, 'informant', self.unarchived_queue) or
                self._check_duplication_in_db(data, 'informant', self.cache_db_query_engine))

    def _allow_requeue_duplicated_cache_data(self, data: dict) -> bool:
        """批次三：cache 查重命中时判断是否允许补投（Drainer 模式）。

        背景：缓存写入成功、分析入队前崩溃的数据，重启后重新提交会因
        cache 查重被永久拒绝（无法补投）。本方法区分"历史终态数据"与
        "崩溃断点数据"：

        - 仅当 Drainer 模式启用时生效（旧 Worker 有自己的恢复路径）；
        - 数据仍在内存队列（original/unarchived）→ 拒绝（正在处理）；
        - cache 文档带终态标记 A/M/D/S/U → 拒绝（已处理完毕或隔离）；
        - cache 文档标记 E（错误待重试）或无标记（崩溃断点/处理中）→
          允许补投；重复入队由延期队列幂等兜底（ENQUEUE_DUPLICATE）。
        """
        if not (self.use_off_peak_drainer and self.off_peak_drainer is not None):
            return False
        # 内存队列中仍有同源数据 → 正在处理，拒绝
        if self._check_duplication_in_queue(data, 'informant', self.original_queue) or \
                self._check_duplication_in_queue(data, 'informant', self.unarchived_queue):
            return False
        # 查 cache 文档的归档标记（用与查重一致的查询通道）
        try:
            informant = self._normalize_url(str(data.get('informant', '') or ''))
            docs = self.cache_db_query_engine.common_query(
                conditions={'informant': informant}, limit=2) or []
            doc = docs[0] if docs else None
        except Exception as e:
            logger.warning(f"Requeue duplication check fail (deny): {e}")
            return False
        if not isinstance(doc, dict):
            return False  # 查不到不冒险放行
        appendix = doc.get('APPENDIX') or {}
        flag = doc.get(APPENDIX_ARCHIVED_FLAG, appendix.get(APPENDIX_ARCHIVED_FLAG))
        # 终态标记：A=已归档 M=仅元数据 D=低值丢弃 S=敏感隔离 U=重复——拒绝补投
        if flag in (ARCHIVED_FLAG_ARCHIVED, ARCHIVED_FLAG_METADATA, ARCHIVED_FLAG_DROP,
                    ARCHIVED_FLAG_SENSITIVE, ARCHIVED_FLAG_DUPLICATED):
            return False
        # E（错误待重试）或无标记（崩溃断点/入队前中断）→ 允许补投
        logger.info(f"Requeue allowed for crashed-pending data {data.get('UUID', '?')} "
                    f"(cache flag={flag!r}).")
        return True

    def _check_duplication_in_processed_data(self, data: dict):
        return (self._check_duplication_in_db(data, 'INFORMANT', self.archive_db_query_engine) or
                self._check_duplication_in_queue(data, 'INFORMANT', self.processed_queue))

    # ---------------------------- Before Process ----------------------------

    def _enqueue_collected_data(self, data: dict) -> True or Error:
        self._cache_original_data(data)
        # P2 修复：原 put() 无超时，队列满时 /collect 的 Web 线程永久阻塞，
        # 4 个并发采集即可拖垮全站（含健康检查）。数据已入 cache 不丢，
        # 超时返回 Error 由调用方回 503。
        try:
            self.original_queue.put(data, block=True, timeout=5)
        except queue.Full:
            logger.error(f"original_queue full (timeout=5s), reject UUID={data.get('UUID', 'unknown')}")
            return IntelligenceHub.Error(error_list=['Queue full, service busy. Try again later.'])
        return True

    def _process_appendix_time(self, original_data: dict, processed_data: dict):
        if pub_time := original_data.get('pub_time', None):
            # P0-4b 修复: 修复后 pydantic validator 已生效，pub_time 可能是 datetime 对象
            # 需要短路避免对 datetime 对象调用 time_str_to_datetime
            if isinstance(pub_time, datetime.datetime):
                pub_time_dt = pub_time
            else:
                # P-review 修复：解析全失败时置 None，不再用 `or pub_time`
                # 把无法解析的原始字符串存进 __TIME_PUB__（下游按 datetime 处理会炸）。
                pub_time_dt = time_digit_list_to_datetime(pub_time) or time_str_to_datetime(pub_time)
            processed_data['APPENDIX'][APPENDIX_TIME_PUB] = pub_time_dt
        else:
            # pub_time 允许为 None（下游爬虫不再伪造当前时间）：__TIME_PUB__ 同样置 None
            processed_data['APPENDIX'][APPENDIX_TIME_PUB] = None

        processed_data['APPENDIX'][APPENDIX_TIME_GOT] = original_data.get('collect_time') or original_data.get('__TIME_GOT__')
        processed_data['APPENDIX'][APPENDIX_TIME_POST] = original_data.get(APPENDIX_TIME_POST, None)
        processed_data['APPENDIX'][APPENDIX_TIME_DONE] = get_aware_time()

    def _enqueue_processed_data(self, data: dict) -> True or Error:
        try:
            # P-review 修复：put 增加 5s 超时（原为无超时的阻塞 put，并删除
            # 被注释的 article_time 死逻辑）。队列满时把 cache 文档标记为 E(待恢复)，
            # 由重启恢复流程重新加载（与 collected 路径"已入 cache 不丢"策略一致）。
            self.processed_queue.put(data, block=True, timeout=5)
            return True

        except queue.Full:
            logger.error(f"processed_queue full (timeout=5s), mark cache E for recovery, "
                         f"UUID={data.get('UUID', 'unknown')}")
            self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_ERROR)
            return IntelligenceHub.Error(error_list=['Processed queue full, marked for recovery.'])
        except Exception as e:
            self._mark_cache_data_archived_flag(data['UUID'], ARCHIVED_FLAG_ERROR)
            logger.error(f"Enqueue archived data error: {str(e)}")
            print(traceback.format_exc())
            return IntelligenceHub.Error(e, [str(e)])

    # ---------------------------- Archive Related ----------------------------

    def _index_archived_data(self, data: dict):
        try:
            self.vectorize_queue.put_nowait(data)
        except queue.Full:
            # P2 修复：原静默 pass 导致向量化丢数据无任何痕迹。
            # 数据仍在 Mongo archive，可人工重索引，但必须留下告警与计数。
            self.vectorize_drop_count += 1
            logger.warning(
                f"vectorize_queue full, drop vectorize for UUID={data.get('UUID', 'unknown')} "
                f"(total dropped: {self.vectorize_drop_count}). Data remains in Mongo archive."
            )
        except Exception as e:
            logger.error(str(e))

    def _cache_original_data(self, data: dict):
        try:
            if self.mongo_db_cache:
                if self._check_duplication_in_db(data, 'informant', self.cache_db_query_engine):
                    logger.info(f"Found duplicated data in cache db. Drop.")
                else:
                    self.mongo_db_cache.insert(data)
        except Exception as e:
            logger.error(f'Cache original data fail: {str(e)}')

    def _archive_processed_data(self, data: dict):
        if self.mongo_db_archive:
            self.mongo_db_archive.insert(data)
            # self.intelligence_cache.encache(data)

    # ---------------------------- Failed Archive Dump ----------------------------

    def _dump_failed_archive(self, data: dict):
        """归档 insert 失败时先把数据 JSON 落盘到 _data/failed_archive/<uuid>.json，
        防止 cache 文档被清理后数据彻底丢失；重试成功后由 _remove_failed_archive_dump 清理。"""
        try:
            dump_dir = os.path.join(DATA_PATH, 'failed_archive')
            os.makedirs(dump_dir, exist_ok=True)
            file_path = os.path.join(dump_dir, f"{data.get('UUID', 'unknown')}.json")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            logger.warning(f"Archive-failed data dumped to {file_path}")
        except Exception as e:
            logger.error(f"Dump failed-archive data fail: {e}")

    def _remove_failed_archive_dump(self, _uuid: str):
        """归档（含重试）成功后清理对应的落盘文件，文件不存在时静默跳过。"""
        try:
            file_path = os.path.join(DATA_PATH, 'failed_archive', f"{_uuid}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Removed failed-archive dump {file_path}")
        except Exception as e:
            logger.warning(f"Remove failed-archive dump fail: {e}")

    # ---------------------------- Low-value Duplication ----------------------------

    def _check_low_value_duplication(self, data: dict) -> bool:
        """低值库 informant 查重（best-effort）：查询失败时不阻塞入库。"""
        try:
            informant = self._normalize_url(data.get('INFORMANT', ''))
            if not informant or not self.mongo_db_low_value:
                return False
            return self.mongo_db_low_value.collection.count_documents(
                {'INFORMANT': informant}, limit=1) > 0
        except Exception as e:
            logger.warning(f'Low-value duplication check fail: {e}')
            return False

    # ---------------------------- Similar Recall ----------------------------

    def _recall_similar_messages(self, original_data: dict) -> List[dict]:
        """AI 分析前的相似消息召回（best-effort）。

        用 summary 向量引擎对 title+content 检索 top_n=3，5s 超时。
        任何异常/超时/服务未就绪都返回空列表并记日志，绝不阻断分析流程。
        返回 [{'uuid', 'similarity', 'content'}]，供 AnalyzerProxy 注入 prompt
        与 IntelligenceScoringEngine 做 0.86/0.92 相似度惩罚。
        """
        if not self.similar_recall_enabled:
            return []
        with self.lock:
            engine = self.vector_db_engine_summary
        if engine is None:
            # 向量服务未启用/未就绪：静默跳过
            return []

        title = str(original_data.get('title', '') or '')
        content = str(original_data.get('content', '') or '')
        query_text = f"{title}\n{content[:1500]}".strip()
        if not query_text:
            return []

        try:
            future = self._similar_recall_executor.submit(engine.query, query_text, 3, 0.5)
            results = future.result(timeout=5)
        except Exception as e:
            logger.warning(f"Similar recall failed (ignored, analysis continues): {e}")
            return []

        similar = []
        seen = set()
        for r in results or []:
            doc_id = r.get('doc_id')
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            similar.append({
                'uuid': doc_id,
                'similarity': r.get('score', 0.0),
                'content': str(r.get('content', '') or '')[:200],
            })
        return similar

    def _record_legacy_analysis_usage(self, resp: Optional[dict], original_uuid: str,
                                      prompt_version: str) -> None:
        """旧 AI Worker 调用后记账（进入与 Drainer/翻译同一费用台账）。

        旧路径模型来自多厂商 ai_client_config，通常不在 Flash/Pro 价格表中：
        费用记为未知（cost_cny=None，status=UNKNOWN_PRICE），
        调用次数与 token 用量如实记录——封堵"只检查预算而不记录消费"的漏洞。
        同时向 BudgetGuard 计一次调用（费用按 0 累计，次数限制仍生效）。
        """
        router = self.model_router
        if router is None or getattr(router, 'ledger', None) is None:
            return
        try:
            usage = {}
            if isinstance(resp, dict):
                raw_usage = resp.get('usage') or {}
                if isinstance(raw_usage, dict):
                    usage = raw_usage
            input_tok = int(usage.get('prompt_tokens', 0) or 0)
            output_tok = int(usage.get('completion_tokens', 0) or 0)
            model_name = str((resp or {}).get('model', '') or 'legacy-unknown')
            router.ledger.record(
                attempt_id=f'legacy-{uuid.uuid4().hex}',
                model=model_name,
                input_tokens=input_tok,
                output_tokens=output_tok,
                cost_cny=None,  # 旧路径模型价格未配置：费用未知不按 0 静默混入
                is_dry_run=bool(self.api_dry_run),
                task_id=f'legacy:{original_uuid}',
                status=STATUS_UNKNOWN_PRICE,
            )
            if self.budget_guard is not None:
                self.budget_guard.record(0.0)
        except Exception as e:
            logger.warning('Legacy analysis usage ledger record fail: %s', e)

    def _drainer_build_messages(self, task: dict):
        """批次二：构造与实际发送完全一致的分析消息（预估与调用共用）。

        与 _drainer_invoke_handler 使用同一逻辑（V25 prompt + 正文），
        保证"预估输入费用"覆盖提示词与正文、"预估输出上限"与实际请求
        的 max_tokens（MAX_OUTPUT_TOKEN=8192）一致——
        修复原先按 task['content'] 估算导致正文被当空字符串、
        输出上限 2048 与实际 8192 不一致的低估缺陷。
        """
        from ServiceComponent.IntelligenceAnalyzerProxy import build_analyze_message
        original_data = dict(task.get('original_data') or {})
        if not original_data:
            # 兜底：直连形态（测试）task 自带 content
            return ([{'role': 'user', 'content': str(task.get('content', '') or '')}],
                    int(task.get('max_tokens', 2048)))
        selected_prompt_index = next(iter(ANALYSIS_PROMPT_TABLE))
        selected_prompt = ANALYSIS_PROMPT_TABLE[selected_prompt_index]
        messages = build_analyze_message(selected_prompt, original_data)
        from ServiceComponent.IntelligenceAnalyzerProxy import MAX_OUTPUT_TOKEN
        return messages, MAX_OUTPUT_TOKEN

    def _drainer_estimate_messages(self, task: dict):
        """Drainer 预估钩子（注入 estimate_messages_fn）。"""
        try:
            return self._drainer_build_messages(task)
        except Exception as e:
            logger.warning("预估消息构造失败，退回 content 文本估算：%s", e)
            return ([{'role': 'user', 'content': str(task.get('content', '') or '')}],
                    int(task.get('max_tokens', 2048)))

    # ---------------------------- Translation Guardrails (Batch-1) ----------------------------
    def _translation_call_gate(self) -> None:
        """后台翻译调用的统一护栏：不满足时抛异常（由翻译模块放弃本次调用）。

        检查顺序：总开关 → 模拟模式 → 允许时段 → 预算。
        翻译使用独立小模型池（translate_small），不走 Flash/Pro 分级路由，
        但预算守卫与时间策略与主分析路径共用同一实例，无法绕过。
        """
        if not self.api_enabled:
            raise RuntimeError('api_enabled=false，禁止后台翻译调用。')
        if self.api_dry_run:
            raise RuntimeError('api_dry_run=true，禁止后台翻译真实调用。')
        if self.time_policy is not None and not self.time_policy.allowed_to_start_request():
            raise RuntimeError('当前不在允许调用窗口（峰谷策略），禁止后台翻译调用。')
        if self.budget_guard is not None and not self.budget_guard.can_call():
            raise RuntimeError('预算或调用次数已耗尽，禁止后台翻译调用。')

    def _record_translation_usage(self, resp: Optional[dict], task_uuid: str = '') -> None:
        """后台翻译调用记账：实际调用进入同一费用台账（ApiUsageLedger）。

        翻译池模型不在 Flash/Pro 价格表中，费用记为未知（cost_cny=None，
        status=UNKNOWN_PRICE），调用次数与 token 用量仍如实记录——
        不允许"只调用不记账"。
        """
        router = self.model_router
        if router is None or getattr(router, 'ledger', None) is None:
            return
        try:
            usage = {}
            if isinstance(resp, dict):
                raw_usage = resp.get('usage') or {}
                if isinstance(raw_usage, dict):
                    usage = raw_usage
            input_tok = int(usage.get('prompt_tokens', 0) or 0)
            output_tok = int(usage.get('completion_tokens', 0) or 0)
            model_name = str((resp or {}).get('model', '') or 'translate_small')
            inserted = router.ledger.record(
                attempt_id=f'translate-{uuid.uuid4().hex}',
                model=model_name,
                input_tokens=input_tok,
                output_tokens=output_tok,
                cost_cny=None,  # 价格未配置，费用未知不按 0 静默
                is_dry_run=bool(self.api_dry_run),
                task_id=f'translate:{task_uuid}',
                status=STATUS_UNKNOWN_PRICE,
            )
            if not inserted and router.ledger.is_degraded():
                logger.warning('翻译记账写入失败（台账降级），费用可能少记。')
        except Exception as e:
            logger.warning('Translation usage ledger record fail: %s', e)

    # ---------------------------- OffPeak Drainer Callbacks (M3) ----------------------------

    def _drainer_invoke_handler(self, task: dict):
        """OffPeakQueueDrainer 的业务调用入口（M3 接线新增）。

        职责：构造 V25 prompt → 经适配器调用 Mock/真实 DeepSeek 客户端 →
        解析业务结果并构造 ProcessedData（与旧 Worker 完成路径语义对齐：
        低值打 D 标记进 processed_queue；解析错误抛异常由队列重试）。
        返回 (resp, business_result)：resp 含 usage 供 Drainer 统一记账。
        """
        original_data = dict(task.get('original_data') or {})
        original_uuid = str(original_data.get('UUID', '') or task.get('original_uuid', ''))
        is_priority = bool(task.get('is_priority', False))

        # 模型分级路由：重点来源（is_priority）用 Pro 复核，普通内容用 Flash
        model = 'deepseek-chat'
        if self.model_router is not None:
            model = self.model_router.route(is_priority)

        # 客户端选择：DRY_RUN 用 Mock；真实模式用 Drainer 注入的 real_client
        if self.model_router is not None and self.model_router.api_dry_run:
            raw_client = self.off_peak_drainer.mock_client
        else:
            raw_client = self.off_peak_drainer.real_client
            if raw_client is None:
                raise RuntimeError(
                    "真实 API 模式（api_dry_run=false）未注入 real_client，拒绝调用以防误产生费用。")

        adapter = _DrainerClientAdapter(raw_client, model)
        selected_prompt_index = next(iter(ANALYSIS_PROMPT_TABLE))
        selected_prompt = ANALYSIS_PROMPT_TABLE[selected_prompt_index]

        # 相似召回与旧 Worker 一致：best-effort，绝不阻断
        similar_messages = self._recall_similar_messages(original_data)
        result = analyze_with_ai(adapter, selected_prompt, original_data,
                                 similar_messages=similar_messages)

        if not result or 'error' in result:
            # 分析失败：抛异常交由 Drainer 队列重试（与旧 Worker E 标记语义对齐）
            err_desc = result.get('error') if isinstance(result, dict) else 'empty response'
            err = RuntimeError(f"AI analysis error for {original_uuid}: {err_desc}")
            # P1-6：API 调用已成功（费用已产生），解析失败仍要把 usage 交回
            # Drainer 记账（status=PARSE_FAILED），封堵"成功响应但解析失败
            # 绕过费用记录"的漏洞
            self._drainer_attach_api_usage(err, adapter)
            raise err

        # 成功：补齐 UUID / INFORMANT（v2x prompt 不提取这两个字段）
        result['UUID'] = original_uuid
        result['INFORMANT'] = str(original_data.get('informant', '')).strip()

        # 低值数据：打 D 标记并直接进 processed_queue（post_process 负责入低值库）
        if self._is_low_value_data(result):
            self._mark_cache_data_archived_flag(original_uuid, ARCHIVED_FLAG_DROP)
            with self.lock:
                self.drop_counter += 1
            business = dict(result)
            business['SUBMITTER'] = 'OffPeakDrainer'
            return (adapter.last_response or {}), business

        # 解析异常防护：与旧 Worker 相同的判定（包 items 或缺 EVENT_TEXT 属解析错误）
        if 'items' in result or 'EVENT_TEXT' not in result:
            err = ValueError(
                f'AI output parse error: missing EVENT_TEXT or wrapped in items. UUID={original_uuid}')
            self._drainer_attach_api_usage(err, adapter)
            raise err

        validated_data, error_text = check_sanitize_dict(dict(result), ArchivedData)
        if error_text:
            err = ValueError(error_text)
            self._drainer_attach_api_usage(err, adapter)
            raise err

        # 评分与 APPENDIX（与旧 Worker 完成路径对齐）
        analysis_warnings = result.pop(APPENDIX_ANALYSIS_WARNING, None)
        validated_data['APPENDIX'][APPENDIX_PROMPT_VERSION] = selected_prompt_index
        validated_data['APPENDIX'][APPENDIX_AI_SERVICE] = adapter.get_api_base_url()
        validated_data['APPENDIX'][APPENDIX_AI_MODEL] = adapter.get_current_model()
        if analysis_warnings:
            validated_data['APPENDIX'][APPENDIX_ANALYSIS_WARNING] = analysis_warnings

        scoring_engine = IntelligenceScoringEngine()
        total_score = scoring_engine.calculate_single(validated_data,
                                                      similar_messages=similar_messages)
        validated_data['APPENDIX'][APPENDIX_TOTAL_SCORE] = total_score

        validated_data['RAW_DATA'] = original_data
        validated_data['SUBMITTER'] = 'OffPeakDrainer'

        with self.lock:
            if 'warning' in result:
                self.conversation_warning += 1
            self.conversation_total += 1

        return (adapter.last_response or {}), validated_data

    def _enqueue_push_task(self, data: dict) -> None:
        """P1-7：归档成功后提交推送任务（best-effort，不阻断归档链路）。

        - 推送服务未注入/未启用：直接跳过（返回，不计失败）；
        - 磁盘 >=85% 降级：跳过推送（低优先级任务让位核心链路）；
        - 按配置的 min_total_score 过滤低分内容；
        - 提交失败（如 Mongo 暂不可用）仅告警：任务可在下次归档重放或
          由运维对 push_tasks 集合补投。
        """
        notifier = getattr(self, 'push_notifier', None)
        if notifier is None or not notifier.is_enabled():
            return
        try:
            if self.disk_guard is not None and self.disk_guard.should_stop_push:
                logger.warning("[DiskGuard] 磁盘 >=85%%，跳过推送任务 %s。",
                               data.get('UUID', '?'))
                return
            total_score = ((data.get('APPENDIX') or {})
                           .get(APPENDIX_TOTAL_SCORE, None))
            if not notifier.should_push(total_score):
                return
            appendix = data.get('APPENDIX') or {}
            task = {
                'item_id': str(data.get('UUID', '')),
                'title': str(data.get('EVENT_TITLE', '')),
                'brief': str(data.get('EVENT_BRIEF', '')),
                'url': str(data.get('INFORMANT', '')),
                'total_score': total_score,
                'archived_at': str(appendix.get(APPENDIX_TIME_ARCHIVED, '')),
            }
            if notifier.submit(task):
                with self.lock:
                    self.push_task_count += 1
        except Exception as e:
            logger.warning("推送任务提交失败（不影响归档）：%s", e)

    @staticmethod
    def _drainer_attach_api_usage(exc: Exception, adapter) -> None:
        """P1-6：把成功响应的 usage 附加到解析失败异常上。

        Drainer._record_failed_attempt 读取异常的 api_usage 属性，
        按 PARSE_FAILED 状态记账——费用不因解析失败而遗漏。
        """
        resp = getattr(adapter, 'last_response', None)
        if isinstance(resp, dict):
            usage = resp.get('usage') or {}
            exc.api_usage = {
                'input_tokens': usage.get('prompt_tokens', 0) or 0,
                'output_tokens': usage.get('completion_tokens', 0) or 0,
            }

    def _drainer_on_complete(self, item_id: str, result: dict):
        """Drainer 单项完成回调（M3 接线 / P1-5 语义修订）：返回是否接收成功。

        返回 True 表示结果已进入 processed_queue（归档链路已接收），
        Drainer 据此标记 DELIVERED；返回 False（队列满/异常）时保持
        未交付状态，由 Drainer 重放机制再次投递，不丢失分析结果。
        队列满不再打 E 标记——E 标记的恢复路径依赖旧 AI 工作线程，
        在 Drainer 模式下不可用（P1-5 修复的恢复断点）。
        """
        business = (result or {}).get('business') if isinstance(result, dict) else None
        if not isinstance(business, dict):
            # 无业务结果（如 DRY_RUN 纯内容项）：视为已接收（结果已持久化）
            return True
        original_uuid = str(business.get('UUID', '') or '')
        if not self._enqueue_processed_data(business):
            logger.error(f"Drainer result enqueue processed_queue fail, "
                         f"keep undelivered for replay: {original_uuid}")
            return False
        logger.info(f"Drainer item {item_id} analysis result enqueued for archive: {original_uuid}")
        return True

    def _drainer_archive_confirmed(self, business) -> bool:
        """批次三：归档链路最终确认回调（注入 Drainer.archive_confirm_fn）。

        判定该业务结果是否已被归档链路处理完毕：
        - cache 文档标记 A（归档成功）；
        - 或 U（post_process 查重确认已存在相同归档结果）；
        - 或 D（低价值信息保存路径完成）。

        内存队列（processed_queue）接收成功不代表持久归档完成——
        Drainer 据本回调决定 DELIVERED 项是否需要重投对账。
        """
        if not isinstance(business, dict):
            return False
        original_uuid = str(business.get('UUID', '') or '').strip()
        if not original_uuid:
            return False
        try:
            docs = self.cache_db_query_engine.common_query(
                conditions={'UUID': original_uuid}, limit=2) or []
        except Exception as e:
            logger.warning(f"Archive confirm query fail for {original_uuid}: {e}")
            return False  # 查询失败按未确认处理（宁可重投，归档幂等兜底）
        if not docs:
            return False
        doc = docs[0]
        appendix = doc.get('APPENDIX') or {}
        flag = doc.get(APPENDIX_ARCHIVED_FLAG, appendix.get(APPENDIX_ARCHIVED_FLAG))
        confirmed = flag in (ARCHIVED_FLAG_ARCHIVED, ARCHIVED_FLAG_DUPLICATED,
                             ARCHIVED_FLAG_DROP)
        if confirmed:
            logger.info(f"Drainer item archive confirmed (flag={flag}): {original_uuid}")
        return confirmed

    def _mark_cache_data_archived_flag(self, _uuid: str, archived: bool or str):
        """
        20250530: Extend the archived parameter as str. It can be the following values:
            'T' - True. Archived
            'F' - False. Low value data so not archived
            'E' - Error. We should go back and check the error, then analysis again.
        :param _uuid:
        :param archived:
        :return:
        """
        try:
            if isinstance(archived, bool):
                archived = ARCHIVED_FLAG_ARCHIVED if archived else ARCHIVED_FLAG_DROP
            if self.mongo_db_cache:
                self.mongo_db_cache.update({
                    'UUID': _uuid},
                    {f'APPENDIX.{APPENDIX_ARCHIVED_FLAG}': archived})
        except Exception as e:
            logger.error(f'Mark archived data flag fail: {str(e)}')

    def _bootstrap_entity_frequency_cache(self):
        """程序启动时，在后台线程中预构建最近30天的实体频率缓存。"""
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            end_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_time = end_time - datetime.timedelta(days=30)
            logger.info(f"Entity frequency bootstrap: building cache from {start_time.date()} to {end_time.date()}")
            for progress in self.entity_frequency_engine.build_cache_with_progress(start_time, end_time):
                if self.shutdown_flag.is_set():
                    self.entity_frequency_engine.cancel_build()
                    logger.info("Entity frequency bootstrap cancelled due to shutdown.")
                    break
            logger.info("Entity frequency bootstrap completed.")
        except Exception as e:
            logger.warning(f"Entity frequency bootstrap failed: {e}")

    def _do_build_entity_frequency_cache(self):
        """每小时任务：构建上一个完整天的实体频率缓存。"""
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_start = day_end - datetime.timedelta(days=1)
            # 消费生成器以执行构建（后台任务不需要进度）
            for _ in self.entity_frequency_engine.build_cache_with_progress(day_start, day_end):
                pass
        except Exception as e:
            logger.warning(f"Entity frequency cache build failed: {e}")
