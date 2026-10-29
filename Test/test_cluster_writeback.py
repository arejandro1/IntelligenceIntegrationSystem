# -*- coding: utf-8 -*-
"""聚类结果写回 MongoDB（ServiceComponent.ClusterWriteback）测试。

使用内存 FakeArchiveWriter 模拟 MongoDBStorage 的 find_one/update 接口，
不连接真实 MongoDB、不调用任何真实 API。

覆盖：
1. 写回字段结构正确（__CLUSTER_CURRENT__ / __CLUSTER_HISTORY__）；
2. 同 (plan_id, version) 重复执行幂等（不重复追加、不发起写）；
3. 历史按版本追加并按 history_keep 截断；
4. Mongo 读写异常不抛出、计入 failed 统计，其余文档正常写入；
5. 空映射 / 缺 version / mongo_writer=None 均为 no-op；
6. 映射内文档不存在于归档库时计 skipped，映射外文档不动；
7. 引擎薄封装 writeback_appendix 与监控线程完成后的自动写回接线；
8. Hub 定时任务把 mongo_db_archive 注入 trigger_offline。
"""
import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ServiceComponent.ClusterWriteback import (  # noqa: E402
    DEFAULT_HISTORY_KEEP,
    writeback_cluster_appendix,
)
from ServiceComponent.IntelligenceHubDefines_v2 import (  # noqa: E402
    APPENDIX_CLUSTER_CURRENT,
    APPENDIX_CLUSTER_HISTORY,
)
from ServiceComponent.IntelligenceAggregationEngine import (  # noqa: E402
    IntelligenceAggregationEngine,
    generate_aggregation_plan,
)

try:
    from IntelligenceHub import IntelligenceHub
    HUB_IMPORTABLE = True
except Exception:
    HUB_IMPORTABLE = False

FIXED_NOW = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return FIXED_NOW


class FakeArchiveWriter:
    """模拟 MongoDBStorage 的 find_one/update 接口（内存字典存储，支持 dot-notation $set）。"""

    def __init__(self, docs: Optional[List[Dict[str, Any]]] = None):
        self._docs: Dict[str, Dict[str, Any]] = {}
        for d in docs or []:
            self._docs[d['UUID']] = copy.deepcopy(d)
        self.update_calls: List[Tuple[Dict, Dict]] = []  # 记录所有写操作
        self.fail_on: set = set()  # 这些 UUID 的 find/update 抛异常

    def find_one(self, query: Dict[str, Any], **kwargs) -> Optional[Dict[str, Any]]:
        doc_uuid = query.get('UUID')
        if doc_uuid in self.fail_on:
            raise RuntimeError('simulated mongo failure')
        doc = self._docs.get(doc_uuid)
        return copy.deepcopy(doc) if doc is not None else None

    def update(self, filter_query: Dict[str, Any], update_data: Dict[str, Any], **kwargs):
        doc_uuid = filter_query.get('UUID')
        if doc_uuid in self.fail_on:
            raise RuntimeError('simulated mongo failure')
        self.update_calls.append((copy.deepcopy(filter_query), copy.deepcopy(update_data)))
        doc = self._docs.get(doc_uuid)
        if doc is None:
            return 0, 0
        # 与 MongoDBStorage.update 一致：非 $ 开头的键自动包成 $set
        if not any(k.startswith('$') for k in update_data):
            update_data = {'$set': update_data}
        for key, value in update_data.get('$set', {}).items():
            self._set_nested(doc, key, value)
        return 1, 1

    @staticmethod
    def _set_nested(doc: Dict[str, Any], dotted_key: str, value: Any) -> None:
        keys = dotted_key.split('.')
        node = doc
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = copy.deepcopy(value)

    def get_doc(self, doc_uuid: str) -> Optional[Dict[str, Any]]:
        return self._docs.get(doc_uuid)


def _make_writer() -> FakeArchiveWriter:
    return FakeArchiveWriter(docs=[
        {'UUID': 'u1', 'APPENDIX': {}},
        {'UUID': 'u2', 'APPENDIX': {'__OTHER__': 'keep-me'}},
        {'UUID': 'u3'},  # 无 APPENDIX
    ])


