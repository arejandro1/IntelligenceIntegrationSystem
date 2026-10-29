# Test/vectordb_stress_test.py
"""
VectorDB 压力与影响评估脚本。

用途：
  - 模拟大量外部向量搜索请求，观察 VectorDB 服务端延迟与拒绝率。
  - 同时模拟内部向量化写入（upsert），验证搜索洪峰是否影响写入吞吐。
  - 输出 P50/P95/P99 延迟、成功率、服务端队列/内存指标。

运行前请确保 VectorDB 服务已启动，例如（必须用 -m 方式启动；
直接 python VectorDB/VectorDBBService.py 会因顶层包导入 VectorDB.aggregation.* 失败）：
    python -m VectorDB.VectorDBBService --db-path ./_data/VectorDB --model BAAI/bge-m3

用法示例：
    python Test/vectordb_stress_test.py \
        --base-url http://127.0.0.1:8001 \
        --collection intelligence_summary \
        --search-concurrency 16 \
        --search-requests 200 \
        --upsert-concurrency 2 \
        --upsert-requests 50
"""

import os
import sys
import time
import random
import string
import argparse
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# 确保能从项目根导入 VectorDBClient
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from VectorDB.VectorDBClient import VectorDBClient, ServerBusyError, ServerInitializingError


def random_text(length: int = 80) -> str:
    """生成随机中文/英文混合文本（仅用于压测）。"""
    words = [
        "中国", "美国", "俄罗斯", "乌克兰", "北约", "欧盟", "日本", "韩国",
        "军事", "经济", "演习", "制裁", "协议", "峰会", "冲突", "谈判",
        "无人机", "导弹", "航母", "情报", "安全", "网络", "能源", "贸易",
    ]
    return " ".join(random.choices(words, k=length // 4)) + " " + "".join(
        random.choices(string.ascii_letters + " ", k=length // 2)
    )


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


class VectorDBStressTest:
    def __init__(self, base_url: str, collection: str, admin_token: str = ""):
        self.client = VectorDBClient(base_url, admin_token=admin_token or None)
        self.collection = collection
        self.remote = self.client.get_collection(collection)

    def warmup(self):
        """等待服务就绪并确认 collection 存在。"""
        print(f"[{now_str()}] 等待 VectorDB 服务就绪...")
        self.client.wait_until_ready(timeout=60)
        try:
            self.client.create_collection(self.collection)
        except Exception:
            pass
        print(f"[{now_str()}] 服务就绪，collection={self.collection}")

    def single_search(self, query_id: int) -> dict:
        start = time.perf_counter()
        try:
            results = self.remote.search(
                query=random_text(40),
                top_n=random.randint(5, 15),
                score_threshold=0.5,
                timeout=30,
            )
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "id": query_id,
                "status": "ok",
                "elapsed_ms": elapsed,
                "result_count": len(results) if isinstance(results, list) else 0,
            }
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            status = "busy" if isinstance(e, (ServerBusyError, ServerInitializingError)) else "error"
            return {
                "id": query_id,
                "status": status,
                "elapsed_ms": elapsed,
                "error": str(e),
            }

    def single_upsert(self, doc_id_prefix: str, idx: int) -> dict:
        start = time.perf_counter()
        doc_id = f"stress_{doc_id_prefix}_{idx}_{int(time.time()*1000)}"
        try:
            self.remote.upsert(
                doc_id=doc_id,
                text=random_text(400),
                metadata={
                    "source": "stress_test",
                    "stress_batch": doc_id_prefix,
                    "timestamp": int(time.time()),
                },
                timeout=30,
            )
            elapsed = (time.perf_counter() - start) * 1000
            return {"id": idx, "status": "ok", "elapsed_ms": elapsed}
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            status = "busy" if isinstance(e, (ServerBusyError, ServerInitializingError)) else "error"
            return {"id": idx, "status": status, "elapsed_ms": elapsed, "error": str(e)}

    def fetch_server_status(self) -> dict:
        try:
            return self.client.get_status()
        except Exception as e:
            return {"error": str(e)}

    def fetch_memory_status(self) -> dict:
        return self.client.get_memory_status()

    def run_search_load(self, concurrency: int, total: int) -> list:
        print(f"[{now_str()}] 启动搜索压测：并发={concurrency}, 总请求={total}")
        results = []
        submitted = 0
        lock = threading.Lock()

        def worker():
            nonlocal submitted
            while True:
                with lock:
                    if submitted >= total:
                        return
                    query_id = submitted
                    submitted += 1
                results.append(self.single_search(query_id))

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"[{now_str()}] 搜索压测完成：成功={sum(1 for r in results if r['status']=='ok')}, "
              f"失败={sum(1 for r in results if r['status']!='ok')}")
        return results

    def run_upsert_load(self, concurrency: int, total: int) -> list:
        print(f"[{now_str()}] 启动写入压测：并发={concurrency}, 总请求={total}")
        batch_id = f"{int(time.time())}"
        results = []
        submitted = 0
        lock = threading.Lock()

        def worker():
            nonlocal submitted
            while True:
                with lock:
                    if submitted >= total:
                        return
                    idx = submitted
                    submitted += 1
                results.append(self.single_upsert(batch_id, idx))

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"[{now_str()}] 写入压测完成：成功={sum(1 for r in results if r['status']=='ok')}, "
              f"失败={sum(1 for r in results if r['status']!='ok')}")
        return results

    @staticmethod
    def summarize(name: str, records: list):
        ok = [r["elapsed_ms"] for r in records if r["status"] == "ok"]
        errors = [r for r in records if r["status"] != "ok"]
        total = len(records)
        success = len(ok)

        print(f"\n=== {name} 汇总 ===")
        print(f"  总请求: {total}")
        print(f"  成功: {success} ({success/total*100:.1f}%)")
        print(f"  失败/拒绝: {len(errors)} ({len(errors)/total*100:.1f}%)")
        if ok:
            ok.sort()
            print(f"  P50:  {ok[int(len(ok)*0.5)]:.1f} ms")
            print(f"  P95:  {ok[int(len(ok)*0.95)]:.1f} ms")
            print(f"  P99:  {ok[min(len(ok)-1, int(len(ok)*0.99))]:.1f} ms")
            print(f"  平均: {statistics.mean(ok):.1f} ms")
            print(f"  最大: {max(ok):.1f} ms")
        if errors:
            by_status = {}
            for r in errors:
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            print(f"  错误分类: {by_status}")


