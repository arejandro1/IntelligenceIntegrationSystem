# -*- coding: utf-8 -*-
"""
PushNotifier 推送服务接口（M6）
第一阶段实现主渠道推送去重、推送历史、失败重试
"""
from typing import Dict, List
import threading
from datetime import datetime


class PushNotifier:
    """推送服务基类。M6 阶段提供接口定义和空报告处理"""

    def __init__(self):
        self._lock = threading.Lock()
        self._push_history: List[Dict] = []
        self._dedup_set: set = set()

    def should_push(self, item_id: str) -> bool:
        """检查是否已推送（去重）"""
        with self._lock:
            if item_id in self._dedup_set:
                return False
            self._dedup_set.add(item_id)
            return True

    def record_push(self, item_id: str, channel: str, success: bool) -> None:
        """记录推送历史"""
        with self._lock:
            self._push_history.append({
                "item_id": item_id, "channel": channel,
                "success": success, "timestamp": datetime.now().isoformat()
            })

    def push(self, item: Dict, channel: str = "default") -> bool:
        """执行推送"""
        item_id = item.get('id', '')
        if not self.should_push(item_id):
            return True
        if not item.get('content'):
            self.record_push(item_id, channel, False)
            return False
        self.record_push(item_id, channel, True)
        return True

    def push_count(self) -> int:
        return len(self._push_history)