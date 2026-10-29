# -*- coding: utf-8 -*-
"""IndexedQueue 队列查重索引的单元测试（问题 5）。

覆盖：
1. 索引基本命中/取出移除语义（UUID 与 informant 任一命中即重复）；
2. 同键多条计数语义（取出一条后其余仍可命中）；
3. _check_duplication_in_queue 走 O(1) 索引路径（IndexedQueue）；
4. 外部传入裸 queue.Queue 时回退旧的锁内扫描路径（兼容既有调用方）；
5. 索引路径与旧扫描路径语义等价（多场景对照）；
6. 多线程并发 put/get/查重不异常且最终一致。

不触 MongoDB / 网络。
"""
import queue
import sys
import threading
import types
import unittest

# 兼容 shim（同 test_review_webapi）
try:
    import attr as _real_attr

    _attr_compat = types.ModuleType('attr')
    _attr_compat.__dict__.update(vars(_real_attr))
    _real_attr_field = _real_attr.field

    def _field_compat(*args, **kwargs):
        if 'default_factory' in kwargs:
            kwargs['factory'] = kwargs.pop('default_factory')
        return _real_attr_field(*args, **kwargs)

    _attr_compat.field = _field_compat
    sys.modules['attr'] = _attr_compat
except ImportError:
    import dataclasses as _dataclasses

    _attr_stub = types.ModuleType('attr')
    _attr_stub.dataclass = _dataclasses.dataclass
    _attr_stub.field = _dataclasses.field
    sys.modules['attr'] = _attr_stub

from IntelligenceHub import IntelligenceHub, IndexedQueue


def _make_hub() -> IntelligenceHub:
    # 绕过 __init__（会起线程/连库），只测纯查重逻辑（同 test_review_accuracy 模式）
    return object.__new__(IntelligenceHub)


class TestIndexedQueueBasics(unittest.TestCase):
    """索引基本语义。"""

    def test_put_then_contains_hit(self):
        q = IndexedQueue()
        q.put({'UUID': 'u1', 'informant': 'http://example.com/a'})
        # UUID 命中
        self.assertTrue(q.contains_identifier('u1', 'http://other'))
        # informant 命中
        self.assertTrue(q.contains_identifier('other', 'http://example.com/a'))
        # 均不命中
        self.assertFalse(q.contains_identifier('u2', 'http://example.com/b'))

    def test_get_removes_index(self):
        q = IndexedQueue()
        q.put({'UUID': 'u1', 'informant': 'http://example.com/a'})
        q.get()
        self.assertFalse(q.contains_identifier('u1', ''))
        self.assertFalse(q.contains_identifier('', 'http://example.com/a'))

    def test_duplicate_items_counted(self):
        # 同一 UUID 入队两条：取出一条后仍命中（计数语义），两条取完后不再命中
        q = IndexedQueue()
        q.put({'UUID': 'u1', 'informant': 'x1'})
        q.put({'UUID': 'u1', 'informant': 'x2'})
        q.get()
        self.assertTrue(q.contains_identifier('u1', ''))
        q.get()
        self.assertFalse(q.contains_identifier('u1', ''))

    def test_informant_and_INFORMANT_namespaced(self):
        # CollectedData 用 informant、ArchivedData 用 INFORMANT：两键名分命名空间，
        # 各自按键名命中、不跨键名命中（与旧扫描 item.get(informant_key) 语义一致）
        q = IndexedQueue()
        q.put({'UUID': 'a', 'informant': 'http://example.com/1'})
        q.put({'UUID': 'b', 'INFORMANT': 'http://example.com/2'})
        self.assertTrue(q.contains_identifier('', 'http://example.com/1', 'informant'))
        self.assertTrue(q.contains_identifier('', 'http://example.com/2', 'INFORMANT'))
        # 不跨键名命中（旧扫描行为：只查 informant_key 对应字段）
        self.assertFalse(q.contains_identifier('', 'http://example.com/2', 'informant'))
        self.assertFalse(q.contains_identifier('', 'http://example.com/1', 'INFORMANT'))

    def test_non_dict_and_empty_values_ignored(self):
        q = IndexedQueue()
        q.put(None)
        q.put({'UUID': '', 'informant': None})
        q.put({'UUID': 123, 'informant': 456})  # 非 str 值不索引（与旧扫描的类型比较一致）
        self.assertFalse(q.contains_identifier('anything', 'anything'))