class TestWritebackFieldStructure(unittest.TestCase):
    """① 字段结构正确：current 全字段 + history 条目四要素 + 其余 APPENDIX 键不受影响。"""

    def test_field_structure(self):
        writer = _make_writer()
        stats = writeback_cluster_appendix(
            writer,
            plan_id='p1',
            version='v1',
            time_range=(100.0, 200.0),
            collection_name='intelligence_summary',
            doc_to_cluster={'u1': 'cluster_0', 'u2': 'cluster_1', 'u3': 'noise'},
            clusters={'cluster_0': {'size': 5}, 'cluster_1': {'size': 2}},
            base_version='v0',
            now_fn=_fixed_now,
        )
        self.assertEqual(stats, {'total': 3, 'updated': 3, 'failed': 0, 'skipped': 0})

        doc1 = writer.get_doc('u1')
        current = doc1['APPENDIX'][APPENDIX_CLUSTER_CURRENT]
        self.assertEqual(current['plan_id'], 'p1')
        self.assertEqual(current['version'], 'v1')
        self.assertEqual(current['cluster_id'], 'cluster_0')
        self.assertEqual(current['cluster_size'], 5)
        self.assertEqual(current['collection_name'], 'intelligence_summary')
        self.assertEqual(current['time_range'], [100.0, 200.0])
        self.assertEqual(current['base_version'], 'v0')
        self.assertEqual(current['updated_at'], FIXED_NOW)
        self.assertFalse(current['is_noise'])

        history = doc1['APPENDIX'][APPENDIX_CLUSTER_HISTORY]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0], {
            'plan_id': 'p1', 'version': 'v1',
            'cluster_id': 'cluster_0', 'updated_at': FIXED_NOW,
        })

        # 噪声文档：is_noise=True，cluster_size 为 None
        doc3 = writer.get_doc('u3')
        current3 = doc3['APPENDIX'][APPENDIX_CLUSTER_CURRENT]
        self.assertTrue(current3['is_noise'])
        self.assertEqual(current3['cluster_id'], 'noise')
        self.assertIsNone(current3['cluster_size'])

        # 既有 APPENDIX 键不受影响
        self.assertEqual(writer.get_doc('u2')['APPENDIX']['__OTHER__'], 'keep-me')


class TestWritebackIdempotency(unittest.TestCase):
    """② 同 (plan_id, version) 重复执行幂等：历史不重复追加，且不发起任何写。"""

    def test_same_version_rerun_is_noop(self):
        writer = _make_writer()
        kwargs = dict(
            plan_id='p1', version='v1', collection_name='c',
            doc_to_cluster={'u1': 'cluster_0', 'u2': 'cluster_1'},
            clusters={'cluster_0': {'size': 1}, 'cluster_1': {'size': 1}},
            now_fn=_fixed_now,
        )
        stats1 = writeback_cluster_appendix(writer, **kwargs)
        self.assertEqual(stats1['updated'], 2)
        calls_after_first = len(writer.update_calls)

        stats2 = writeback_cluster_appendix(writer, **kwargs)
        self.assertEqual(stats2, {'total': 2, 'updated': 0, 'failed': 0, 'skipped': 2})
        self.assertEqual(len(writer.update_calls), calls_after_first,
                         '幂等命中时不得再发起写操作')

        history = writer.get_doc('u1')['APPENDIX'][APPENDIX_CLUSTER_HISTORY]
        self.assertEqual(len(history), 1, '同版本重复执行不得重复追加历史')

    def test_same_version_changed_cluster_self_heals(self):
        """同版本重跑但归属变化：历史条目原地更新为最新值，不产生重复条目。"""
        writer = _make_writer()
        base = dict(plan_id='p1', collection_name='c', now_fn=_fixed_now)
        writeback_cluster_appendix(writer, version='v1',
                                   doc_to_cluster={'u1': 'cluster_0'}, **base)
        writeback_cluster_appendix(writer, version='v1',
                                   doc_to_cluster={'u1': 'cluster_9'}, **base)
        doc = writer.get_doc('u1')
        history = doc['APPENDIX'][APPENDIX_CLUSTER_HISTORY]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['cluster_id'], 'cluster_9')
        self.assertEqual(doc['APPENDIX'][APPENDIX_CLUSTER_CURRENT]['cluster_id'], 'cluster_9')


class TestWritebackHistoryTruncation(unittest.TestCase):
    """③ 历史按版本追加，超出 history_keep 时截断保留最新。"""

    def test_history_append_and_truncate(self):
        writer = _make_writer()
        for i in range(1, 6):  # v1..v5，keep=3
            stats = writeback_cluster_appendix(
                writer,
                plan_id='p1', version=f'v{i}', collection_name='c',
                doc_to_cluster={'u1': f'cluster_{i}'},
                history_keep=3,
                now_fn=_fixed_now,
            )
            self.assertEqual(stats['updated'], 1)

        doc = writer.get_doc('u1')
        history = doc['APPENDIX'][APPENDIX_CLUSTER_HISTORY]
        self.assertEqual([h['version'] for h in history], ['v3', 'v4', 'v5'],
                         '历史应按 history_keep 截断并保留最新')
        self.assertEqual(doc['APPENDIX'][APPENDIX_CLUSTER_CURRENT]['version'], 'v5')
        self.assertEqual(doc['APPENDIX'][APPENDIX_CLUSTER_CURRENT]['cluster_id'], 'cluster_5')

    def test_default_history_keep(self):
        writer = _make_writer()
        for i in range(DEFAULT_HISTORY_KEEP + 2):
            writeback_cluster_appendix(
                writer,
                plan_id='p1', version=f'v{i}', collection_name='c',
                doc_to_cluster={'u1': 'cluster_0'},
                now_fn=_fixed_now,
            )
        history = writer.get_doc('u1')['APPENDIX'][APPENDIX_CLUSTER_HISTORY]
        self.assertEqual(len(history), DEFAULT_HISTORY_KEEP)


