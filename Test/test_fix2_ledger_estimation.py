# -*- coding: utf-8 -*-
"""批次二修复测试：费用预估、重试和持久化记账。

覆盖（暴露原缺陷，非仅辅助函数）：
1. 生产形态 payload 的正文不能被当作空字符串估算（预估覆盖 prompt+正文）；
2. 预估输出上限与实际请求使用的上限一致（8192 而非 2048）；
3. 日预算不足以覆盖实际请求上界时，网络调用次数为零；
4. 每分钟限额 1 次，首次超时后不能在同一分钟继续发起重试；
5. 每日限额 1 次，允许第一次合法请求，阻止第二次；
6. 首次超时、后续重试被时段限制阻止，已发出的尝试和占用仍保留；
7. 重启前未知费用占用 0.4 元，重启后不能恢复成 0 元；
8. 台账读取失败不等于空台账，写入失败不等于重复记录；
9. 宿主机 UTC 时，UTC 9/30 16:30 归入北京 10/1 的日账与月账；
10. 缺失用量数据不补零记 OK（MISSING_USAGE）；
11. 已取得的调用名额不能在发送前被当作额度耗尽拒绝。
"""
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ServiceComponent.ApiTimePolicy import ApiTimePolicy  # noqa: E402
from ServiceComponent.ApiUsageLedger import (  # noqa: E402
    STATUS_IN_FLIGHT,
    STATUS_MISSING_USAGE,
    STATUS_OK,
    STATUS_TIMEOUT_COST_UNKNOWN,
    ApiUsageLedger,
    LedgerStoreError,
    MemoryLedgerStore,
    MongoLedgerStore,
)
from ServiceComponent.BudgetGuard import BudgetGuard  # noqa: E402
from ServiceComponent.DeepSeekClient import DeepSeekClient  # noqa: E402
from ServiceComponent.DeepSeekModelRouter import (  # noqa: E402
    DeepSeekModelRouter,
    estimate_tokens_for_messages,
)
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient  # noqa: E402
from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer  # noqa: E402
from ServiceComponent.PersistentQueue import MemoryQueueBackend, PersistentQueue  # noqa: E402
from ServiceComponent.PricingPolicyRegistry import (  # noqa: E402
    ModelPricing,
    PricingPolicyRegistry,
)


def _sh_fixed():
    """非高峰北京时间 20:00（naive，按计费时区解释）。"""
    return datetime(2026, 9, 20, 20, 0, 0)


class _FakeClockPolicy(ApiTimePolicy):
    """时间可控的峰谷策略（allowed/is_peak 可外部切换，不依赖真实时钟）。"""

    def __init__(self, allowed=True, peak=False):
        super().__init__()
        self.allowed_flag = allowed
        self.peak_flag = peak

    def allowed_to_start_request(self, at=None):
        return self.allowed_flag

    def is_peak(self, at=None):
        return self.peak_flag


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
    # 默认使用可控时钟（非高峰、允许调用），避免真实时钟落在高峰时测试不稳定
    return DeepSeekModelRouter(
        time_policy or _FakeClockPolicy(allowed=True, peak=False),
        pricing,
        budget or BudgetGuard(daily_budget_cny=100.0, now_fn=_sh_fixed),
        ledger or ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed),
        api_enabled=api_enabled, api_dry_run=api_dry_run)


def _fake_httpx_module(calls, behavior):
    """构造假 httpx 模块：behavior(first_call_exception, later_calls)。"""
    import types
    module = types.ModuleType('httpx')

    class _Resp:
        status_code = 200

        def json(self):
            return {'id': 'x', 'model': 'flash',
                    'choices': [{'message': {'content': '{}'}}],
                    'usage': {'prompt_tokens': 50, 'completion_tokens': 20,
                              'total_tokens': 70}}

    def post(*a, **kw):
        calls['n'] += 1
        if calls['n'] == 1 and behavior.get('first_error'):
            raise behavior['first_error']
        if behavior.get('always_error'):
            raise behavior['always_error']
        return _Resp()

    module.post = post
    return module


def _install_httpx(module):
    prev = sys.modules.get('httpx')
    sys.modules['httpx'] = module
    return prev


