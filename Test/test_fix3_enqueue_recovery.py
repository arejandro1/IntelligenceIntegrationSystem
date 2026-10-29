# -*- coding: utf-8 -*-
"""批次三修复测试：入队与归档恢复。

覆盖（暴露原缺陷）：
1. 入队三态：新增成功 / 确定重复 / 存储故障（数据库连接错误不得
   返回"重复任务"或"提交成功"——原缺陷：bool 语义混淆）；
2. 缓存写入后崩溃 → 重启后补投（cache 查重不得永久阻止恢复）；
3. 分析结果保存后崩溃（complete 已写、未交付）→ 重放交付，不重复调 AI；
4. 内存入队后崩溃（DELIVERED 未 ACK）→ 对账重投，不重复调模型；
5. 归档写入后、最终确认前 → 对账确认 ACK，不重复投递；
6. complete/deliver 返回 False 时正确处理（原缺陷：忽略返回值）；
7. 正常归档与低价值信息保存路径的确认。
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ServiceComponent.ApiTimePolicy import ApiTimePolicy  # noqa: E402
from ServiceComponent.ApiUsageLedger import ApiUsageLedger  # noqa: E402
from ServiceComponent.BudgetGuard import BudgetGuard  # noqa: E402
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter  # noqa: E402
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient  # noqa: E402
from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer  # noqa: E402
from ServiceComponent.PersistentQueue import (  # noqa: E402
    ENQUEUE_DUPLICATE,
    ENQUEUE_NEW,
    MemoryQueueBackend,
    MongoQueueBackend,
    PersistentQueue,
)
from ServiceComponent.PricingPolicyRegistry import (  # noqa: E402
    ModelPricing,
    PricingPolicyRegistry,
)

try:
    from IntelligenceHub import IntelligenceHub
    HUB_IMPORTABLE = True
except Exception:
    HUB_IMPORTABLE = False


def _sh_fixed():
    return datetime(2026, 9, 20, 20, 0, 0)


def _make_drainer(on_complete=None, invoke_handler=None, confirm_fn=None):
    pricing = PricingPolicyRegistry(
        flash_model="flash", pro_model="pro",
        flash_pricing=ModelPricing(model_name="flash", is_flash=True),
        pro_pricing=ModelPricing(model_name="pro", is_flash=False))
    router = DeepSeekModelRouter(
        ApiTimePolicy(), pricing, BudgetGuard(daily_budget_cny=10.0, now_fn=_sh_fixed),
        ApiUsageLedger(now_fn=_sh_fixed), api_enabled=True, api_dry_run=True)
    queue = PersistentQueue(MemoryQueueBackend())
    d = OffPeakQueueDrainer(
        router=router, mock_client=MockDeepSeekClient(),
        drain_interval_seconds=0, max_per_minute=100,
        persistent_queue=queue,
        invoke_handler=invoke_handler,
        on_complete=on_complete)
    if confirm_fn is not None:
        d.archive_confirm_fn = confirm_fn
    return d, queue


def _business_result(uuid):
    return {"model": "flash", "is_dry_run": True,
            "business": {"UUID": uuid, "EVENT_TITLE": "t"}}


class TestEnqueueTriState(unittest.TestCase):
    """验收 1/6：入队结果区分新增、重复与存储失败。"""

    def test_new_then_duplicate(self):
        d, _ = _make_drainer()
        self.assertEqual(d.enqueue({'content_hash': 'h1', 'content': 'a'}),
                         ENQUEUE_NEW)
        self.assertEqual(d.enqueue({'content_hash': 'h1', 'content': 'a'}),
                         ENQUEUE_DUPLICATE, '确定重复')

    def test_backend_error_raises_not_duplicate(self):
        """原缺陷暴露：Mongo 后端连接错误原先被当作"已存在"（False），
        Hub 侧再当作幂等命中返回提交成功。现在必须抛出异常。"""

        class _BrokenCollection:
            def insert_one(self, doc):
                raise ConnectionError('mongo down')

            def create_index(self, *a, **kw):
                pass

        backend = MongoQueueBackend(_BrokenCollection())
        q = PersistentQueue(backend)
        with self.assertRaises(ConnectionError):
            q.enqueue_ex('h1', {'content': 'a'})
        # 同样地 drainer.enqueue 也必须传播
        d, _ = _make_drainer()
        d.queue = q
        with self.assertRaises(ConnectionError):
            d.enqueue({'content_hash': 'h1', 'content': 'a'})

    def test_duplicate_key_is_duplicate(self):
        class _DupErr(Exception):
            code = 11000

        class _Col:
            def __init__(self):
                self.docs = set()

            def insert_one(self, doc):
                if doc['_id'] in self.docs:
                    raise _DupErr('E11000 duplicate key')
                self.docs.add(doc['_id'])

            def create_index(self, *a, **kw):
                pass

        q = PersistentQueue(MongoQueueBackend(_Col()))
        self.assertEqual(q.enqueue_ex('h1', {}), ENQUEUE_NEW)
        self.assertEqual(q.enqueue_ex('h1', {}), ENQUEUE_DUPLICATE)


class TestCrashAfterCacheWrite(unittest.TestCase):
    """验收 2：缓存写入后、入队前崩溃 → 补投不被永久拒绝。"""

    def _hub_for_requeue(self, flag):
        hub = IntelligenceHub.__new__(IntelligenceHub)
        hub.use_off_peak_drainer = True
        hub.off_peak_drainer = MagicMock()
        import queue as _queue
        hub.original_queue = _queue.Queue()
        hub.unarchived_queue = _queue.Queue()
        hub.cache_db_query_engine = MagicMock()
        appendix = {} if flag is None else {'__ARCHIVED__': flag}
        hub.cache_db_query_engine.common_query.return_value = [
            {'UUID': 'u1', 'informant': 'https://x/1', 'APPENDIX': appendix}]
        return hub

    def test_no_flag_allows_requeue(self):
        """原缺陷暴露：cache 查重命中即永久拒绝（无标记=崩溃断点数据卡死）。"""
        hub = self._hub_for_requeue(flag=None)
        data = {'UUID': 'u1', 'informant': 'https://x/1'}
        self.assertTrue(hub._allow_requeue_duplicated_cache_data(data),
                        '无标记（崩溃断点）必须允许补投')

    def test_flag_e_allows_requeue(self):
        hub = self._hub_for_requeue(flag='E')
        self.assertTrue(hub._allow_requeue_duplicated_cache_data(
            {'UUID': 'u1', 'informant': 'https://x/1'}))

    def test_terminal_flags_deny_requeue(self):
        for flag in ('A', 'U', 'D', 'M', 'S'):
            hub = self._hub_for_requeue(flag=flag)
            self.assertFalse(
                hub._allow_requeue_duplicated_cache_data(
                    {'UUID': 'u1', 'informant': 'https://x/1'}),
                f'终态 {flag} 不得补投')

    def test_drainer_mode_required(self):
        """旧 Worker 模式（非 Drainer）不走补投判定。"""
        hub = self._hub_for_requeue(flag=None)
        hub.use_off_peak_drainer = False
        self.assertFalse(hub._allow_requeue_duplicated_cache_data(
            {'UUID': 'u1', 'informant': 'https://x/1'}))


class TestCrashAfterComplete(unittest.TestCase):
    """验收 3：分析结果保存后崩溃 → 重放交付，AI 只调用 1 次。"""

    def test_replay_delivers_once_no_ai_recall(self):
        calls = {'invoke': 0}
        delivered = []

        def invoke(task):
            calls['invoke'] += 1
            resp = MockDeepSeekClient().chat_completion(
                messages=[{'role': 'user', 'content': 'abc'}])
            return resp, {'UUID': task.get('original_uuid'), 'EVENT_TITLE': 't'}

        def on_complete(item_id, result):
            delivered.append(item_id)
            return True

        d, q = _make_drainer(on_complete=on_complete, invoke_handler=invoke)
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        q.complete(item['item_id'], d.worker_id, result=result)  # 断点：此后崩溃
        self.assertEqual(calls['invoke'], 1)

        d2, _ = _make_drainer(on_complete=on_complete, invoke_handler=invoke)
        d2.queue = q
        d2.replay_undelivered()
        self.assertEqual(delivered, [item['item_id']])
        self.assertEqual(calls['invoke'], 1, '重放绝不重复调用 AI')


class TestCrashAfterMemoryEnqueue(unittest.TestCase):
    """验收 4：内存入队后（DELIVERED 未 ACK）崩溃 → 对账重投，不重复调模型。"""

    def test_redeliver_without_ai_recall(self):
        """原缺陷暴露：DELIVERED 后不再重投，processed_queue 崩溃丢失 → 漏归档。"""
        calls = {'invoke': 0}
        redeliver = []

        def invoke(task):
            calls['invoke'] += 1
            resp = MockDeepSeekClient().chat_completion(
                messages=[{'role': 'user', 'content': 'abc'}])
            return resp, {'UUID': task.get('original_uuid'), 'EVENT_TITLE': 't'}

        def on_complete(item_id, result):
            redeliver.append(item_id)
            return True

        def confirm_fn(business):
            return False  # 归档未完成（cache 无 A 标记）

        d, q = _make_drainer(on_complete=on_complete, invoke_handler=invoke,
                             confirm_fn=confirm_fn)
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        q.complete(item['item_id'], d.worker_id, result=result)
        d._deliver_result(item, result)  # 模拟内存队列接收成功
        self.assertEqual(len(redeliver), 1)
        # 断点③：此后崩溃——processed_queue 数据丢失

        d2, _ = _make_drainer(on_complete=on_complete, invoke_handler=invoke,
                              confirm_fn=confirm_fn)
        d2.queue = q
        d2.replay_undelivered()
        self.assertEqual(len(redeliver), 2, '未确认项必须重投（原缺陷：漏归档）')
        self.assertEqual(calls['invoke'], 1, '重投复用已保存结果，不重新付费分析')


class TestCrashAfterArchiveWrite(unittest.TestCase):
    """验收 5/7：归档写入后确认 ACK；低价值路径（D）确认。"""

    def test_acknowledge_no_duplicate_delivery(self):
        calls = {'invoke': 0}
        redeliver = []

        def invoke(task):
            calls['invoke'] += 1
            resp = MockDeepSeekClient().chat_completion(
                messages=[{'role': 'user', 'content': 'abc'}])
            return resp, {'UUID': task.get('original_uuid'), 'EVENT_TITLE': 't'}

        def on_complete(item_id, result):
            redeliver.append(item_id)
            return True

        def confirm_fn(business):
            return True  # 归档已完成（cache flag=A）

        d, q = _make_drainer(on_complete=on_complete, invoke_handler=invoke,
                             confirm_fn=confirm_fn)
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        q.complete(item['item_id'], d.worker_id, result=result)
        d._deliver_result(item, result)
        self.assertEqual(len(redeliver), 1)

        d2, _ = _make_drainer(on_complete=on_complete, invoke_handler=invoke,
                              confirm_fn=confirm_fn)
        d2.queue = q
        d2.replay_undelivered()
        self.assertEqual(len(redeliver), 1, '已确认项不得重复投递（不重复归档）')
        self.assertEqual(calls['invoke'], 1)
        self.assertEqual(q.list_delivered_unacknowledged(), [], 'ACK 后移出对账列表')

    def test_low_value_path_confirmed_via_drop_flag(self):
        """低价值信息保存路径（D 标记）也可最终确认。"""
        redeliver = []

        def on_complete(item_id, result):
            redeliver.append(item_id)
            return True

        def confirm_fn(business):
            return True  # post_process 低值路径打 D 后确认

        d, q = _make_drainer(on_complete=on_complete, confirm_fn=confirm_fn)
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)
        result = _business_result('u1')
        q.complete(item['item_id'], d.worker_id, result=result)
        d._deliver_result(item, result)
        d2, _ = _make_drainer(on_complete=on_complete, confirm_fn=confirm_fn)
        d2.queue = q
        d2.replay_undelivered()
        self.assertEqual(len(redeliver), 1, '低值保存确认后不重投')
        self.assertEqual(q.list_delivered_unacknowledged(), [])


class TestStateUpdateFailures(unittest.TestCase):
    """验收 6：complete/deliver 状态更新失败或返回 False 的处理。"""

    def test_complete_false_still_delivers_and_rewrites(self):
        """complete 返回 False：交付保护已付费成果并补写（原缺陷：忽略返回值）。"""
        redeliver = []
        complete_calls = {'n': 0}

        def on_complete(item_id, result):
            redeliver.append(item_id)
            return True

        d, q = _make_drainer(on_complete=on_complete)
        orig_complete = q.complete

        def flaky_complete(item_id, worker_id, result=None):
            complete_calls['n'] += 1
            if complete_calls['n'] == 1:
                return False  # 第一次失败（网络抖动）
            return orig_complete(item_id, worker_id, result=result)

        q.complete = flaky_complete
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        # 复现 _drain_loop 的处理：complete 失败 → 交付 → 补写
        completed = q.complete(item['item_id'], d.worker_id, result=result)
        self.assertFalse(completed)
        d._deliver_result(item, result)
        self.assertEqual(redeliver, [item['item_id']], '结果必须交付（保护已付费成果）')
        self.assertTrue(orig_complete(item['item_id'], d.worker_id, result=result),
                        '交付成功后补写 complete')
        self.assertEqual(q.count_completed(), 1)

    def test_deliver_false_keeps_undelivered_for_replay(self):
        """deliver 返回 False：保持未交付（原缺陷：无视返回值计为已交付）。"""
        delivered = []

        def on_complete(item_id, result):
            delivered.append(item_id)
            return True

        d, q = _make_drainer(on_complete=on_complete)
        q.backend.mark_delivered = lambda item_id: False  # 模拟状态更新失败
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)
        result = _business_result('u1')
        q.complete(item['item_id'], d.worker_id, result=result)
        ok = d._deliver_result(item, result)
        self.assertFalse(ok, 'deliver 失败必须返回 False（不得计为已交付）')
        self.assertEqual(len(q.list_completed_undelivered()), 1,
                         '保持未交付，下一轮重放（归档幂等兜底）')


@unittest.skipUnless(HUB_IMPORTABLE, "IntelligenceHub 不可导入")
class TestHubSubmitBackendError(unittest.TestCase):
    """验收 6：数据库连接错误不能返回"重复任务"或"提交成功"。"""

    def test_drainer_enqueue_error_returns_error(self):
        import threading
        hub = IntelligenceHub.__new__(IntelligenceHub)
        hub.lock = threading.Lock()
        hub.disk_guard = None
        hub.prefilter = MagicMock()
        pf = MagicMock()
        pf.decision = 'NEEDS_AI'
        pf.content_hash = 'h1'
        pf.original_uuid = 'u1'
        pf.source_id = 's1'
        hub.prefilter.screen = lambda data: pf
        hub.prefilter.registry.get = lambda sid: None
        hub.use_off_peak_drainer = True

        class _BrokenDrainer:
            def enqueue(self, task):
                raise ConnectionError('mongo down')  # 存储故障

        hub.off_peak_drainer = _BrokenDrainer()
        hub._cache_original_data = lambda data: None
        hub.prefilter_rejected_count = 0
        hub.prefilter_metadata_only_count = 0
        hub.prefilter_needs_ai_count = 0
        hub._check_duplication_in_unprocess_data = lambda data: False
        hub._normalize_url = lambda u: u
        from Tools.DateTimeUtility import get_aware_time
        data = {
            'UUID': 'u1', 'informant': 'https://x/1',
            'content': 'a' * 2000, 'title': 't',
            'token': 'test-token',
            'collect_time': get_aware_time(),
            '__TIME_POST__': get_aware_time(),
        }
        result = hub.submit_collected_data(data)
        self.assertFalse(bool(result), '存储故障必须返回 Error（不报成功）')
        self.assertTrue(any('backend' in str(e).lower() for e in result.error_list),
                        f'错误信息应指明后端故障：{result.error_list}')


if __name__ == '__main__':
    unittest.main()