class TestWritebackMongoFailure(unittest.TestCase):
    """④ Mongo 读写异常不抛出、计入 failed，其余文档正常写入。"""

    def test_mongo_error_never_raises(self):
        writer = _make_writer()
        writer.fail_on = {'u2'}
        with self.assertLogs('ServiceComponent.ClusterWriteback', level='ERROR'):
            stats = writeback_cluster_appendix(
                writer,
                plan_id='p1', version='v1', collection_name='c',
                doc_to_cluster={'u1': 'cluster_0', 'u2': 'cluster_1'},
                clusters={'cluster_0': {'size': 1}},
                now_fn=_fixed_now,
            )
        self.assertEqual(stats, {'total': 2, 'updated': 1, 'failed': 1, 'skipped': 0})
        # 正常文档已完整写入
        self.assertIn(APPENDIX_CLUSTER_CURRENT, writer.get_doc('u1')['APPENDIX'])
        # 失败文档未被写入
        self.assertNotIn(APPENDIX_CLUSTER_CURRENT, writer.get_doc('u2').get('APPENDIX', {}))


class TestWritebackNoop(unittest.TestCase):
    """⑤ 空映射 / 缺 version / mongo_writer=None 均为 no-op。"""

    def test_empty_mapping_noop(self):
        writer = _make_writer()
        stats = writeback_cluster_appendix(
            writer, plan_id='p1', version='v1', doc_to_cluster={}, now_fn=_fixed_now)
        self.assertEqual(stats, {'total': 0, 'updated': 0, 'failed': 0, 'skipped': 0})
        self.assertEqual(writer.update_calls, [])

    def test_missing_version_noop(self):
        writer = _make_writer()
        stats = writeback_cluster_appendix(
            writer, plan_id='p1', version=None,
            doc_to_cluster={'u1': 'cluster_0'}, now_fn=_fixed_now)
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(writer.update_calls, [])

    def test_none_writer_noop(self):
        stats = writeback_cluster_appendix(
            None, plan_id='p1', version='v1',
            doc_to_cluster={'u1': 'cluster_0'}, now_fn=_fixed_now)
        self.assertEqual(stats, {'total': 0, 'updated': 0, 'failed': 0, 'skipped': 0})


class TestWritebackSkipped(unittest.TestCase):
    """⑥ 映射内但归档库不存在的文档计 skipped；映射外文档不动。"""

    def test_missing_doc_skipped_and_outsiders_untouched(self):
        writer = _make_writer()
        outsider_before = copy.deepcopy(writer.get_doc('u3'))
        stats = writeback_cluster_appendix(
            writer,
            plan_id='p1', version='v1', collection_name='c',
            doc_to_cluster={'u1': 'cluster_0', 'ghost': 'cluster_1'},
            now_fn=_fixed_now,
        )
        self.assertEqual(stats, {'total': 2, 'updated': 1, 'failed': 0, 'skipped': 1})
        self.assertIsNone(writer.get_doc('ghost'))
        self.assertEqual(writer.get_doc('u3'), outsider_before, '映射外文档本次不动')