def _uninstall_httpx(prev):
    if prev is None:
        sys.modules.pop('httpx', None)
    else:
        sys.modules['httpx'] = prev


def _make_drainer_with_real_client(router, rate_max=100, queue=None):
    """构造带真实客户端（假 httpx）的 drainer。"""
    client = DeepSeekClient(api_key='k', max_retries=3)
    client._backoff = lambda attempt: None  # 测试不等待退避
    d = OffPeakQueueDrainer(
        router=router, mock_client=MockDeepSeekClient(),
        drain_interval_seconds=0, max_per_minute=rate_max,
        persistent_queue=queue or PersistentQueue(MemoryQueueBackend()),
        real_client=client)
    return d, client


class TestEstimationFromRealMessages(unittest.TestCase):
    """验收 1/2：预估覆盖 prompt+正文；输出上限与实际一致。"""

    def test_payload_body_not_estimated_as_empty(self):
        """原缺陷暴露：生产形态 task 无 'content' 键（正文在
        original_data.content），旧 estimate_cost(model, '') 把输入当 0。"""
        router = _make_router()
        task = {
            'content_hash': 'h1', 'original_uuid': 'u1',
            'original_data': {
                'UUID': 'u1', 'title': 'US China trade talk',
                'content': '正文内容' * 500,  # 长正文
                'informant': 'https://example.com/a',
            },
            'source_id': 's1',
        }
        # 旧路径：按 task['content']（不存在 → ''）估算 → 输入费用为 0
        old_estimate = router.estimate_cost('flash', task.get('content', ''), 2048)
        self.assertEqual(
            old_estimate, router.estimate_cost('flash', '', 2048),
            '缺陷前提：旧估算确实把正文当空字符串')

        # 新路径：从实际发送消息估算（含 V25 prompt 与正文）
        from ServiceComponent.IntelligenceAnalyzerProxy import (
            MAX_OUTPUT_TOKEN,
            build_analyze_message,
        )
        from prompts_v2x import ANALYSIS_PROMPT_TABLE
        prompt = ANALYSIS_PROMPT_TABLE[next(iter(ANALYSIS_PROMPT_TABLE))]
        messages = build_analyze_message(prompt, task['original_data'])
        new_estimate = router.estimate_cost_for_messages(
            'flash', messages, MAX_OUTPUT_TOKEN)

        self.assertGreater(new_estimate, old_estimate * 5,
                           '预估必须显著高于按空字符串的旧估算')
        body_tokens = estimate_tokens_for_messages(
            [{'role': 'user', 'content': task['original_data']['content']}])
        total_tokens = estimate_tokens_for_messages(messages)
        self.assertGreater(total_tokens, body_tokens,
                           '总输入 token 必须覆盖正文（还有 prompt）')

    def test_output_cap_matches_actual_request(self):
        """预估输出上限必须与实际请求使用的上限一致（8192）。"""
        from ServiceComponent.IntelligenceAnalyzerProxy import MAX_OUTPUT_TOKEN
        self.assertEqual(MAX_OUTPUT_TOKEN, 8192)
        router = _make_router()
        messages = [{'role': 'user', 'content': 'x' * 100}]
        est = router.estimate_cost_for_messages('flash', messages, MAX_OUTPUT_TOKEN)
        # 输入：100/3.5 token + 16 开销；输出：8192 token 全额
        expected_input = (round(100 / 3.5 + 0.5) + 16) / 1000 * 0.001
        expected_output = 8192 / 1000 * 0.002
        self.assertAlmostEqual(est, expected_input + expected_output, places=6)

    def test_drainer_uses_estimate_messages_fn(self):
        """drainer 注入 estimate_messages_fn 后按消息估算（而非 content）。"""
        router = _make_router(api_dry_run=True)
        big_messages = [{'role': 'user', 'content': '内' * 4000}]
        d = OffPeakQueueDrainer(
            router=router, mock_client=MockDeepSeekClient(),
            max_per_minute=100,
            estimate_messages_fn=lambda task: (big_messages, 1024))
        q = PersistentQueue(MemoryQueueBackend())
        d.queue = q
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': 'short'})
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        self.assertEqual(result['model'], 'flash')
        self.assertGreaterEqual(router.budget.daily_calls, 1)


