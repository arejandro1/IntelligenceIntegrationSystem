# Tools/PerformanceLogger.py
"""
统一性能日志模块。

为 IIS 的关键路径提供“既打印到普通日志，又单独写入结构化性能日志文件”的能力。
主要用途：
  - 定位向量搜索/向量化任务超时热点
  - 监控内存、CPU、队列深度随请求的变化
  - 区分未登录用户与登录用户的资源消耗

日志文件默认写入 _log/perf.log，使用 JSON 格式，便于后续用脚本/ELK 分析。
"""

import os
import sys
import time
import logging
import threading
import traceback
from contextlib import contextmanager
from typing import Any, Dict, Optional
from logging.handlers import RotatingFileHandler


PERF_LOGGER_NAME = "IIS.Performance"
DEFAULT_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_log", "perf.log")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_BACKUP_COUNT = 3

# 避免重复初始化
_setup_lock = threading.Lock()
_setup_done = False


def _snapshot_system() -> Dict[str, Any]:
    """采集当前进程的轻量级系统指标，优先使用 psutil，Windows 下回退到 ctypes。"""
    out = {
        "cpu_percent": None,
        "rss_mb": None,
        "vms_mb": None,
        "thread_count": threading.active_count(),
        "source": None,
    }

    try:
        import psutil
        proc = psutil.Process(os.getpid())
        out["cpu_percent"] = round(proc.cpu_percent(interval=None), 2)
        mem = proc.memory_info()
        out["rss_mb"] = round(mem.rss / 1024 / 1024, 2)
        out["vms_mb"] = round(mem.vms / 1024 / 1024, 2)
        out["source"] = "psutil"
        return out
    except Exception:
        # 有意忽略：性能采样失败时该指标缺省（上层以缺字段呈现）；
        # 观测系统自身故障不再写日志，避免"记日志失败→再记日志"的递归放大
        pass

    if os.name == "nt":
        try:
            import ctypes

            class ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(ProcessMemoryCountersEx)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if ok:
                out["rss_mb"] = round(counters.WorkingSetSize / 1024 / 1024, 2)
                out["vms_mb"] = round(counters.PrivateUsage / 1024 / 1024, 2)
                out["source"] = "windows_psapi"
        except Exception:
            # 有意忽略：Windows 兼容采样失败时该指标缺省（同 psutil 分支，
            # 观测系统自身故障不写日志防递归放大）
            pass

    return out


def setup_performance_logging(
    log_file: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    初始化性能日志 logger。

    幂等：多次调用不会重复添加 handler。
    """
    global _setup_done

    log_file = log_file or DEFAULT_LOG_FILE
    log_file = os.path.abspath(log_file)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger(PERF_LOGGER_NAME)
    logger.setLevel(level)
    # 不要让性能日志向上传播到 root，避免重复打印在 console/iis.log
    logger.propagate = False

    with _setup_lock:
        if _setup_done:
            return logger

        # 清理已有 handler，防止重载时重复
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                # 有意忽略：观测系统自身清理失败不影响重装 handler 的主流程
                pass

        try:
            from pythonjsonlogger import jsonlogger
            formatter = jsonlogger.JsonFormatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        except Exception:
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 同时输出到控制台，满足“既正常打印，又单独落盘”
        class _PerfConsoleFormatter(logging.Formatter):
            def format(self, record):
                payload = record.msg if isinstance(record.msg, dict) else {"message": record.getMessage()}
                ts = self.formatTime(record, datefmt="%H:%M:%S")
                op = payload.get("operation", "-")
                status = payload.get("status", "-")
                elapsed = payload.get("elapsed_ms")
                extra_parts = []
                for k in ("client_ip", "is_public", "search_mode", "top_n", "collection", "result_count", "queue_size", "error"):
                    v = payload.get(k)
                    if v is not None:
                        extra_parts.append(f"{k}={v}")
                extra = " ".join(extra_parts)
                if elapsed is not None:
                    return f"[PERF] {ts} {op} {elapsed:.2f}ms [{status}] {extra}"
                return f"[PERF] {ts} {op} [{status}] {extra}"

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(_PerfConsoleFormatter())
        logger.addHandler(console_handler)

        _setup_done = True

    return logger


class PerformanceLogger:
    """
    性能日志记录器。

    典型用法：
        perf = PerformanceLogger()

        # 方式 1：上下文管理器（自动计算耗时）
        with perf.timed("vector_search", client_ip="1.2.3.4", top_n=10):
            result = do_search()

        # 方式 2：装饰器
        @perf.timed("my_op")
        def my_func():
            ...

        # 方式 3：手动记录
        perf.record("vector_search", elapsed_ms=120, status="ok", result_count=5)
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(PERF_LOGGER_NAME)

    def record(self, operation: str, *, status: str = "ok", elapsed_ms: Optional[float] = None,
               error: Optional[str] = None, extra: Optional[Dict[str, Any]] = None):
        """直接记录一条性能日志。"""
        payload = {
            "operation": operation,
            "status": status,
        }
        if elapsed_ms is not None:
            payload["elapsed_ms"] = round(elapsed_ms, 2)
        if error is not None:
            payload["error"] = str(error)
        if extra:
            # 过滤掉不可序列化的对象，避免 JSON formatter 崩溃
            for k, v in extra.items():
                payload[k] = _safe_json_value(v)

        # 加入系统指标
        payload.update(_snapshot_system())

        if status == "error" or error is not None:
            self.logger.warning(payload)
        else:
            self.logger.info(payload)

    def timed(self, operation: str, **ctx):
        """返回一个上下文管理器/装饰器，自动记录耗时。"""
        return _TimedContext(self, operation, ctx)


class _TimedContext:
    """支持上下文管理器和函数装饰器两种模式。"""

    def __init__(self, perf: PerformanceLogger, operation: str, ctx: Dict[str, Any]):
        self.perf = perf
        self.operation = operation
        self.ctx = ctx
        self.start = None
        self.status = "ok"
        self.error = None

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed_ms = (time.perf_counter() - self.start) * 1000 if self.start else None
        if exc_val is not None:
            self.status = "error"
            self.error = f"{exc_type.__name__}: {exc_val}"
        self.perf.record(
            self.operation,
            status=self.status,
            elapsed_ms=elapsed_ms,
            error=self.error,
            extra=self.ctx,
        )
        return False  # 不吞掉异常

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
        return wrapper


def _safe_json_value(value: Any) -> Any:
    """把常见不可 JSON 序列化的类型转成安全类型。"""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_json_value(v) for k, v in value.items()}
    try:
        # 尝试基本 str 转换
        return str(value)
    except Exception:
        return "<unserializable>"


def get_performance_logger() -> PerformanceLogger:
    """便捷函数：返回已初始化的 PerformanceLogger 实例。"""
    return PerformanceLogger()
