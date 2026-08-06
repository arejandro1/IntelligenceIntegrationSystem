# -*- coding: utf-8 -*-
"""ApiUsageLedger API调用账本（M3）。记录每次调用供审计"""
from datetime import datetime
from typing import Dict, List
import threading

class ApiUsageLedger:
    def __init__(self):
        self._lock = threading.Lock()
        self._records: List[Dict] = []
    def record(self, attempt_id: str, model: str, input_tokens: int, output_tokens: int, cost_cny: float, is_dry_run: bool):
        with self._lock:
            self._records.append({"attempt_id":attempt_id,"model":model,"input_tokens":input_tokens,"output_tokens":output_tokens,"cost_cny":cost_cny,"is_dry_run":is_dry_run,"timestamp":datetime.now().isoformat()})
    def total_cost(self) -> float:
        with self._lock:
            return round(sum(r["cost_cny"] for r in self._records), 6)
    def count(self) -> int:
        return len(self._records)