class TestBudgetBlocksBeforeSend(unittest.TestCase):
    """验收 3：日预算不足以覆盖实际请求上界 → 网络调用次数为零。"""

    def test_insufficient_budget_zero_calls(self):
        calls = {'n': 0}
        httpx_mod = _fake_httpx_module(calls, {})
        budget = BudgetGuard(daily_budget_cny=0.01, now_fn=_sh_fixed)
        router = _make_router(budget=budget)
        d, _ = _make_drainer_with_real_client(router)
        q = d.queue
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': '正文' * 2000})
        item = q.claim(worker_id=d.worker_id)
        prev = _install_httpx(httpx_mod)
        try:
            with self.assertRaises(Exception):
                d._process_item(item)
        finally:
            _uninstall_httpx(prev)
        self.assertEqual(calls['n'], 0, '预算不足以覆盖上界时网络调用必须为 0')

    def test_daily_limit_1_allows_first_blocks_second(self):
        """验收 5：每日限额 1 次——第一次合法，第二次被阻止。"""
        calls = {'n': 0}
        httpx_mod = _fake_httpx_module(calls, {})
        budget = BudgetGuard(daily_budget_cny=100.0, daily_call_limit=1,
                             now_fn=_sh_fixed)
        router = _make_router(budget=budget)
        d, _ = _make_drainer_with_real_client(router)
        q = d.queue
        prev = _install_httpx(httpx_mod)
        try:
            q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                             'content': 'abc'})
            item1 = q.claim(worker_id=d.worker_id)
            result = d._process_item(item1)
            self.assertEqual(result['model'], 'flash')
            self.assertEqual(calls['n'], 1, '第一次合法请求必须放行')

            q.enqueue('h2', {'content_hash': 'h2', 'original_uuid': 'u2',
                             'content': 'abc'})
            item2 = q.claim(worker_id=d.worker_id)
            with self.assertRaises(Exception) as ctx:
                d._process_item(item2)
            self.assertIn('预算', str(ctx.exception))
            self.assertEqual(calls['n'], 1, '每日限额用尽后第二次必须被阻止')
        finally:
            _uninstall_httpx(prev)


class TestRetryRateLimit(unittest.TestCase):
    """验收 4：每分钟限额 1 次，首次超时后不能在同一分钟继续重试。"""

    def test_no_retry_in_same_minute_after_timeout(self):
        calls = {'n': 0}
        httpx_mod = _fake_httpx_module(
            calls, {'first_error': RuntimeError('request timed out')})
        router = _make_router()
        d, _ = _make_drainer_with_real_client(router, rate_max=1)
        q = d.queue
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': 'abc'})
        item = q.claim(worker_id=d.worker_id)
        prev = _install_httpx(httpx_mod)
        try:
            with self.assertRaises(Exception):
                d._process_item(item)
        finally:
            _uninstall_httpx(prev)
        self.assertEqual(calls['n'], 1,
                         '首次超时计入配额后，同一分钟内不得发起重试')
        statuses = sorted(r['status'] for r in router.ledger.records(10))
        self.assertIn(STATUS_TIMEOUT_COST_UNKNOWN, statuses)

    def test_retry_allowed_next_minute(self):
        """窗口滑过后允许重试（限流窗口按真实时间滑动）。"""
        now = {'t': 0.0}
        from ServiceComponent.OffPeakQueueDrainer import StartRateLimiter
        limiter = StartRateLimiter(1, now_fn=lambda: now['t'])
        self.assertTrue(limiter.allow())
        limiter.record_start()
        self.assertFalse(limiter.allow(), '同一分钟内第二次被拒')
        now['t'] = 61.0
        self.assertTrue(limiter.allow(), '窗口滑过后恢复')


