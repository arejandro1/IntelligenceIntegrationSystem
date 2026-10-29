# -*- coding: utf-8 -*-
"""
DeepSeek API 连通性烟雾测试（手动运行，非自动化测试套件）

⚠️ 注意：此脚本会发起真实 API 调用，产生极小费用（约 0.001 元）。
   仅用于验证 API Key 和网络连通性，不进入自动化测试套件。
   必须在非高峰时段运行：[00:00,09:00) / [12:00,14:00) / [18:00,24:00)

用法：
    set PYTHONIOENCODING=utf-8
    "C:\\Users\\Administrator\\AppData\\Local\\Python\\bin\\python.exe" _smoke_test_deepseek.py

退出码：
    0 = 成功
    1 = 失败（API Key 无效 / 网络 / 模型错误）
"""
import os
import sys
from datetime import datetime

# 时区 Asia/Shanghai（避免依赖 pytz，用固定偏移）
UTC8 = timezone_shanghai = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))


def check_off_peak() -> bool:
    """检查当前是否为非高峰时段（北京时间）"""
    now = datetime.now(UTC8)
    t = now.hour * 60 + now.minute
    # 高峰：09:00-12:00, 14:00-18:00
    if 9 * 60 <= t < 12 * 60:
        return False
    if 14 * 60 <= t < 18 * 60:
        return False
    return True


def main() -> int:
    # --force: 仅用于人工批准的一次性试验，绕过峰谷守护
    force = "--force" in sys.argv
    print("=" * 60)
    print("DeepSeek API 烟雾测试")
    print("=" * 60)

    # 1. 检查非高峰
    now = datetime.now(UTC8)
    print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    if not check_off_peak():
        if not force:
            print("❌ 当前为高峰时段（[09:00,12:00) / [14:00,18:00)），按规则禁止调用 API。")
            print("   请在非高峰时段运行：[00:00,09:00) / [12:00,14:00) / [18:00,24:00)")
            print("   如已获人工批准一次性试验，可加 --force 绕过（仅限测试）。")
            return 1
        print("⚠️  当前为高峰时段，已检测到 --force（人工批准一次性试验），继续。")
    else:
        print("✅ 处于非高峰时段，继续。")

    # 2. 读取 API Key（仅从环境变量或直接传参，不硬编码）
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("❌ 未设置环境变量 DEEPSEEK_API_KEY")
        print("   请先: set DEEPSEEK_API_KEY=sk-xxxx")
        return 1
    print(f"✅ 已读取 API Key（前 8 位: {api_key[:8]}...）")

    # 3. 调用
    try:
        from ServiceComponent.DeepSeekClient import DeepSeekClient
    except ImportError as e:
        print(f"❌ 无法导入 DeepSeekClient: {e}")
        return 1

    client = DeepSeekClient(
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        timeout=30.0,
        max_retries=2,
    )

    print("📡 发送极简请求（max_tokens=5，最小化费用）...")
    try:
        resp = client.chat_completion(
            messages=[{"role": "user", "content": "回复一个字：好"}],
            model="deepseek-chat",
            max_tokens=5,
            temperature=0.0,
        )
    except Exception as e:
        print(f"❌ 调用失败: {e}")
        return 1

    # 4. 解析结果
    usage = resp.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    print(f"✅ 调用成功！")
    print(f"   模型: {resp.get('model', 'unknown')}")
    print(f"   id: {resp.get('id', 'unknown')}")
    print(f"   prompt_tokens: {prompt_tokens}")
    print(f"   completion_tokens: {completion_tokens}")
    print(f"   total_tokens: {total_tokens}")
    # 估算费用（deepseek-chat: input 0.001元/1k, output 0.002元/1k）
    cost = (prompt_tokens * 0.001 + completion_tokens * 0.002) / 1000
    print(f"   估算费用: {cost:.6f} 元")

    choices = resp.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content", "")
        print(f"   回复内容: {content[:50]}")

    print("=" * 60)
    print("✅ 烟雾测试通过，API Key 有效，模型可调用。")
    print("   可正式启用 OffPeakQueueDrainer 进行真实 AI 分析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())