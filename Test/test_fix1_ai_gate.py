# -*- coding: utf-8 -*-
"""批次一修复测试：统一人工智能调用控制。

覆盖（对应原缺陷，非仅辅助函数）：
1. 旧、新消费者都关闭时，即使总调用开关开启也不启动任何分析消费者
   （原缺陷：startup() 漏检 use_legacy_ai_worker，双关时旧线程仍启动）；
2. 总开关关闭或模拟模式开启时，后台翻译线程根本不创建，
   且带 call_gate 的翻译路径在护栏阻止时真实网络调用次数为 0；
3. 翻译重试同样受护栏约束（gate 阻止即放弃，不计失败、不打死信）；
4. 旧分析路径调用成功后必须记录消费（台账有记录，不再"只查预算不记账"）；
5. 布尔开关严格解析（字符串 "false" 不得解析为 True）。
"""
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ServiceComponent.ApiTimePolicy import ApiTimePolicy  # noqa: E402
from ServiceComponent.ApiUsageLedger import (  # noqa: E402
    STATUS_UNKNOWN_PRICE,
    ApiUsageLedger,
    MemoryLedgerStore,
)
from ServiceComponent.BudgetGuard import BudgetGuard  # noqa: E402
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter  # noqa: E402
from ServiceComponent.PricingPolicyRegistry import (  # noqa: E402
    ModelPricing,
    PricingPolicyRegistry,
)

try:
    from ServiceComponent.AsyncTranslationPatch import AsyncTranslationPatch
    TRANSLATION_IMPORTABLE = True
except Exception:
    TRANSLATION_IMPORTABLE = False

try:
    from IntelligenceHub import IntelligenceHub
    HUB_IMPORTABLE = True
except Exception:
    HUB_IMPORTABLE = False


def _sh_fixed():
    """非高峰北京时间 20:00（naive，按计费时区解释）。"""
    return datetime(2026, 9, 20, 20, 0, 0)


def _make_router(api_enabled=True, api_dry_run=False, budget=None, ledger=None,
                 time_policy=None):
    pricing = PricingPolicyRegistry(
        flash_model="flash", pro_model="pro",
        flash_pricing=ModelPricing(model_name="flash", is_flash=True,
                                   input_price_cny_per_1k=0.001,
                                   output_price_cny_per_1k=0.002),
        pro_pricing=ModelPricing(model_name="pro", is_flash=False,
                                 input_price_cny_per_1k=0.003,
                                 output_price_cny_per_1k=0.006))
    return DeepSeekModelRouter(
        time_policy or ApiTimePolicy(),
        pricing,
        budget or BudgetGuard(daily_budget_cny=10.0, now_fn=_sh_fixed),
        ledger or ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed),
        api_enabled=api_enabled, api_dry_run=api_dry_run)