class TestTimeoutThenWindowClose(unittest.TestCase):
    """验收 6：首次超时、后续重试被时段阻止，已发出的尝试和占用保留。"""

    def test_reservation_kept_when_retry_blocked_after_send(self):
        calls = {'n': 0}
        httpx_mod = _fake_httpx_module(
            calls, {'first_error': RuntimeError('read timeout')})
        policy = _FakeClockPolicy(allowed=True)
        budget = BudgetGuard(daily_budget_cny=100.0, now_fn=_sh_fixed)
        ledger = ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed)
        router = _make_router(budget=budget, ledger=ledger, time_policy=policy)
        d, _ = _make_drainer_with_real_client(router, rate_max=100)
        q = d.queue
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': 'abc'})
        item = q.claim(worker_id=d.worker_id)

        # 首次尝试发出后进入高峰（时段关闭）→ 客户端重试前被护栏阻止。
        # 注意：置位必须在 original_post 调用之前——第一次尝试的 guard
        # 已在 post 前执行完毕，此处设置只影响第二次（重试）尝试。
        original_post = httpx_mod.post

        def _post_then_close(*a, **kw):
            policy.allowed_flag = False  # 请求即将发出，之后进入高峰
            return original_post(*a, **kw)

        httpx_mod.post = _post_then_close
        prev = _install_httpx(httpx_mod)
        try:
            with self.assertRaises(Exception) as ctx:
                d._process_item(item)
            self.assertTrue(getattr(ctx.exception, 'sent_before_block', False),
                            '阻止时必须标记已有尝试发出')
        finally:
            _uninstall_httpx(prev)

        self.assertEqual(calls['n'], 1, '已发出的尝试计入')
        self.assertGreater(budget.daily_cost, 0.0,
                           '已发送但结果未知时占用必须保留（不得退款为 0）')
        recs = ledger.records(10)
        inflight = [r for r in recs if r['status'] == STATUS_IN_FLIGHT]
        self.assertEqual(len(inflight), 1, 'IN_FLIGHT 记录必须保留')
        self.assertGreater(inflight[0]['cost_cny'], 0.0)

    def test_cancelled_before_send_gets_refund(self):
        """对照：从未发出请求 → 退款 + CANCELLED 终态。"""
        policy = _FakeClockPolicy(allowed=False)  # 首次尝试前即被阻止
        budget = BudgetGuard(daily_budget_cny=100.0, now_fn=_sh_fixed)
        ledger = ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed)
        router = _make_router(budget=budget, ledger=ledger, time_policy=policy)
        d, _ = _make_drainer_with_real_client(router, rate_max=100)
        q = d.queue
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': 'abc'})
        item = q.claim(worker_id=d.worker_id)
        with self.assertRaises(Exception) as ctx:
            d._process_item(item)
        self.assertFalse(getattr(ctx.exception, 'sent_before_block', True))
        self.assertEqual(budget.daily_cost, 0.0, '未发送必须退款')
        recs = ledger.records(10)
        cancelled = [r for r in recs if r['status'] == 'CANCELLED']
        self.assertEqual(len(cancelled), 1)


class TestRestartRestore(unittest.TestCase):
    """验收 7：重启前未知费用占用 0.4 元，重启后不能恢复成 0 元。"""

    def test_inflight_restored_as_reserved_cost(self):
        ledger = ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed)
        # 模拟：请求发出后崩溃——IN_FLIGHT 记录保留预估占用 0.4 元
        ledger.record('t1#0#abc', 'flash', 0, 0, 0.4, False,
                      task_id='t1', status=STATUS_IN_FLIGHT)
        # "重启"：新 BudgetGuard 从台账恢复
        budget = BudgetGuard(daily_budget_cny=1.0, now_fn=_sh_fixed)
        budget.restore_from_ledger(ledger)
        self.assertGreaterEqual(budget.daily_cost, 0.4,
                                '未知费用占用不得恢复成 0 元')
        self.assertEqual(budget.daily_calls, 1, '调用次数一并恢复')

    def test_settled_records_restored_normally(self):
        ledger = ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed)
        ledger.record('a1', 'flash', 100, 50, 0.123, False)
        budget = BudgetGuard(daily_budget_cny=1.0, now_fn=_sh_fixed)
        budget.restore_from_ledger(ledger)
        self.assertAlmostEqual(budget.daily_cost, 0.123, places=6)


class _FailingCollection:
    """模拟连接故障的 Mongo collection。"""

    def insert_one(self, doc):
        raise ConnectionError('mongo unreachable')

    def update_one(self, q, u):
        raise ConnectionError('mongo unreachable')

    def find(self, q):
        raise ConnectionError('mongo unreachable')


class _DupKeyError(Exception):
    code = 11000


