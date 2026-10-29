# -*- coding: utf-8 -*-
"""
M3 护栏接线测试：验证护栏组件与主链路的接线行为。

覆盖：
- build_china_watch_guardrails 的 fail-fast（互斥 / effective_from 空 / DEEPSEEK_API_KEY 缺失）
- 旧 AI Worker 三重护栏判定 _ai_call_allowed（DRY_RUN / 高峰 / 预算耗尽 → 拒绝调用）
- startup() 分派（DRY_RUN 或 Drainer 启用时不启动旧分析线程）
- 预筛三分类分派（拒绝 / 元数据 M 标记 / NEEDS_AI 进 Drainer 延期队列）
- Drainer 消费链路（invoke_handler 调用 → 费用记账 → on_complete 归档回调）
- informant 域名反查（SourceRegistry.resolve_by_informant + RulePreFilter 兜底）

约束：不连接真实 MongoDB、不调用真实 API，全部使用 Mock / 内存后端 / FakeClock。
"""
import os
import sys
import threading
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from ServiceComponent.ApiTimePolicy import ApiTimePolicy
from ServiceComponent.ApiUsageLedger import ApiUsageLedger
from ServiceComponent.BudgetGuard import BudgetGuard
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient
from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer
from ServiceComponent.PersistentQueue import MemoryQueueBackend, PersistentQueue
from ServiceComponent.PricingPolicyRegistry import ModelPricing, PricingPolicyRegistry
from ServiceComponent.RulePreFilter import RulePreFilter
from ServiceComponent.SourceRegistry import SourceRegistry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_YAML = os.path.join(ROOT, 'sources', 'china_watch_sources.yaml')

try:
    from IntelligenceHub import IntelligenceHub
    HUB_IMPORTABLE = True
except Exception:  # 环境缺重依赖时跳过 Hub 相关用例
    HUB_IMPORTABLE = False


def _import_startup_builder():
    """惰性导入 Startup 组装函数。

    IntelligenceHubStartup 顶层导入 Tools.SystemMonitorService → psutil、
    PyLoggingBackend → pythonjsonlogger 等重依赖；本机精简环境缺失时用
    MagicMock 顶替（仅测试进程内生效，生产环境装有真实依赖不受影响）。
    """
    for mod in ('psutil', 'pythonjsonlogger', 'pythonjsonlogger.jsonlogger'):
        if mod not in sys.modules:
            try:
                __import__(mod)
            except ImportError:
                sys.modules.setdefault(mod, MagicMock())
    try:
        from IntelligenceHubStartup import build_china_watch_guardrails as builder
        return builder
    except Exception:
        return None


build_china_watch_guardrails = _import_startup_builder()
STARTUP_IMPORTABLE = build_china_watch_guardrails is not None


# ---------------------------------------------------------------------------
# 测试设施
# ---------------------------------------------------------------------------

class FakeConfig:
    """EasyConfig 替代：点路径 get，内存字典存储。"""

    def __init__(self, data: Optional[Dict] = None):
        self._data = data or {}

    def get(self, path: str, default=None):
        node: Any = self._data
        for part in path.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeClockPolicy(ApiTimePolicy):
    """可注入固定时钟的 ApiTimePolicy（30-testing 规则要求）。"""

    def __init__(self, fixed_now: datetime, **kwargs):
        super().__init__(**kwargs)
        self._fixed_now = fixed_now

    def now(self) -> datetime:
        return self._fixed_now


def make_router(api_enabled: bool = True, api_dry_run: bool = True,
                fixed_now: Optional[datetime] = None) -> DeepSeekModelRouter:
    """构造带 FakeClock 的完整路由组件链。"""
    if fixed_now is not None:
        time_policy = FakeClockPolicy(fixed_now)
    else:
        time_policy = ApiTimePolicy()
    pricing = PricingPolicyRegistry(
        flash_model='flash', pro_model='pro',
        flash_pricing=ModelPricing(model_name='flash', is_flash=True),
        pro_pricing=ModelPricing(model_name='pro', is_flash=False))
    budget = BudgetGuard()
    ledger = ApiUsageLedger()
    return DeepSeekModelRouter(time_policy, pricing, budget, ledger,
                               api_enabled=api_enabled, api_dry_run=api_dry_run)


