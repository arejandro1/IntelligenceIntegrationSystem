# -*- coding: utf-8 -*-
"""
MockDeepSeekClient Mock 客户端（M3）
用于 DRY_RUN 模式下的无真实调用 AI 客户端。
返回模拟的 DeepSeek API 响应。
"""
from datetime import datetime
from typing import Dict, Optional
import uuid
import json


class MockDeepSeekClient:
    """Mock DeepSeek API 客户端——DRY_RUN 模式使用"""

    def __init__(self, model_name: str = "deepseek-flash"):
        self.model_name = model_name
        self.call_count: int = 0

    def chat_completion(self, messages: list, max_tokens: int = 1000,
                        temperature: float = 0.3) -> Dict:
        """模拟 chat completion 响应"""
        self.call_count += 1
        content_text = self._mock_response(messages)
        input_tokens = sum(len(m.get('content', '')) for m in messages) // 3
        output_tokens = len(content_text) // 3
        return {
            "id": f"mock-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": self.model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content_text},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": max(input_tokens, 1),
                "completion_tokens": max(output_tokens, 1),
                "total_tokens": max(input_tokens + output_tokens, 2)
            }
        }

    def _mock_response(self, messages: list) -> str:
        """根据输入生成 Mock 回复"""
        last_msg = messages[-1].get('content', '') if messages else ''
        if '分析' in last_msg or 'analyze' in last_msg.lower():
            return json.dumps({
                "title": "Mock Analysis Result",
                "summary": "This is a DRY_RUN mock analysis for verification purposes.",
                "score": 5,
                "category": "test",
                "tags": ["mock", "dry_run"],
                "key_entities": [],
                "sentiment": "neutral"
            }, ensure_ascii=False)
        return json.dumps({"status": "ok", "note": "DRY_RUN mock response"}, ensure_ascii=False)