def main():
    parser = argparse.ArgumentParser(description="VectorDB Stress & Impact Test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--collection", default="intelligence_summary")
    parser.add_argument("--search-concurrency", type=int, default=8)
    parser.add_argument("--search-requests", type=int, default=100)
    parser.add_argument("--upsert-concurrency", type=int, default=2)
    parser.add_argument("--upsert-requests", type=int, default=30)
    parser.add_argument(
        "--admin-token",
        default=os.getenv("VECTOR_ADMIN_TOKEN", ""),
        help="VectorDB admin token; defaults to VECTOR_ADMIN_TOKEN",
    )
    parser.add_argument("--mixed", action="store_true",
                        help="同时运行搜索与写入，评估互相影响")
    args = parser.parse_args()

    test = VectorDBStressTest(args.base_url, args.collection, admin_token=args.admin_token)
    test.warmup()

    print(f"[{now_str()}] 压测前服务端状态: {test.fetch_server_status()}")
    print(f"[{now_str()}] 压测前内存/任务状态:")
    print(test.fetch_memory_status())

    search_results = []
    upsert_results = []

    if args.mixed:
        search_thread = threading.Thread(
            target=lambda: search_results.extend(test.run_search_load(args.search_concurrency, args.search_requests))
        )
        upsert_thread = threading.Thread(
            target=lambda: upsert_results.extend(test.run_upsert_load(args.upsert_concurrency, args.upsert_requests))
        )
        search_thread.start()
        upsert_thread.start()
        search_thread.join()
        upsert_thread.join()
    else:
        search_results = test.run_search_load(args.search_concurrency, args.search_requests)
        upsert_results = test.run_upsert_load(args.upsert_concurrency, args.upsert_requests)

    print(f"\n[{now_str()}] 压测后服务端状态: {test.fetch_server_status()}")
    print(f"[{now_str()}] 压测后内存/任务状态:")
    print(test.fetch_memory_status())

    test.summarize("搜索", search_results)
    test.summarize("写入", upsert_results)


if __name__ == "__main__":
    main()