def make_minimal_hub(**attrs) -> Any:
    """绕过重型 __init__ 构造最小 Hub 实例，手动注入护栏相关属性。

    仅赋值接线代码路径触及的属性，避免连接 MongoDB / 启动线程。
    """
    hub = IntelligenceHub.__new__(IntelligenceHub)
    hub.api_enabled = attrs.get('api_enabled', False)
    hub.api_dry_run = attrs.get('api_dry_run', True)
    hub.time_policy = attrs.get('time_policy')
    hub.budget_guard = attrs.get('budget_guard')
    hub.model_router = attrs.get('model_router')
    hub.prefilter = attrs.get('prefilter')
    hub.off_peak_drainer = attrs.get('off_peak_drainer')
    hub.use_legacy_ai_worker = attrs.get('use_legacy_ai_worker', True)
    hub.use_off_peak_drainer = attrs.get('use_off_peak_drainer', False)
    hub.lock = threading.Lock()
    hub.prefilter_rejected_count = 0
    hub.prefilter_metadata_only_count = 0
    hub.prefilter_needs_ai_count = 0
    return hub


# ---------------------------------------------------------------------------
# 1) build_china_watch_guardrails fail-fast
# ---------------------------------------------------------------------------

@unittest.skipUnless(STARTUP_IMPORTABLE, "IntelligenceHubStartup 不可导入（环境缺依赖）")
class TestGuardrailAssemblyFailFast(unittest.TestCase):

    def _cfg(self, china_watch: Dict, api_policy: Optional[Dict] = None) -> FakeConfig:
        return FakeConfig({'intelligence_hub': {
            'china_watch': china_watch,
            'api_policy': api_policy if api_policy is not None else {
                'effective_from': '2026-01-01T00:00:00+08:00'},
            'budget': {}, 'pricing': {}, 'prefilter': {},
        }})

    def test_mutex_rejects_both_true(self):
        """互斥配置（旧 Worker 与 Drainer 同 true）必须拒绝启动。"""
        from ServiceComponent.ConsumerMutex import ConsumerMutexError
        cfg = self._cfg({'use_legacy_ai_worker': True, 'use_off_peak_drainer': True})
        with self.assertRaises(ConsumerMutexError):
            build_china_watch_guardrails(cfg, 'localhost', 27017, '', '')

    def test_effective_from_empty_rejects_real_mode(self):
        """真实模式 + 生效日期为空 → RuntimeError（规则红线）。"""
        cfg = self._cfg({'api_enabled': True, 'api_dry_run': False},
                        api_policy={'effective_from': ''})
        with self.assertRaises(RuntimeError):
            build_china_watch_guardrails(cfg, 'localhost', 27017, '', '')

    def test_missing_deepseek_key_rejects_real_mode(self):
        """真实模式 + DEEPSEEK_API_KEY 缺失 → RuntimeError（fail-fast）。"""
        os.environ.pop('DEEPSEEK_API_KEY', None)
        cfg = self._cfg({'api_enabled': True, 'api_dry_run': False})
        with self.assertRaises(RuntimeError):
            build_china_watch_guardrails(cfg, 'localhost', 27017, '', '')

    def test_collect_only_mode_assembles(self):
        """只采集模式（两者皆 false）正常组装全部组件。"""
        cfg = self._cfg({'api_enabled': False, 'api_dry_run': True,
                         'use_legacy_ai_worker': False, 'use_off_peak_drainer': False})
        parts = build_china_watch_guardrails(cfg, 'localhost', 27017, '', '')
        self.assertIsNotNone(parts['time_policy'])
        self.assertIsNotNone(parts['budget_guard'])
        self.assertIsNotNone(parts['model_router'])
        self.assertIsNotNone(parts['prefilter'])  # prefilter.enabled 默认 true
        self.assertIsNone(parts['drainer'])

    def test_off_peak_only_false_disables_time_policy(self):
        """off_peak_only=false 按第一阶段约束 fail-closed：时间策略禁用。"""
        cfg = self._cfg({'api_enabled': False, 'api_dry_run': True},
                        api_policy={'effective_from': '2026-01-01T00:00:00+08:00',
                                    'off_peak_only': False})
        parts = build_china_watch_guardrails(cfg, 'localhost', 27017, '', '')
        self.assertFalse(parts['time_policy'].allowed_to_start_request())


# ---------------------------------------------------------------------------
# 2) 旧 Worker 护栏判定
# ---------------------------------------------------------------------------