class TestCheckDuplicationIndexPath(unittest.TestCase):
    """_check_duplication_in_queue 的索引路径与回退路径。"""

    def setUp(self):
        self.hub = _make_hub()

    def test_index_path_hits_uuid_and_informant(self):
        q = IndexedQueue()
        q.put({'UUID': 'old', 'informant': 'http://example.com/1'})
        # informant 变体经规范化后命中（与旧扫描语义一致）
        variant = {'UUID': 'new', 'informant': 'http://EXAMPLE.com/1/?utm_campaign=x'}
        self.assertTrue(self.hub._check_duplication_in_queue(variant, 'informant', q))
        # UUID 命中
        self.assertTrue(self.hub._check_duplication_in_queue(
            {'UUID': 'old', 'informant': 'http://example.com/other'}, 'informant', q))
        # 均不命中
        self.assertFalse(self.hub._check_duplication_in_queue(
            {'UUID': 'new', 'informant': 'http://example.com/2'}, 'informant', q))

    def test_fallback_for_plain_queue(self):
        # 外部传入裸 queue.Queue（如既有测试构造）时回退旧的锁内扫描路径
        q = queue.Queue()
        q.put({'UUID': 'old', 'informant': 'http://example.com/1'})
        self.assertTrue(self.hub._check_duplication_in_queue(
            {'UUID': 'new', 'informant': 'http://example.com/1'}, 'informant', q))
        self.assertFalse(self.hub._check_duplication_in_queue(
            {'UUID': 'new', 'informant': 'http://example.com/2'}, 'informant', q))

    def test_index_path_matches_plain_scan_semantics(self):
        # 同一批数据、同一批查询：索引路径与旧扫描路径的判定必须逐项一致
        items = [
            {'UUID': 'a', 'informant': 'http://example.com/1'},
            {'UUID': 'b', 'INFORMANT': 'http://example.com/2'},
            {'UUID': 'c', 'informant': None},
        ]
        queries = [
            {'UUID': 'q', 'informant': 'http://example.com/1'},   # informant 命中
            {'UUID': 'a', 'informant': 'http://example.com/x'},   # UUID 命中
            {'UUID': 'b', 'INFORMANT': 'http://example.com/2'},   # INFORMANT 键命中
            {'UUID': 'z', 'informant': 'http://example.com/9'},   # 不命中
        ]
        for stage in ('full', 'after-one-get'):
            indexed = IndexedQueue()
            plain = queue.Queue()
            for item in items:
                indexed.put(dict(item))
                plain.put(dict(item))
            if stage == 'after-one-get':
                indexed.get()
                plain.get()
            for data in queries:
                for key in ('informant', 'INFORMANT'):
                    self.assertEqual(
                        self.hub._check_duplication_in_queue(data, key, indexed),
                        self.hub._check_duplication_in_queue(data, key, plain),
                        msg=f'semantics differ: stage={stage} data={data} key={key}')


class TestIndexedQueueConcurrency(unittest.TestCase):
    """并发 put/get/查重：不抛异常且最终一致。"""

    def test_concurrent_put_get_query_consistent(self):
        q = IndexedQueue()
        producers = 4
        per_producer = 400
        total = producers * per_producer
        errors = []
        consumed = []
        stop = threading.Event()

        def producer(pid: int):
            try:
                for i in range(per_producer):
                    q.put({'UUID': f'u-{pid}-{i}',
                           'informant': f'http://example.com/{pid}/{i}'})
            except Exception as e:  # pragma: no cover - 记录未预期异常
                errors.append(('put', pid, repr(e)))

        def consumer():
            try:
                while not stop.is_set():
                    try:
                        item = q.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    try:
                        # 并发查询不抛异常即可（值不确定：其他项可能同键）
                        q.contains_identifier(str(item.get('UUID', '')),
                                              str(item.get('informant', '')))
                    finally:
                        q.task_done()
                        consumed.append(1)
            except Exception as e:  # pragma: no cover - 记录未预期异常
                errors.append(('get', None, repr(e)))

        threads = [threading.Thread(target=producer, args=(p,)) for p in range(producers)]
        threads += [threading.Thread(target=consumer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads[:producers]:
            t.join()
        q.join()  # 全部项被消费且 task_done
        stop.set()
        for t in threads[producers:]:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(consumed), total)
        self.assertEqual(q.qsize(), 0)
        # 队列清空后索引必须同步清空（无泄漏、无误留）
        for p in range(producers):
            for i in (0, per_producer // 2, per_producer - 1):
                self.assertFalse(q.contains_identifier(f'u-{p}-{i}',
                                                       f'http://example.com/{p}/{i}'))


if __name__ == '__main__':
    unittest.main()
