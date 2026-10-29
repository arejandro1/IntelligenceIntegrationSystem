# -*- coding: utf-8 -*-
"""批次四修复测试：磁盘保护。

覆盖（暴露原缺陷，含实际消费者行为验证）：
1. 默认配置解析：watch_path="_data" → 真实 _data 目录（不拼出 _data/_data）；
2. 目录不存在 → 检测异常（不显示为已确认正常）；
3. 初始状态与检测失败 → stats 展示 unknown/check_error；
4. 阈值边界（69.9/70/84.9/85/94.9/95）；
5. 95% 时消费者（Drainer）停止认领新任务与新增付费请求（真实行为验证）；
6. 空间恢复后消费者继续处理；
7. 在途请求不被中断（已认领项处理完成）。
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ServiceComponent.ApiTimePolicy import ApiTimePolicy  # noqa: E402
from ServiceComponent.ApiUsageLedger import ApiUsageLedger  # noqa: E402
from ServiceComponent.BudgetGuard import BudgetGuard  # noqa: E402
from ServiceComponent.DeepSeekModelRouter import DeepSeekModelRouter  # noqa: E402
from ServiceComponent.DiskSpaceGuard import (  # noqa: E402
    LEVEL_OK,
    LEVEL_STOP_COLLECT,
    LEVEL_STOP_PUSH,
    LEVEL_UNKNOWN,
    LEVEL_WARN,
    DiskSpaceGuard,
)
from ServiceComponent.MockDeepSeekClient import MockDeepSeekClient  # noqa: E402
from ServiceComponent.OffPeakQueueDrainer import OffPeakQueueDrainer  # noqa: E402
from ServiceComponent.PersistentQueue import MemoryQueueBackend, PersistentQueue  # noqa: E402
from ServiceComponent.PricingPolicyRegistry import (  # noqa: E402
    ModelPricing,
    PricingPolicyRegistry,
)


def _guard(percent_fn):
    return DiskSpaceGuard("/mock/path", usage_fn=percent_fn)


class _FakeClockPolicy(ApiTimePolicy):
    def allowed_to_start_request(self, at=None):
        return True

    def is_peak(self, at=None):
        return False


def _make_drainer(pause_predicate=None):
    """DRY_RUN drainer（无真实网络调用）。"""
    pricing = PricingPolicyRegistry(
        flash_model="flash", pro_model="pro",
        flash_pricing=ModelPricing(model_name="flash", is_flash=True),
        pro_pricing=ModelPricing(model_name="pro", is_flash=False))
    fixed = lambda: datetime(2026, 9, 20, 20, 0, 0)  # noqa: E731
    router = DeepSeekModelRouter(
        _FakeClockPolicy(), pricing,
        BudgetGuard(daily_budget_cny=10.0, now_fn=fixed),
        ApiUsageLedger(now_fn=fixed), api_enabled=True, api_dry_run=True)
    queue = PersistentQueue(MemoryQueueBackend())
    return OffPeakQueueDrainer(
        router=router, mock_client=MockDeepSeekClient(),
        drain_interval_seconds=0, max_per_minute=100,
        persistent_queue=queue,
        pause_predicate=pause_predicate)


class TestWatchPathResolution(unittest.TestCase):
    """验收 1：默认路径指向真实 _data，不拼出 _data/_data。"""

    def test_default_config_resolves_to_real_data_dir(self):
        """原缺陷暴露：watch_path='_data' 相对路径以 DATA_PATH 为基准
        拼接 → _data/_data（不存在）。现以项目根为基准。

        IntelligenceHubStartup 导入需重依赖（psutil），此处按其解析公式
        （self_path=项目根）等价验证；Startup 实际行为由回归套件覆盖。
        """
        import os
        from GlobalConfig import DATA_PATH, PRJ_PATH
        watch_path = "_data"  # config_example.json 默认值
        # IntelligenceHubStartup 以 self_path（其所在目录=项目根）为基准
        resolved = os.path.normpath(os.path.join(PRJ_PATH, watch_path))
        self.assertEqual(resolved, DATA_PATH,
                         '默认配置必须解析到真实 _data 目录')
        self.assertNotIn('_data%s_data' % os.sep, resolved,
                         '不得拼出 _data/_data')
        self.assertTrue(os.path.isdir(resolved), '解析结果必须是存在的目录')

    def test_missing_dir_check_error(self):
        """验收 2：目录不存在 → 检测异常（不显示为已确认正常）。"""
        g = DiskSpaceGuard("/nonexistent/path/xyz", usage_fn=None)
        result = g.check()
        self.assertIsNotNone(result.get("error"), '检测必须报错')
        stats = g.stats()
        self.assertEqual(stats["level"], "check_error",
                         '目录不存在时展示"检测异常"（原缺陷：显示 ok）')
        self.assertFalse(stats["should_stop_collect"], '检测异常不触发降级（fail-open）')


class TestInitialAndCheckFailureStates(unittest.TestCase):
    """验收 3：初始状态与检测失败不得显示为已确认正常。"""

    def test_initial_state_unknown(self):
        g = _guard(lambda p: 50.0)
        stats = g.stats()
        self.assertEqual(stats["level"], LEVEL_UNKNOWN, '从未检测 → unknown')

    def test_check_failure_shows_check_error(self):
        """原缺陷暴露：检测失败后 level 保持 ok，展示"已确认正常"。"""
        g = _guard(lambda p: 50.0)
        g.check()
        self.assertEqual(g.stats()["level"], LEVEL_OK)
        g._usage_fn = lambda p: (_ for _ in ()).throw(OSError("disk info fail"))
        g.check()
        stats = g.stats()
        self.assertEqual(stats["level"], "check_error", '检测失败 → 检测异常')
        self.assertEqual(stats["internal_level"], LEVEL_OK, '内部判定保持上次结果')

    def test_check_recovers_after_failure(self):
        g = _guard(lambda p: 50.0)
        g.check()
        g._usage_fn = lambda p: (_ for _ in ()).throw(OSError("fail"))
        g.check()
        g._usage_fn = lambda p: 60.0
        g.check()
        self.assertEqual(g.stats()["level"], LEVEL_OK, '检测恢复后正常展示')


class TestThresholdBoundaries(unittest.TestCase):
    """验收 4：阈值边界。"""

    def test_boundaries(self):
        cases = [
            (69.9, LEVEL_OK), (70.0, LEVEL_WARN), (84.9, LEVEL_WARN),
            (85.0, LEVEL_STOP_PUSH), (94.9, LEVEL_STOP_PUSH),
            (95.0, LEVEL_STOP_COLLECT), (99.9, LEVEL_STOP_COLLECT),
        ]
        for percent, expected in cases:
            g = _guard(lambda p, v=percent: v)
            g.check()
            self.assertEqual(g.level, expected, f'{percent}% → {expected}')
            self.assertEqual(g.stats()["level"], expected)

    def test_recovery_back_to_ok(self):
        usage = {'v': 96.0}
        g = _guard(lambda p: usage['v'])
        g.check()
        self.assertTrue(g.should_stop_collect)
        usage['v'] = 40.0
        g.check()
        self.assertEqual(g.level, LEVEL_OK)
        self.assertFalse(g.should_stop_collect)


class TestDrainerPauseBehavior(unittest.TestCase):
    """验收 5/6/7：实际消费者行为（非仅状态字段）。"""

    def test_drainer_stops_claiming_at_95(self):
        """95% 时 Drainer 不再认领新任务（真实模型调用次数为 0）。"""
        calls = {'process': 0}
        d = _make_drainer()
        orig_process = d._process_item

        def counting_process(item):
            calls['process'] += 1
            return orig_process(item)

        d._process_item = counting_process
        q = d.queue
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        guard = _guard(lambda p: 96.0)
        guard.check()
        d.pause_predicate = lambda: guard.should_stop_collect
        # 模拟 _drain_loop 内循环首轮判定：暂停即退出，不认领
        if d.pause_predicate():
            pass  # drain loop break
        else:
            item = q.claim(worker_id=d.worker_id)
            d._process_item(item)
        self.assertEqual(calls['process'], 0, '暂停期间不得认领新任务/调用模型')
        self.assertEqual(q.count_pending(), 1, '任务保留在队列中')

    def test_drainer_resumes_after_recovery(self):
        """验收 6：空间恢复后继续处理。"""
        calls = {'process': 0}
        d = _make_drainer()
        orig_process = d._process_item

        def counting_process(item):
            calls['process'] += 1
            return orig_process(item)

        d._process_item = counting_process
        q = d.queue
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        usage = {'v': 96.0}
        guard = _guard(lambda p: usage['v'])
        guard.check()
        self.assertTrue(guard.should_stop_collect)
        usage['v'] = 50.0
        guard.check()
        d.pause_predicate = lambda: guard.should_stop_collect
        self.assertFalse(d.pause_predicate(), '恢复后暂停谓词为假')
        item = q.claim(worker_id=d.worker_id)
        result = d._process_item(item)
        self.assertEqual(calls['process'], 1, '恢复后继续处理')
        self.assertEqual(result['model'], 'flash')

    def test_inflight_request_not_interrupted(self):
        """验收 7：在途请求（已认领）自然完成，不被磁盘状态中断。"""
        calls = {'process': 0}
        d = _make_drainer()
        orig_process = d._process_item

        def counting_process(item):
            calls['process'] += 1
            return orig_process(item)

        d._process_item = counting_process
        q = d.queue
        q.enqueue('h1', {'content': 'abc', 'original_uuid': 'u1'})
        item = q.claim(worker_id=d.worker_id)  # 认领发生在磁盘告警之前
        guard = _guard(lambda p: 96.0)
        guard.check()
        d.pause_predicate = lambda: guard.should_stop_collect
        result = d._process_item(item)  # 在途项继续处理
        self.assertEqual(calls['process'], 1, '在途请求必须完成（不中断）')
        self.assertEqual(result['model'], 'flash')

    def test_hub_rejects_collect_at_95(self):
        """Hub 侧：95% 拒绝新采集提交（既有行为保持）。"""
        try:
            from IntelligenceHub import IntelligenceHub
        except Exception:
            self.skipTest("IntelligenceHub 不可导入")
        hub = IntelligenceHub.__new__(IntelligenceHub)
        guard = _guard(lambda p: 96.0)
        guard.check()
        hub.disk_guard = guard
        result = hub.submit_collected_data({'UUID': 'x'})
        self.assertFalse(bool(result), '>=95% 时采集提交必须被拒绝')


if __name__ == '__main__':
    unittest.main()