@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestAiCallAllowed(unittest.TestCase):

    def test_dry_run_blocks_call(self):
        """api_dry_run=true → 护栏拒绝（fail-closed，修复死开关）。"""
        hub = make_minimal_hub(api_enabled=True, api_dry_run=True,
                               time_policy=ApiTimePolicy(),
                               budget_guard=BudgetGuard())
        self.assertFalse(hub._ai_call_allowed('test'))

    def test_peak_window_blocks_call(self):
        """高峰时段（10:00 处于 09:00-12:00）→ 拒绝调用。"""
        hub = make_minimal_hub(api_enabled=True, api_dry_run=False,
                               time_policy=FakeClockPolicy(datetime(2026, 8, 28, 10, 0, 0)),
                               budget_guard=BudgetGuard())
        self.assertFalse(hub._ai_call_allowed('test'))

    def test_budget_exhausted_blocks_call(self):
        """预算耗尽 → 拒绝调用（非高峰时段下验证预算独立生效）。"""
        budget = BudgetGuard(daily_budget_cny=0.1)
        budget.record(0.2)  # 超预算
        hub = make_minimal_hub(api_enabled=True, api_dry_run=False,
                               time_policy=FakeClockPolicy(datetime(2026, 8, 28, 20, 0, 0)),
                               budget_guard=budget)
        self.assertFalse(hub._ai_call_allowed('test'))

    def test_off_peak_with_budget_allows_call(self):
        """非高峰（20:00）+ 预算充足 + 非 DRY_RUN → 允许调用。"""
        hub = make_minimal_hub(api_enabled=True, api_dry_run=False,
                               time_policy=FakeClockPolicy(datetime(2026, 8, 28, 20, 0, 0)),
                               budget_guard=BudgetGuard(daily_budget_cny=1.0))
        self.assertTrue(hub._ai_call_allowed('test'))


# ---------------------------------------------------------------------------
# 3) startup 分派（DRY_RUN / Drainer 时不启动旧分析线程）
# ---------------------------------------------------------------------------

@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestStartupDispatch(unittest.TestCase):

    def _hub_for_startup(self, **attrs):
        hub = make_minimal_hub(**attrs)
        hub.start_analysis_threads = MagicMock()
        hub.post_process_thread = MagicMock()
        hub.intelligence_vectorization_thread = MagicMock()
        return hub

    def test_dry_run_does_not_start_legacy_worker(self):
        """api_dry_run=true：旧分析线程不启动（数据保留在 cache 与队列）。"""
        hub = self._hub_for_startup(api_enabled=True, api_dry_run=True)
        hub.startup(1)
        hub.start_analysis_threads.assert_not_called()

    def test_drainer_mode_does_not_start_legacy_worker(self):
        """use_off_peak_drainer=true：旧分析线程不启动（互斥），Drainer 启动。"""
        drainer = MagicMock()
        hub = self._hub_for_startup(api_enabled=True, api_dry_run=False,
                                    use_off_peak_drainer=True,
                                    off_peak_drainer=drainer)
        hub.startup(1)
        hub.start_analysis_threads.assert_not_called()
        drainer.start.assert_called_once()

    def test_legacy_mode_starts_worker(self):
        """仅旧 Worker 模式（无护栏阻断条件）正常启动分析线程。"""
        hub = self._hub_for_startup(api_enabled=True, api_dry_run=False)
        hub.startup(2)
        hub.start_analysis_threads.assert_called_once_with(2)

    def test_collect_only_starts_nothing(self):
        """api_enabled=false：不启动旧 Worker；注入的 Drainer 正常启动（内部拒绝真实调用）。"""
        drainer = MagicMock()
        hub = self._hub_for_startup(api_enabled=False, api_dry_run=True,
                                    use_off_peak_drainer=True, off_peak_drainer=drainer)
        hub.startup(1)
        hub.start_analysis_threads.assert_not_called()
        drainer.start.assert_called_once()


# ---------------------------------------------------------------------------
# 4) 预筛三分类分派
# ---------------------------------------------------------------------------

