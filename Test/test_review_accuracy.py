# -*- coding: utf-8 -*-
"""批次A 审查修复的准确度回归测试（不调用真实 API / MongoDB / 向量库）。

覆盖：
① 评分引擎不再误杀 "二十大召开""观察员称""一周内" 标题（回顾类误判 ×0.35）；
② URL 规范化写回后，queue 查重对同一 URL 的变体一致命中；
③ looks_non_zh 对韩/俄/阿/日语判定为需翻译，对中文判定为不需；
④ RATE 键别名归一（'新颖性'→'新颖性与异常性'、英文键→中文全名）；
⑤ '无'/'无法判断' → None，评分时不计入权重（按有值维度归一化），而非计 0。
"""
import dataclasses
import importlib
import re
import sys
import types
import unittest


class _StubModule(types.ModuleType):
    """空桩模块：任意属性按需构造为 dummy 类，仅保证 import 期不炸。
    只在最小测试环境（缺 pymongo/apscheduler 等）中使用；测试只调用纯逻辑，
    不会真正执行桩对象的功能。"""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        dummy = type(name, (), {})
        setattr(self, name, dummy)
        return dummy


class _StubRetryCond:
    """tenacity 桩：retry 条件支持 | 组合运算。"""

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self


# 需要在 import 期具备真实行为/常量的模块，给功能性桩
_FUNCTIONAL_STUBS = {
    'tzlocal': {'get_localzone_name': lambda: 'UTC'},
    'tenacity': {
        'retry': lambda **kw: (lambda f: f),
        'stop_after_attempt': lambda *a, **kw: None,
        'wait_exponential': lambda *a, **kw: None,
        'retry_if_exception_type': lambda *a, **kw: _StubRetryCond(),
        'retry_if_result': lambda *a, **kw: _StubRetryCond(),
    },
    'attr': {'dataclass': dataclasses.dataclass, 'field': dataclasses.field},
    'pymongo': {'ASCENDING': 1, 'DESCENDING': -1},
    # except 子句需要真实异常类
    'pymongo.errors': {'ConnectionFailure': Exception, 'PyMongoError': Exception},
}


def _auto_stub_module(name: str) -> None:
    """为缺失的可选依赖注册桩模块（仅在最小测试环境中使用）。"""
    if name in sys.modules:
        return
    module = _StubModule(name)
    module.__path__ = []  # 假装是包，允许 from x.y import z 继续解析
    for attr, value in _FUNCTIONAL_STUBS.get(name, {}).items():
        setattr(module, attr, value)
    sys.modules[name] = module
    if '.' in name:
        parent_name, _, child = name.rpartition('.')
        parent = sys.modules.get(parent_name)
        if parent is None:
            _auto_stub_module(parent_name)
            parent = sys.modules[parent_name]
        setattr(parent, child, module)


def _import_with_stubs(module_name: str):
    """导入目标模块；最小环境中对缺失的第三方依赖自动打桩后重试。

    全量依赖环境下等价于直接 import。仅用于测试纯逻辑（不触碰桩模块的功能）。
    """
    for _ in range(100):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            if not e.name:
                raise
            _auto_stub_module(e.name)
        except ImportError as e:
            # from X import Y 失败：桩模块上补一个 dummy 属性
            m = re.match(r"cannot import name '(\w+)' from '([\w.]+)'", str(e))
            if not m:
                raise
            attr, parent = m.group(1), m.group(2)
            if parent not in sys.modules:
                _auto_stub_module(parent)
            setattr(sys.modules[parent], attr, type(attr, (), {}))
    raise RuntimeError(f"Too many missing dependencies when importing {module_name}")


class TestScoringNoFalseRecapKill(unittest.TestCase):
    """① 回顾类误杀：正常新闻标题不得触发 ×0.35 惩罚"""

    def setUp(self):
        module = _import_with_stubs('ServiceComponent.IntelligenceScoringEngine')
        self.engine = module.IntelligenceScoringEngine()
        self.base = {
            'TAXONOMY': '政治与安全',
            'RATE': {
                '影响广度': 7, '影响深度': 7, '新颖性与异常性': 7,
                '演化与连锁潜力': 6, '舆情及认知影响': 5, '可行动性': 6,
            },
            'EVENT_TEXT': '', 'EVENT_BRIEF': '', 'REASON': '', 'TIPS': '',
        }

    def test_normal_titles_not_penalized(self):
        for title in ('二十大召开', '观察员称局势缓和', '一周内三次会议'):
            data = dict(self.base, EVENT_TITLE=title)
            self.assertFalse(self.engine._is_recap_like(data), f'误判为回顾类: {title}')
            score = self.engine.calculate_single(data)
            self.assertGreater(score, 3.0, f'{title} 被回顾类惩罚误杀, score={score}')

    def test_llm_marked_recap_still_penalized(self):
        # LLM 按 V25 prompt 规则写出的标记语仍然触发惩罚（保留对真正回顾类的压制）
        data = dict(self.base, EVENT_TITLE='本周要闻', REASON='回顾类，新增事实有限')
        self.assertTrue(self.engine._is_recap_like(data))
        self.assertLessEqual(self.engine.calculate_single(data), 3.0)

    def test_sub_event_count_strict_pattern(self):
        # "同时/第一"等普通子串不再误触发；只数编号列表模式且阈值 ≥5
        data = dict(self.base, EVENT_TEXT='他第一时间报道了这一消息，同时也关注了后续。')
        self.assertEqual(self.engine._count_sub_events(data), 0)
        data2 = dict(self.base, EVENT_TEXT='第一、第二、第三、第四、第五、')
        self.assertEqual(self.engine._count_sub_events(data2), 5)