class TestEngineWritebackWrapper(unittest.TestCase):
    """⑦ 引擎薄封装 writeback_appendix 与监控线程完成后的自动写回接线。"""

    def _engine(self) -> IntelligenceAggregationEngine:
        # vector_client=None：薄封装与监控接线均不触网
        return IntelligenceAggregationEngine(
            vector_client=None, plan_spec=generate_aggregation_plan('hdbscan_fine'))

    def _offline_result(self) -> Dict[str, Any]:
        return {
            'plan_id': 'agg_intelligence_summary_24h',
            'collection_name': 'intelligence_summary',
            'version': 'v1',
            'time_range': [100.0, 200.0],
            'doc_to_cluster': {'u1': 'cluster_0', 'u3': 'noise'},
            'clusters': {'cluster_0': {'size': 7}},
        }

    def test_wrapper_extracts_and_writes(self):
        writer = _make_writer()
        stats = self._engine().writeback_appendix(self._offline_result(), writer)
        self.assertEqual(stats['updated'], 2)
        current = writer.get_doc('u1')['APPENDIX'][APPENDIX_CLUSTER_CURRENT]
        self.assertEqual(current['plan_id'], 'agg_intelligence_summary_24h')
        self.assertEqual(current['version'], 'v1')
        self.assertEqual(current['cluster_size'], 7)

    def test_wrapper_empty_result_noop(self):
        writer = _make_writer()
        stats = self._engine().writeback_appendix({}, writer)
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(writer.update_calls, [])

    def test_wrapper_writer_failure_counted_not_raised(self):
        class _BadWriter:
            def find_one(self, query, **kwargs):
                raise RuntimeError('boom')

        stats = self._engine().writeback_appendix(self._offline_result(), _BadWriter())
        self.assertEqual(stats['failed'], 2, '单文档失败计入统计而非抛出')
        self.assertEqual(stats['updated'], 0)

    def test_wrapper_internal_error_returns_zero_stats(self):
        # time_range 非可迭代对象 → 封装层 tuple() 抛 TypeError → 兜底返回零统计
        bad_result = dict(self._offline_result())
        bad_result['time_range'] = 123
        writer = _make_writer()
        stats = self._engine().writeback_appendix(bad_result, writer)
        self.assertEqual(stats, {'total': 0, 'updated': 0, 'failed': 0, 'skipped': 0})
        self.assertEqual(writer.update_calls, [])

    def test_monitor_triggers_writeback_on_completed(self):
        """监控线程在 job completed 后：先生成快照，再写回归档库。"""
        engine = self._engine()
        engine.get_job = lambda job_id: {'status': 'completed'}
        snapshots = []
        engine._generate_snapshot = lambda job_id, fetcher: snapshots.append(job_id)
        engine.get_latest_offline = lambda: self._offline_result()

        writer = _make_writer()
        engine._monitor_offline_job('job-1', doc_fetcher=None, mongo_writer=writer)

        self.assertEqual(snapshots, ['job-1'], '完成后应先生成快照')
        self.assertIn(APPENDIX_CLUSTER_CURRENT, writer.get_doc('u1')['APPENDIX'],
                      '监控线程完成后应自动写回归档库')

    def test_monitor_skips_writeback_without_writer(self):
        """mongo_writer 未注入时静默跳过，不影响快照与监控循环。"""
        engine = self._engine()
        engine.get_job = lambda job_id: {'status': 'completed'}
        engine._generate_snapshot = lambda job_id, fetcher: None
        engine.get_latest_offline = MagicMock(side_effect=AssertionError('不应被调用'))
        engine._monitor_offline_job('job-1', doc_fetcher=None, mongo_writer=None)

    def test_monitor_survives_vector_outage(self):
        """VectorDB 暂不可达（get_latest_offline 抛异常）：只记日志，不抛出。"""
        engine = self._engine()
        engine.get_job = lambda job_id: {'status': 'completed'}
        engine._generate_snapshot = lambda job_id, fetcher: None
        engine.get_latest_offline = MagicMock(side_effect=ConnectionError('vectordb down'))
        writer = _make_writer()
        engine._monitor_offline_job('job-1', doc_fetcher=None, mongo_writer=writer)
        self.assertEqual(writer.update_calls, [])


@unittest.skipUnless(HUB_IMPORTABLE, 'IntelligenceHub 不可导入（环境缺依赖）')
class TestHubWiring(unittest.TestCase):
    """⑧ Hub 定时任务把 mongo_db_archive 注入 trigger_offline 作为写回目标。"""

    def test_hub_passes_archive_writer_to_trigger(self):
        hub = IntelligenceHub.__new__(IntelligenceHub)
        engine = MagicMock()
        engine.trigger_offline.return_value = 'job-1'
        hub.aggregation_engine_summary = engine
        hub.archive_db_query_engine = MagicMock()
        hub.archive_db_query_engine.get_latest_archive_timestamp.return_value = None
        sentinel_writer = object()
        hub.mongo_db_archive = sentinel_writer
        hub.get_intelligence = MagicMock()

        hub._do_run_summary_aggregation()

        engine.trigger_offline.assert_called_once()
        kwargs = engine.trigger_offline.call_args.kwargs
        self.assertIs(kwargs.get('mongo_writer'), sentinel_writer,
                      '定时聚合必须把归档库注入为写回目标')

    def test_hub_skips_when_engine_not_ready(self):
        hub = IntelligenceHub.__new__(IntelligenceHub)
        hub.aggregation_engine_summary = None
        hub._do_run_summary_aggregation()  # 不抛即通过


if __name__ == '__main__':
    unittest.main()