@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestPrefilterDispatch(unittest.TestCase):

    def setUp(self):
        self.registry = SourceRegistry(SOURCES_YAML)
        # 注意：CollectedData.content 有 Pydantic min_length=10 先行校验，
        # 预筛 min_content_length 的 METADATA_ONLY 分支仅在配置值 > 10 时可达；
        # 此处用 metadata_only_patterns 触发 Level 3b 路径验证分派逻辑。
        rules = {'min_content_length': 10, 'require_title': True,
                 'reject_patterns': ['赌博'], 'metadata_only_patterns': ['SUBSCRIBE_ONLY']}
        self.prefilter = RulePreFilter(self.registry, rules)
        self.marked: List = []
        self.cached: List = []
        self.enqueued_legacy: List = []

    def _hub(self, drainer_enabled: bool):
        drainer = MagicMock() if drainer_enabled else None
        hub = make_minimal_hub(prefilter=self.prefilter,
                               use_off_peak_drainer=drainer_enabled,
                               off_peak_drainer=drainer)
        hub._check_duplication_in_unprocess_data = lambda data: False
        hub._cache_original_data = lambda data: self.cached.append(data)
        hub._mark_cache_data_archived_flag = lambda u, f: self.marked.append((u, f))
        hub._enqueue_collected_data = lambda data: self.enqueued_legacy.append(data) or True
        return hub

    @staticmethod
    def _sample_data(**extra) -> Dict:
        data = {
            'UUID': 'uuid-test-0001',
            'token': 'tok',
            'title': 'US China policy update',
            'content': 'A' * 50,
            'informant': 'https://www.bbc.com/news/articles/x',
            'authors': [],
        }
        data.update(extra)
        return data

    def test_rejected_returns_error(self):
        """命中拒绝模式 → 返回 Error 且不入任何队列。"""
        hub = self._hub(drainer_enabled=False)
        result = hub.submit_collected_data(
            self._sample_data(content='赌博内容' * 10))
        self.assertFalse(bool(result))  # Error 的 __bool__ 为 False
        self.assertEqual(hub.prefilter_rejected_count, 1)
        self.assertEqual(self.enqueued_legacy, [])

    def test_metadata_only_marks_m_and_skips_ai(self):
        """命中元数据模式 → 仅元数据：入 cache、打 M 标记、不进 AI 队列。"""
        hub = self._hub(drainer_enabled=False)
        result = hub.submit_collected_data(
            self._sample_data(content='SUBSCRIBE_ONLY weekly digest content'))
        self.assertTrue(result is True)
        self.assertEqual(hub.prefilter_metadata_only_count, 1)
        self.assertEqual(len(self.cached), 1)
        self.assertEqual(self.marked, [('uuid-test-0001', 'M')])
        self.assertEqual(self.enqueued_legacy, [])

    def test_needs_ai_goes_to_drainer_queue(self):
        """NEEDS_AI + Drainer 启用 → 入延期队列（含 content_hash/is_priority）。"""
        hub = self._hub(drainer_enabled=True)
        result = hub.submit_collected_data(self._sample_data())
        self.assertTrue(result is True)
        self.assertEqual(hub.prefilter_needs_ai_count, 1)
        hub.off_peak_drainer.enqueue.assert_called_once()
        task = hub.off_peak_drainer.enqueue.call_args[0][0]
        self.assertEqual(task['original_uuid'], 'uuid-test-0001')
        self.assertTrue(task['content_hash'])
        self.assertTrue(task['is_priority'])  # bbc 是重点来源
        self.assertEqual(len(self.cached), 1)

    def test_needs_ai_goes_to_legacy_queue_when_drainer_off(self):
        """NEEDS_AI + 未启用 Drainer → 走原 original_queue 路径。"""
        hub = self._hub(drainer_enabled=False)
        result = hub.submit_collected_data(self._sample_data())
        self.assertTrue(result is True)
        self.assertEqual(hub.prefilter_needs_ai_count, 1)
        self.assertEqual(len(self.enqueued_legacy), 1)


# ---------------------------------------------------------------------------
# 5) informant 域名反查
# ---------------------------------------------------------------------------