class TestURLNormalizeConsistency(unittest.TestCase):
    """② URL 规范化写回后，queue/db 查重对变体一致"""

    def setUp(self):
        module = _import_with_stubs('IntelligenceHub')
        self.hub_cls = module.IntelligenceHub
        # 绕过 __init__（会起线程/连库），只测纯方法
        self.hub = object.__new__(self.hub_cls)

    def test_normalize_url_strips_tracking(self):
        normalized = self.hub._normalize_url('HTTP://Example.COM/news/1/?utm_source=feed&ref=top#frag')
        self.assertEqual(normalized, 'http://example.com/news/1')

    def test_queue_dedup_consistent_after_writeback(self):
        import queue as queue_module
        # 模拟写回后的存量数据（informant 已规范化）
        normalized = self.hub._normalize_url('HTTP://Example.COM/news/1/?utm_source=feed&ref=top#frag')
        q = queue_module.Queue()
        q.put({'UUID': 'old-uuid', 'informant': normalized})
        # 新数据带跟踪参数的变体 URL：规范化后应命中查重
        variant = {'UUID': 'new-uuid', 'informant': 'https://example.com/news/1?utm_campaign=x'}
        # scheme 不同（http/https）属不同地址，不命中；同 scheme 变体必须命中
        same = {'UUID': 'new-uuid', 'informant': 'http://EXAMPLE.com/news/1/?utm_campaign=x&gclid=9'}
        self.assertTrue(self.hub._check_duplication_in_queue(same, 'informant', q))
        self.assertFalse(self.hub._check_duplication_in_queue(variant, 'informant', q))
        other = {'UUID': 'new-uuid', 'informant': 'http://example.com/other/2'}
        self.assertFalse(self.hub._check_duplication_in_queue(other, 'informant', q))


class TestLooksNonZh(unittest.TestCase):
    """③ 翻译失明修复：韩/俄/阿/日语判定需翻译，中文判定不需"""

    def setUp(self):
        module = _import_with_stubs('ServiceComponent.AsyncTranslationPatch')
        self.looks_non_zh = module.looks_non_zh

    def test_korean_needs_translation(self):
        self.assertTrue(self.looks_non_zh('한국 정부가 새로운 정책을 발표했다고 밝혔다'))

    def test_russian_needs_translation(self):
        self.assertTrue(self.looks_non_zh('Правительство объявило о новых санкциях против страны'))

    def test_arabic_needs_translation(self):
        self.assertTrue(self.looks_non_zh('أعلنت الحكومة عن قرارات جديدة بشأن الاقتصاد'))

    def test_japanese_needs_translation(self):
        self.assertTrue(self.looks_non_zh('日本政府が新しい方針を発表した'))

    def test_chinese_not_needs_translation(self):
        self.assertFalse(self.looks_non_zh('中国政府宣布了一项新的经济政策，涉及多个行业调整'))


class TestRateKeyAliasAndNone(unittest.TestCase):
    """④ RATE 键别名归一；⑤ '无'/'无法判断' → None 不计权重"""

    def setUp(self):
        defines = _import_with_stubs('ServiceComponent.IntelligenceHubDefines_v2')
        scoring = _import_with_stubs('ServiceComponent.IntelligenceScoringEngine')
        self.ProcessedData = defines.ProcessedData
        self.engine = scoring.IntelligenceScoringEngine()

    def test_chinese_alias_normalized(self):
        m = self.ProcessedData.model_validate({'TAXONOMY': '政治与安全', 'RATE': {'新颖性': 8}})
        self.assertEqual(m.RATE.get('新颖性与异常性'), 8)
        self.assertNotIn('新颖性', m.RATE)

    def test_english_alias_normalized(self):
        m = self.ProcessedData.model_validate(
            {'TAXONOMY': '政治与安全', 'RATE': {'Impact Scope': 6, 'Actionability': '7'}})
        self.assertEqual(m.RATE.get('影响广度'), 6)
        self.assertEqual(m.RATE.get('可行动性'), 7)

    def test_none_token_becomes_none(self):
        m = self.ProcessedData.model_validate(
            {'TAXONOMY': '政治与安全', 'RATE': {'影响深度': '无', '可行动性': '无法判断'}})
        self.assertIsNone(m.RATE['影响深度'])
        self.assertIsNone(m.RATE['可行动性'])

    def test_none_dimension_not_counted_in_weights(self):
        # None 维度不计入权重：5 个有值维度全 8 分，应按 8 分归一（×1.05 分类系数），
        # 若 None 被计 0 分，总分会被拖到约 5.8
        data = {
            'TAXONOMY': '经济与金融',
            'RATE': {
                '影响广度': 8, '影响深度': None, '新颖性与异常性': 8,
                '演化与连锁潜力': 8, '舆情及认知影响': 8, '可行动性': 8,
            },
            'EVENT_TEXT': '', 'EVENT_BRIEF': '', 'REASON': '', 'TIPS': '', 'EVENT_TITLE': 'x',
        }
        score = self.engine.calculate_single(data)
        self.assertAlmostEqual(score, 8.4, places=1)


if __name__ == '__main__':
    unittest.main()