def _minimal_hub(**overrides):
    """绕过 __init__ 构造最小 Hub（startup/consumer_mode 决策测试）。"""
    hub = IntelligenceHub.__new__(IntelligenceHub)
    hub.api_enabled = overrides.get('api_enabled', False)
    hub.api_dry_run = overrides.get('api_dry_run', True)
    hub.use_legacy_ai_worker = overrides.get('use_legacy_ai_worker', False)
    hub.use_off_peak_drainer = overrides.get('use_off_peak_drainer', False)
    hub.off_peak_drainer = overrides.get('off_peak_drainer', None)
    hub.disk_guard = None
    hub.shutdown_flag = threading.Event()
    hub.post_process_thread = MagicMock()
    hub.intelligence_vectorization_thread = MagicMock()
    hub._analysis_threads = []
    started = {'n': 0}

    def _fake_start(n):
        started['n'] += n
        hub._analysis_threads.extend([object()] * n)

    hub.start_analysis_threads = _fake_start
    return hub, started


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestNoConsumerNoAnalysisThread(unittest.TestCase):
    """验收：两个消费者都关闭，即使总调用开关开启，也不启动分析线程。"""

    def test_both_consumers_off_api_on_no_thread(self):
        """原缺陷暴露：api_enabled=true + api_dry_run=false + 双消费者关闭，
        旧实现 start_legacy_worker=True 会启动分析线程。"""
        hub, started = _minimal_hub(api_enabled=True, api_dry_run=False,
                                    use_legacy_ai_worker=False,
                                    use_off_peak_drainer=False)
        hub.startup(3)
        self.assertEqual(started['n'], 0, "双消费者关闭时不得启动任何分析线程")
        self.assertEqual(hub._analysis_threads, [])
        self.assertEqual(hub.consumer_mode(), 'collect_only')

    def test_legacy_switch_off_blocks_legacy_worker(self):
        """仅旧消费者开关关闭（Drainer 也关）→ collect_only。"""
        hub, started = _minimal_hub(api_enabled=True, api_dry_run=False,
                                    use_legacy_ai_worker=False)
        hub.startup(2)
        self.assertEqual(started['n'], 0)

    def test_legacy_switch_on_still_starts(self):
        """旧消费者开启 + 总开关开 + 非模拟 → 正常启动（不误伤合法配置）。"""
        hub, started = _minimal_hub(api_enabled=True, api_dry_run=False,
                                    use_legacy_ai_worker=True)
        hub.startup(2)
        self.assertEqual(started['n'], 2)
        self.assertEqual(hub.consumer_mode(), 'legacy_ai_worker')

    def test_dry_run_blocks_legacy_worker(self):
        """模拟模式开启 → 不启动分析线程。"""
        hub, started = _minimal_hub(api_enabled=True, api_dry_run=True,
                                    use_legacy_ai_worker=True)
        hub.startup(2)
        self.assertEqual(started['n'], 0)
        self.assertEqual(hub.consumer_mode(), 'dry_run')

    def test_api_disabled_blocks_legacy_worker(self):
        """总开关关闭 → 不启动分析线程。"""
        hub, started = _minimal_hub(api_enabled=False, api_dry_run=False,
                                    use_legacy_ai_worker=True)
        hub.startup(2)
        self.assertEqual(started['n'], 0)
        self.assertEqual(hub.consumer_mode(), 'collect_only')


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入（环境缺依赖）")
class TestTranslationGate(unittest.TestCase):
    """验收：翻译护栏逐项生效（总开关/模拟/时段/预算）。"""

    def _hub_for_gate(self):
        hub = IntelligenceHub.__new__(IntelligenceHub)
        hub.api_enabled = True
        hub.api_dry_run = False
        hub.time_policy = None
        hub.budget_guard = None
        return hub

    def test_gate_blocks_when_api_disabled(self):
        hub = self._hub_for_gate()
        hub.api_enabled = False
        with self.assertRaises(RuntimeError):
            hub._translation_call_gate()

    def test_gate_blocks_when_dry_run(self):
        hub = self._hub_for_gate()
        hub.api_dry_run = True
        with self.assertRaises(RuntimeError):
            hub._translation_call_gate()

    def test_gate_blocks_at_peak_window(self):
        hub = self._hub_for_gate()
        policy = ApiTimePolicy()
        policy.now = lambda: datetime(2026, 9, 20, 10, 0, 0,
                                      tzinfo=policy.timezone)
        hub.time_policy = policy
        with self.assertRaises(RuntimeError):
            hub._translation_call_gate()

    def test_gate_blocks_when_budget_exhausted(self):
        hub = self._hub_for_gate()
        hub.budget_guard = BudgetGuard(daily_budget_cny=0.01, now_fn=_sh_fixed)
        hub.budget_guard.record(1.0)
        with self.assertRaises(RuntimeError):
            hub._translation_call_gate()

    def test_gate_allows_when_all_pass(self):
        hub = self._hub_for_gate()
        hub.budget_guard = BudgetGuard(daily_budget_cny=10.0, now_fn=_sh_fixed)
        policy = ApiTimePolicy()
        policy.now = lambda: datetime(2026, 9, 20, 20, 0, 0,
                                      tzinfo=policy.timezone)
        hub.time_policy = policy
        hub._translation_call_gate()  # 不抛即通过