class TestResolveByInformant(unittest.TestCase):

    def setUp(self):
        self.registry = SourceRegistry(SOURCES_YAML)

    def test_exact_and_subdomain_match(self):
        self.assertEqual(self.registry.resolve_by_informant(
            'https://www.bbc.com/news/x').source_id, 'bbc')
        self.assertEqual(self.registry.resolve_by_informant(
            'https://feeds.bbci.co.uk/x').source_id, 'bbc')
        self.assertEqual(self.registry.resolve_by_informant(
            'https://www.chinanews.com.cn/sl/2026/x.shtml').source_id, 'news_cn')
        self.assertEqual(self.registry.resolve_by_informant(
            'https://www.ecns.cn/m/a/x.html').source_id, 'chinanews')

    def test_unknown_domain_returns_none(self):
        self.assertIsNone(self.registry.resolve_by_informant('https://example.org/x'))
        self.assertIsNone(self.registry.resolve_by_informant(''))
        self.assertIsNone(self.registry.resolve_by_informant('not-a-url'))

    def test_ambiguous_domains_return_none(self):
        """域名重叠（歧义）→ None（fail-closed）。"""
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False,
                                         encoding='utf-8') as f:
            f.write("sources:\n"
                    "  - source_id: a\n    name: A\n    informant_domains: [x.com]\n"
                    "  - source_id: b\n    name: B\n    informant_domains: [x.com]\n")
            path = f.name
        try:
            registry = SourceRegistry(path)
            self.assertIsNone(registry.resolve_by_informant('https://x.com/p'))
        finally:
            os.remove(path)

    def test_prefilter_source_fallback_by_informant(self):
        """预筛 source 缺失时经 informant 反查兜底为 NEEDS_AI。"""
        prefilter = RulePreFilter(self.registry,
                                  {'min_content_length': 10, 'require_title': True,
                                   'reject_patterns': [], 'metadata_only_patterns': []})
        pf = prefilter.screen({'UUID': 'u1', 'informant': 'https://www.bbc.com/news/x',
                               'title': 't', 'content': 'c' * 30})
        self.assertEqual(pf.decision, 'NEEDS_AI')
        self.assertEqual(pf.source_id, 'bbc')

    def test_prefilter_unresolvable_source_rejected(self):
        """source 缺失且域名无法反查 → 拒绝（fail-closed）。"""
        prefilter = RulePreFilter(self.registry,
                                  {'min_content_length': 10, 'require_title': True,
                                   'reject_patterns': [], 'metadata_only_patterns': []})
        pf = prefilter.screen({'UUID': 'u1', 'informant': 'https://example.org/x',
                               'title': 't', 'content': 'c' * 30})
        self.assertEqual(pf.decision, 'RULE_REJECTED')
        self.assertEqual(pf.matched_rule, 'PARAM_SOURCE_MISSING')


# ---------------------------------------------------------------------------
# 6) Drainer 消费链路（invoke_handler → 记账 → on_complete）
# ---------------------------------------------------------------------------

class TestDrainerConsumeChain(unittest.TestCase):

    def test_invoke_handler_flow_records_cost_and_calls_complete(self):
        """非高峰窗口：消费一项 → invoke_handler 被调 → 费用入台账 → on_complete 收到 business。"""
        router = make_router(api_enabled=True, api_dry_run=True,
                             fixed_now=datetime(2026, 8, 28, 20, 0, 0))
        self.assertTrue(router.can_call())  # 前置：非高峰窗口允许
        pqueue = PersistentQueue(MemoryQueueBackend())
        completed: List[Dict] = []
        invoke_calls: List[Dict] = []

        def fake_invoke(task: Dict):
            invoke_calls.append(task)
            resp = {'usage': {'prompt_tokens': 100, 'completion_tokens': 50},
                    'choices': [{'message': {'content': 'ok'}}]}
            return resp, {'UUID': task.get('original_uuid'), 'EVENT_TEXT': 'x'}

        drainer = OffPeakQueueDrainer(
            router=router, mock_client=MockDeepSeekClient(),
            persistent_queue=pqueue,
            invoke_handler=fake_invoke,
            on_complete=lambda item_id, result: completed.append(result))

        self.assertTrue(drainer.enqueue({
            'content_hash': 'hash-1', 'original_uuid': 'u-1',
            'content': 'c' * 20, 'is_priority': False}))

        item = pqueue.claim(worker_id=drainer.worker_id)
        self.assertIsNotNone(item)
        result = drainer._process_item(item)
        pqueue.complete(item['item_id'], drainer.worker_id, result=result)
        drainer.on_complete(item['item_id'], result)

        self.assertEqual(len(invoke_calls), 1)
        self.assertGreater(router.ledger.total_cost(), 0)  # 费用已按 usage 记账
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]['business']['UUID'], 'u-1')
        self.assertEqual(pqueue.count_pending(), 0)

    def test_drainer_blocked_at_peak_window(self):
        """高峰时段：router.can_call() 为 False，Drainer 不会消费（窗口判定分支）。"""
        router = make_router(api_enabled=True, api_dry_run=True,
                             fixed_now=datetime(2026, 8, 28, 10, 0, 0))
        self.assertFalse(router.can_call())
        pqueue = PersistentQueue(MemoryQueueBackend())
        drainer = OffPeakQueueDrainer(router=router, mock_client=MockDeepSeekClient(),
                                      persistent_queue=pqueue)
        self.assertTrue(drainer.enqueue({'content_hash': 'h', 'content': 'x'}))
        self.assertEqual(drainer.queue_size(), 1)
        self.assertFalse(router.can_call())


if __name__ == '__main__':
    unittest.main()