class _DupKeyCollection:
    """总抛重复主键错误的 collection。"""

    def __init__(self):
        self.docs = {}

    def insert_one(self, doc):
        if doc['_id'] in self.docs:
            raise _DupKeyError('E11000 duplicate key error')
        self.docs[doc['_id']] = dict(doc)

    def update_one(self, q, u):
        return type('R', (), {'modified_count': 1})()

    def find(self, q):
        return [dict(d) for d in self.docs.values()]


class TestLedgerStoreFaults(unittest.TestCase):
    """验收 8：读取失败 ≠ 空台账；写入失败 ≠ 重复记录。"""

    def test_write_failure_raises_not_false(self):
        store = MongoLedgerStore(_FailingCollection())
        with self.assertRaises(LedgerStoreError):
            store.insert_if_absent({'_id': 'a1'})
        # 写入失败不得被当作"幂等命中"（原缺陷：捕获一切异常返回 False）

    def test_read_failure_raises_not_empty(self):
        store = MongoLedgerStore(_FailingCollection())
        with self.assertRaises(LedgerStoreError):
            store.list_all()
        # 读取失败不得返回空列表冒充空台账（原缺陷）

    def test_duplicate_key_still_false(self):
        store = MongoLedgerStore(_DupKeyCollection())
        self.assertTrue(store.insert_if_absent({'_id': 'a1'}))
        self.assertFalse(store.insert_if_absent({'_id': 'a1'}),
                         '只有重复主键可按已存在处理')

    def test_ledger_degraded_and_recovery(self):
        """degraded：暂停新增付费请求；恢复后自动解除。"""

        class _Switchable:
            def __init__(self):
                self.fail = False
                self.docs = {}

            def insert_one(self, doc):
                if self.fail:
                    raise ConnectionError('down')
                if doc['_id'] in self.docs:
                    raise _DupKeyError('dup')
                self.docs[doc['_id']] = dict(doc)
                return True

            def find(self, q):
                if self.fail:
                    raise ConnectionError('down')
                return [dict(d) for d in self.docs.values()]

            def update_one(self, q, u):
                return type('R', (), {'modified_count': 1})()

        col = _Switchable()
        ledger = ApiUsageLedger(store=MongoLedgerStore(col), now_fn=_sh_fixed)
        self.assertTrue(ledger.record('a1', 'flash', 1, 1, 0.01, False))
        col.fail = True
        self.assertFalse(ledger.record('a2', 'flash', 1, 1, 0.01, False))
        self.assertTrue(ledger.is_degraded(), '写入故障必须进入降级')
        self.assertFalse(ledger.count() == 0 and not ledger.is_degraded(),
                         '读取失败不等于空台账')
        router = _make_router(ledger=ledger)
        self.assertFalse(router.can_call(), '台账降级期间暂停新增付费请求')
        self.assertEqual(router.block_reason(), 'ledger_degraded')
        col.fail = False
        self.assertTrue(ledger.record('a3', 'flash', 1, 1, 0.01, False))
        self.assertFalse(ledger.is_degraded(), '恢复写入后自动解除降级')


class TestBillingTimezone(unittest.TestCase):
    """验收 9：宿主机 UTC 时，UTC 9/30 16:30 → 北京 10/1 日账/月账。"""

    def test_utc_1630_counts_as_beijing_oct1(self):
        utc = timezone.utc
        ledger = ApiUsageLedger(
            store=MemoryLedgerStore(),
            now_fn=lambda: datetime(2026, 9, 30, 16, 30, 0, tzinfo=utc))
        self.assertTrue(ledger.record('a1', 'flash', 1, 1, 0.01, False))
        day = ledger.daily_summary()
        mon = ledger.monthly_summary()
        self.assertEqual(day['date'], '2026-10-01',
                         'UTC 16:30 = 北京 10/1 00:30，日账归入 10/1')
        self.assertEqual(day['calls'], 1)
        self.assertEqual(mon['month'], '2026-10',
                         '月账归入北京 2026-10（跨月）')
        import datetime as _dt
        sep = ledger.daily_summary(day=_dt.date(2026, 9, 30))
        self.assertEqual(sep['calls'], 0, '北京 9 月的账不含这条')

    def test_naive_treated_as_billing_tz(self):
        """naive now_fn 按计费时区解释（9/30 16:30 北京 → 9/30 日账）。"""
        ledger = ApiUsageLedger(
            store=MemoryLedgerStore(),
            now_fn=lambda: datetime(2026, 9, 30, 16, 30, 0))
        ledger.record('a1', 'flash', 1, 1, 0.01, False)
        self.assertEqual(ledger.daily_summary()['date'], '2026-09-30')
        self.assertEqual(ledger.monthly_summary()['month'], '2026-09')