@unittest.skipUnless(TRANSLATION_IMPORTABLE, "翻译模块不可导入")
class TestTranslationRealCallBlocked(unittest.TestCase):
    """验收：有可用翻译客户端和待翻译数据时，护栏阻止 → 真实调用次数为 0。"""

    def _make_patch(self, gate, recorder=None):
        return AsyncTranslationPatch(
            mongo_db_archive=MagicMock(), query_engine=MagicMock(),
            ai_client_manager=MagicMock(), shutdown_flag=threading.Event(),
            backfill_enabled=False, call_gate=gate, ledger_recorder=recorder)

    def _fake_client(self, calls):
        class _Client:
            name = 'translate-client'

            def validate_response(self, resp):
                return None

            def complain_error(self, reason):
                pass

            def chat(self, **kwargs):
                calls['n'] += 1
                return {'choices': [{'message': {'content': '{}'}}], 'usage': {}}

        return _Client()

    def test_gate_blocks_call_and_retry(self):
        """护栏阻止时：不调用、返回 None（原缺陷：完全绕过护栏直接调用）。"""
        calls = {'n': 0}

        def gate():
            raise RuntimeError('api_dry_run=true')

        patch = self._make_patch(gate=gate)
        patch.ai_client_manager.get_available_client = lambda **kw: self._fake_client(calls)
        doc = {'UUID': 'u1', 'EVENT_TITLE': 'China trade policy update',
               'EVENT_BRIEF': 'b', 'EVENT_TEXT': 'text'}
        result = patch._translate_via_ai(doc)
        self.assertIsNone(result, "被护栏阻止应返回 None（区别于翻译失败）")
        self.assertEqual(calls['n'], 0, "护栏阻止时真实调用次数必须为 0")

    def test_gate_blocks_by_peak_window(self):
        """高峰时段 → 翻译被阻止，调用次数为 0（原缺陷：翻译不看时段）。"""
        calls = {'n': 0}

        def peak_gate():
            policy = ApiTimePolicy()
            policy.now = lambda: datetime(2026, 9, 20, 10, 0, 0,
                                          tzinfo=policy.timezone)
            if not policy.allowed_to_start_request():
                raise RuntimeError('高峰窗口')

        patch = self._make_patch(gate=peak_gate)
        patch.ai_client_manager.get_available_client = lambda **kw: self._fake_client(calls)
        doc = {'UUID': 'u2', 'EVENT_TITLE': 'Russia China summit', 'EVENT_TEXT': 't'}
        self.assertIsNone(patch._translate_via_ai(doc))
        self.assertEqual(calls['n'], 0)

    def test_gated_task_not_counted_as_failure(self):
        """被护栏阻止的任务不打失败标记/死信（保留待下一轮 backfill）。"""
        patch = self._make_patch(gate=lambda: (_ for _ in ()).throw(RuntimeError('blocked')))
        from ServiceComponent.AsyncTranslationPatch import TranslationTask
        task = TranslationTask(uuid='u3', priority=0, reason='test')
        patch.query_engine.get_intelligence = lambda u, light_weight=False: {
            'UUID': 'u3', 'EVENT_TITLE': 'US China tariff news', 'EVENT_TEXT': 'x'}
        patch._translate_via_ai = lambda doc: None  # 模拟被护栏阻止
        patch._process_task(task)
        patch.mongo_db_archive.update.assert_not_called()

    def test_ledger_recorder_called_after_real_call(self):
        """真实调用完成后记账被调用（实际调用进入同一费用台账）。"""
        calls = {'n': 0}
        recorded = []
        patch = self._make_patch(gate=lambda: None,
                                 recorder=lambda resp, u: recorded.append((resp, u)))

        class _Client:
            name = 'c'

            def validate_response(self, resp):
                return None

            def complain_error(self, reason):
                pass

            def chat(self, **kw):
                calls['n'] += 1
                return {'choices': [{'message': {
                    'content': '{"EVENT_TITLE": "中国经济相关标题内容", '
                               '"EVENT_BRIEF": "简短内容说明文字", '
                               '"EVENT_TEXT": "正文中文内容足够长度以通过健全性检查"}'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}

        client = _Client()
        patch.ai_client_manager.get_available_client = lambda **kw: client
        doc = {'UUID': 'u4', 'EVENT_TITLE': 'China economy grows', 'EVENT_TEXT': 'text'}
        patch._translate_via_ai(doc)
        self.assertEqual(calls['n'], 1)
        self.assertEqual(len(recorded), 1, "真实调用后必须记账")


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入")
class TestLegacyWorkerLedger(unittest.TestCase):
    """验收：旧分析路径不能只检查预算而不记录消费。"""

    def test_legacy_analysis_records_usage(self):
        ledger = ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed)
        budget = BudgetGuard(daily_budget_cny=10.0, daily_call_limit=100,
                             now_fn=_sh_fixed)
        hub = IntelligenceHub.__new__(IntelligenceHub)
        hub.api_dry_run = False
        hub.model_router = _make_router(ledger=ledger, budget=budget)
        hub.budget_guard = budget

        resp = {'model': 'legacy-model-x',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}
        hub._record_legacy_analysis_usage(resp, 'uuid-1', 'V25')

        self.assertEqual(ledger.count(), 1, "旧路径调用后台账必须有记录")
        rec = ledger.records(1)[0]
        self.assertEqual(rec['status'], STATUS_UNKNOWN_PRICE)
        self.assertEqual(rec['input_tokens'], 100)
        self.assertEqual(rec['output_tokens'], 50)
        self.assertIsNone(rec['cost_cny'], "价格未配置时费用记未知，不按 0 记正常完成")
        self.assertEqual(budget.daily_calls, 1, "调用次数必须计入预算守卫")

    def test_usage_capturing_client_captures(self):
        from IntelligenceHub import _UsageCapturingClient
        inner = MagicMock()
        inner.get_current_model.return_value = 'gpt-test'
        inner.chat.return_value = {'usage': {'prompt_tokens': 7}, 'model': 'gpt-test'}
        wrapper = _UsageCapturingClient(inner)
        resp = wrapper.chat(messages=[{'role': 'user', 'content': 'x'}])
        self.assertEqual(wrapper.last_response['usage']['prompt_tokens'], 7)
        self.assertEqual(wrapper.last_model, 'gpt-test')
        self.assertEqual(resp['usage']['prompt_tokens'], 7)


class TestStrictBoolParsing(unittest.TestCase):
    """布尔配置统一严格解析（字符串开关不得反向）。"""

    def test_string_false_not_truthy(self):
        from ServiceComponent.ConfigParsing import InvalidConfigValueError, parse_bool_or
        self.assertFalse(parse_bool_or('false', True, 'k'))
        self.assertFalse(parse_bool_or('0', True, 'k'))
        self.assertTrue(parse_bool_or('true', False, 'k'))
        with self.assertRaises(InvalidConfigValueError):
            parse_bool_or('flase', False, 'k')
        with self.assertRaises(InvalidConfigValueError):
            parse_bool_or(2, False, 'k')
        self.assertTrue(parse_bool_or(None, True, 'k'), "缺失用默认值")

    def test_consumer_mutex_strict_parse(self):
        from ServiceComponent.ConsumerMutex import (
            ConsumerMutexError,
            validate_startup_config,
        )
        switches = validate_startup_config({
            'api_enabled': 'true', 'api_dry_run': 'false',
            'use_legacy_ai_worker': 'false', 'use_off_peak_drainer': 'false'})
        self.assertTrue(switches['api_enabled'])
        self.assertFalse(switches['api_dry_run'])
        with self.assertRaises(ConsumerMutexError):
            validate_startup_config({
                'use_legacy_ai_worker': 'flase',  # 非法值 fail-fast
            })


if __name__ == '__main__':
    unittest.main()