class TestMissingUsage(unittest.TestCase):
    """验收 10：缺失用量不补零记 OK。"""

    def test_missing_usage_status(self):
        import types
        httpx_mod = types.ModuleType('httpx')

        class _Resp:
            status_code = 200

            def json(self):
                # 响应成功但无 usage 字段
                return {'id': 'x', 'model': 'flash',
                        'choices': [{'message': {'content': '{}'}}]}

        calls = {'n': 0}

        def post(*a, **kw):
            calls['n'] += 1
            return _Resp()

        httpx_mod.post = post
        budget = BudgetGuard(daily_budget_cny=100.0, now_fn=_sh_fixed)
        ledger = ApiUsageLedger(store=MemoryLedgerStore(), now_fn=_sh_fixed)
        router = _make_router(budget=budget, ledger=ledger)
        d, _ = _make_drainer_with_real_client(router)
        q = d.queue
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': 'abc'})
        item = q.claim(worker_id=d.worker_id)
        prev = _install_httpx(httpx_mod)
        try:
            result = d._process_item(item)
        finally:
            _uninstall_httpx(prev)
        self.assertEqual(calls['n'], 1)
        self.assertIsNone(result['cost_cny'], '费用未知不补零')
        recs = ledger.records(10)
        self.assertIn(STATUS_MISSING_USAGE, [r['status'] for r in recs],
                      '缺失用量必须记 MISSING_USAGE，不得记 OK')
        self.assertGreater(budget.daily_cost, 0.0,
                           '预算按最坏情况保留预估')


class TestQuotaNotDoubleRejected(unittest.TestCase):
    """验收 11：已取得的调用名额不能在发送前再次被当作额度耗尽拒绝。"""

    def test_guard_allows_send_with_active_reserve(self):
        """预算恰好被预留推到边界时，发送前护栏不得误拒。"""
        router = _make_router()
        # 边界场景：预算恰好等于预估上界——try_reserve 成功（daily_cost==budget），
        # 常规 can_call 为假（名额已占），但护栏必须放行已取得的名额。
        estimate = router.estimate_cost('flash', 'x' * 100, 2048)
        budget = BudgetGuard(daily_budget_cny=estimate, now_fn=_sh_fixed)
        router.budget = budget
        self.assertTrue(budget.try_reserve(estimate))
        self.assertFalse(budget.can_call(),
                         '预留后常规预算判定为假（名额已占）')
        self.assertTrue(budget.has_active_reserve(), '活跃预留存在')
        # 逐尝试护栏：有活跃预留时放行（原缺陷会误拒已取得的名额）
        guard = router.make_attempt_guard()
        guard()  # 不抛异常即通过
        budget.cancel_reserve(estimate)

    def test_end_to_end_boundary_budget_still_sends(self):
        """端到端：预算恰好够一次调用 → 请求正常发出（不被自身预留误拒）。"""
        calls = {'n': 0}
        httpx_mod = _fake_httpx_module(calls, {})
        router = _make_router()
        estimate = router.estimate_cost('flash', 'abc', 2048)
        # 预算恰好等于预估上界
        budget = BudgetGuard(daily_budget_cny=estimate, now_fn=_sh_fixed)
        router.budget = budget
        d, _ = _make_drainer_with_real_client(router)
        q = d.queue
        q.enqueue('h1', {'content_hash': 'h1', 'original_uuid': 'u1',
                         'content': 'abc'})
        item = q.claim(worker_id=d.worker_id)
        prev = _install_httpx(httpx_mod)
        try:
            result = d._process_item(item)
            self.assertEqual(calls['n'], 1,
                             '边界预算下已取得名额的请求必须发出（原缺陷会被误拒）')
            self.assertEqual(result['model'], 'flash')
        finally:
            _uninstall_httpx(prev)


if __name__ == '__main__':
    unittest.main()
