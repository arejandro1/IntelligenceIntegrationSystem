import hashlib
import os
import sys
import time
import queue
import logging
import threading
import traceback
from pathlib import Path
from threading import Thread
from dataclasses import dataclass
from typing import Optional, Tuple, Dict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from GlobalConfig import *
from IntelligenceCrawler.CrawlerFlowScheduler import FlowScheduler
from MyPythonUtility.easy_config import EasyConfig
from PyLoggingBackend import LoggerBackend
from PyLoggingBackend.LogUtility import set_tls_logger, backup_and_clean_previous_log_file, setup_logging, \
    limit_logger_level
from MyPythonUtility.plugin_manager import PluginManager, PluginWrapper
from IntelligenceCrawler.CrawlerGovernanceBackend import CrawlerGovernanceBackend
from IntelligenceCrawler.CrawlerGovernanceCore import GovernanceManager

logger = logging.getLogger(__name__)
logger.info(f"[BOOT] pid={os.getpid()}")
project_root = os.path.dirname(os.path.abspath(__file__))


def _file_sig(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class ServiceContext:
    """
    Use this class to pass parameters to plugins and to selectively expose functions to plugins.
    """
    def __init__(
            self,
            module_logger: Optional[logging.Logger] = None,
            module_config: Optional[EasyConfig] = None,
            crawler_governor: GovernanceManager = None
    ):
        self.sys = sys
        self.logger = module_logger or logger
        self.config = module_config or EasyConfig()
        self.crawler_governor = crawler_governor
        self.project_root = project_root

    def solve_import_path(self):
        import sys              # Import sys here because we must use the same sys with plugin.
                                # Just for test.
        if self.sys == sys:
            print('The same sys')
        else:
            if self.project_root not in sys.path:
                sys.path.insert(0, self.project_root)

            print('Different sys')

            print('------------------------------- Service sys -------------------------------')
            print(f"Search path：\n{chr(10).join(self.sys.path)}")
            print(f"Project Root path：{os.path.abspath(os.curdir)}")

            print('------------------------------- Plugin sys -------------------------------')
            print(f"Search path：\n{chr(10).join(sys.path)}")
            print(f"Project Root path：{os.path.abspath(os.curdir)}")


@dataclass(frozen=True)
class TaskCmd:
    op: str          # "reload" | "remove" | "shutdown"
    path: str        # file path (for reload/remove)
    seq: int         # monotonically increasing sequence
    sig: Optional[str] = None   # 文件内容签名,reload 成功后才更新到 self._sig


class TaskManager:
    THREAD_JOIN_TIMEOUT = 2
    THREAD_JOIN_ATTEMPTS = 10
    # manager 管理循环的空闲唤醒周期：用于检查延迟重载（pending restart）补启动
    PENDING_CHECK_INTERVAL_SEC = 2.0

    def __init__(self, watch_dir: str, security_config=None):
        self.watch_dir = watch_dir
        self.security = security_config

        self.config = EasyConfig(DEFAULT_CONFIG_FILE)

        # 调度器单独持有引用，便于创建治理器后回注控制信号（修复 PAUSE/IMMEDIATE 无效问题）
        scheduler = FlowScheduler(max_concurrency=5, startup_stagger=10.0)
        self.crawler_governance = GovernanceManager(
            db_path=os.path.join(DATA_PATH, 'spider_governance.db'),
            files_path=os.path.join(DATA_PATH, 'spider_governance_files'),
            scheduler=scheduler
        )
        # 把治理控制信号注入调度器睡眠循环：PAUSE 即时冻结倒计时，IMMEDIATE 立即唤醒
        scheduler.set_signal_provider(self.crawler_governance.poll_sleep_signal)

        self.plugin_manager = PluginManager(['module_init', 'start_task'])

        self._sig = {}  # plugin_name -> sig

        # {plugin_name: (PluginWrapper, Thread, Event)}
        self.tasks: Dict[str, Tuple[PluginWrapper, threading.Thread, threading.Event]] = {}

        # 延迟重载登记：{plugin_name: (PluginWrapper, sig)}
        # 旧线程 join 超时未退出时，新模块先加载到 PluginManager 但不启动，
        # 由 manager 管理循环定期检查，旧线程退出后补启动（避免静默丢失）。
        self._pending_restart: Dict[str, Tuple[PluginWrapper, Optional[str]]] = {}

        # command queue / manager thread
        self._q: queue.Queue[TaskCmd] = queue.Queue()
        self._seq = 0
        self._stop_mgr = threading.Event()
        self._mgr_thread = threading.Thread(target=self._manager_loop, name="TaskManagerThread", daemon=True)
        self._mgr_thread.start()

        self.scan_existing_files()

    # ---------------- public APIs: NON-BLOCKING ----------------

    @staticmethod
    def _is_excluded_task(file_path: str) -> bool:
        """task_example* 为插件编写示例/模板，不是生产任务。

        约定：示例文件一律以 task_example 前缀命名（见 CrawlTasks/task_example.py），
        在命名规范层面排除，不参与生产加载与热重载。
        """
        return os.path.basename(file_path).startswith('task_example')

    @staticmethod
    def _is_config_file(file_path: str) -> bool:
        """crawler_config_*.py 是任务的配置文件，本身不是可加载插件。"""
        return os.path.basename(file_path).startswith('crawler_config_')

    def submit_reload(self, file_path: str, source: str):
        """For created/modified/moved-in events."""
        if self._is_excluded_task(file_path):
            return

        abs_path = os.path.abspath(file_path)
        name = PluginManager.plugin_name(abs_path)

        try:
            sig = _file_sig(abs_path)
        except FileNotFoundError:
            return
        if self._sig.get(name) == sig:
            logger.info(f"[CMD] skip reload (content unchanged) {abs_path}")
            return
        # 注意:不能在这里更新 self._sig[sig],否则新版本加载失败后
        # 该签名已被记录,后续文件再变动也不会触发重载。改为加载成功后更新。

        if self._is_config_file(abs_path):
            # crawler_config_X.py 不是插件（无 module_init/start_task），直接当插件重载必然失败。
            # 取舍说明：配置与任务的依赖关系并非严格按命名一一对应
            # （如 crawler_config_nhk 被 task_crawl_nhk_ic 引用），静态解析 import
            # 代价高且易漏；配置变更属于低频人工操作，故采用"配置变更 → 重载全部任务"
            # 的保守策略，代价是全部任务（含 Playwright 浏览器）重启一次。
            self._enqueue("reload_all_tasks", file_path, source, sig=sig)
            return

        self._enqueue("reload", file_path, source, sig=sig)

    def submit_remove(self, file_path: str, source: str):
        """For deleted/moved-out events."""
        if self._is_excluded_task(file_path):
            return
        if self._is_config_file(file_path):
            # 配置文件删除不卸载任何任务（任务下次重载时自然感知）
            logger.info(f"[CMD] skip remove for config file {file_path}")
            return
        self._enqueue("remove", file_path, source)

    def shutdown(self, source: str = "fs"):
        """Stop all tasks & manager thread."""
        self._enqueue("shutdown", "", source)
        self._mgr_thread.join(timeout=10)

    # ---------------- internal: queue & manager ----------------

    def _enqueue(self, op: str, path: str, source: str, sig: Optional[str] = None):
        self._seq += 1
        logger.info(f"[CMD] seq={self._seq} op={op} source={source} path={path}")
        self._q.put(TaskCmd(op=op, path=path, seq=self._seq, sig=sig))

    def _manager_loop(self):
        """
        Single writer:
          - Only this thread mutates self.tasks and calls plugin_manager add/remove.
        Coalescing:
          - Drain queue and keep the last cmd per plugin_name.
        """
        while not self._stop_mgr.is_set():
            try:
                # 带超时的阻塞：周期性唤醒以检查延迟重载（pending restart）补启动
                cmd = self._q.get(timeout=self.PENDING_CHECK_INTERVAL_SEC)
            except queue.Empty:
                self._check_pending_restarts()
                continue
            if cmd.op == "shutdown":
                self._handle_shutdown()
                self._stop_mgr.set()
                break

            # Drain more commands quickly to coalesce storms
            batch = [cmd]
            try:
                while True:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                # 队列取空是批量收集命令的正常结束信号（有意忽略，非错误）
                pass

            # Keep only the last cmd for each plugin_name
            last_by_name: Dict[str, TaskCmd] = {}
            for c in batch:
                if c.op == "shutdown":
                    # prioritize shutdown immediately
                    self._handle_shutdown()
                    self._stop_mgr.set()
                    return
                name = PluginManager.plugin_name(c.path)
                prev = last_by_name.get(name)
                if prev is None or c.seq > prev.seq:
                    last_by_name[name] = c

            # 配置变更（reload_all_tasks）与普通插件命令分开处理：
            # 同一批次多个配置文件变更只触发一次全量重载
            reload_all_cmds = [c for c in last_by_name.values() if c.op == "reload_all_tasks"]
            normal_cmds = [c for c in last_by_name.values() if c.op != "reload_all_tasks"]

            # Apply in seq order for determinism (optional)
            for c in sorted(normal_cmds, key=lambda it: it.seq):
                name = PluginManager.plugin_name(c.path)
                try:
                    if c.op == "remove":
                        self._do_remove(name)
                    elif c.op == "reload":
                        self._do_reload(name, c.path, c.sig)
                    else:
                        logger.warning(f"Unknown op: {c.op}")
                except Exception as e:
                    logger.error(f"Manager op {c.op}({c.path}) failed: {e}", exc_info=True)

            if reload_all_cmds:
                try:
                    self._do_reload_all_tasks(reload_all_cmds)
                except Exception as e:
                    logger.error(f"Manager op reload_all_tasks failed: {e}", exc_info=True)

    # ---------------- internal: operations (serialized) ----------------

    def _do_reload(self, plugin_name: str, file_path: str, sig: Optional[str] = None):
        # 先卸载旧模块(仅从 PluginManager 字典与 sys.modules 移除)。
        # 运行中的旧线程仍持有旧的 module_inst 引用,可以继续执行,
        # 因此这一步不会打断正在运行的任务。
        self.plugin_manager.remove_plugin(plugin_name)

        # 先加载新版本,成功后才会停旧任务:
        # 若新版本语法错误/导入失败,add_plugin 返回 None,旧任务继续运行,
        # 且 self._sig 未更新,下次文件变动仍会再次触发 reload。
        plugin = self.plugin_manager.add_plugin(file_path)
        if not plugin:
            logger.error(f"Load plugin failed: {file_path}")
            return

        # 新版本加载成功,再停旧线程
        if not self._stop_and_join_if_running(plugin_name):
            # 旧线程 join 超时(如阻塞在 Playwright 调用中)。
            # 新模块已加载到 manager,登记 pending 状态;manager 管理循环
            # 会定期检查,旧线程退出后由 _check_pending_restarts 补启动。
            self._pending_restart[plugin_name] = (plugin, sig)
            logger.info(f"Defer reload for {plugin_name}, old thread still running; pending restart registered.")
            return

        # 若之前有同名 pending（极端情况：连续两次 defer），以最新模块为准
        self._pending_restart.pop(plugin_name, None)
        self._start_plugin_task(plugin, sig)

    def _start_plugin_task(self, plugin: PluginWrapper, sig: Optional[str] = None):
        """为已加载成功的插件启动任务线程并登记签名。"""
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.__drive_module,
            name=f"PluginThread-{plugin.plugin_name}",
            args=(plugin, stop_event),
            daemon=True
        )
        self.tasks[plugin.plugin_name] = (plugin, thread, stop_event)
        thread.start()

        # 只有加载并启动成功后,才更新签名,避免失败后新版本永不重载
        if sig:
            self._sig[plugin.plugin_name] = sig

        logger.info(f"Reloaded & started plugin: {plugin.plugin_name}")

    def _check_pending_restarts(self):
        """管理循环周期检查：延迟重载的旧线程一旦退出，立即补启动新模块。"""
        for name, (plugin, sig) in list(self._pending_restart.items()):
            try:
                # _stop_and_join_if_running 在线程已退出时清理旧 entry 并释放旧模块资源
                if self._stop_and_join_if_running(name):
                    self._pending_restart.pop(name, None)
                    self._start_plugin_task(plugin, sig)
                    logger.info(f"Pending restart completed for plugin: {name}")
            except Exception as e:
                logger.error(f"Pending restart for {name} failed: {e}", exc_info=True)

    def _do_reload_all_tasks(self, cmds: list):
        """crawler_config_*.py 变更后重载全部任务（配置缓存清除 + 逐个 reload）。"""
        # 1) 清除配置模块的 import 缓存,确保任务重新 import 时读到新文件内容
        #    （__remove_plugin 只删插件自身的 plugin_<path> 键，够不到 CrawlTasks.* 配置模块）
        pkg_name = os.path.basename(os.path.normpath(self.watch_dir))
        PluginManager.purge_modules_by_prefix(f"{pkg_name}.crawler_config_")

        # 2) 记录配置文件签名（配置文件不是插件，单独记录，避免相同内容的保存事件重复触发）
        for c in cmds:
            if c.sig:
                self._sig[PluginManager.plugin_name(c.path)] = c.sig

        # 3) 重载全部已登记任务
        for name, entry in list(self.tasks.items()):
            plugin = entry[0]
            self._do_reload(name, plugin.module_path, sig=None)

    def _do_remove(self, plugin_name: str):
        self._stop_and_join_if_running(plugin_name)
        # unload AFTER join
        self.plugin_manager.remove_plugin(plugin_name)
        # 清除内容签名：否则删除后再放回同内容文件时，签名比对会误判为"未变化"而跳过加载
        self._sig.pop(plugin_name, None)
        # 丢弃该插件可能存在的延迟重载登记
        self._pending_restart.pop(plugin_name, None)
        logger.info(f"Removed plugin: {plugin_name}")

    def _handle_shutdown(self):
        self._pending_restart.clear()

        # 先向全部任务发出停止信号（所有线程并行开始退出），
        # 再逐个 join——原实现串行"set→join(最坏20s)"，17 个任务总耗时叠加；
        # 现在等待在墙钟时间上重叠，总耗时≈最慢的单个任务。
        for name, entry in list(self.tasks.items()):
            entry[2].set()  # stop_event.set()

        names = list(self.tasks.keys())
        for name in names:
            self._stop_and_join_if_running(name)
            self.plugin_manager.remove_plugin(name)
        logger.info("TaskManager shutdown complete.")

        # 共享浏览器池（Scraper/PlaywrightRawScraper）跨热重载保留属设计，
        # 仅在服务整体关停时整池关闭；热重载路径（_release_plugin_resources）不调用。
        # 局部导入：避免引擎启动即依赖 playwright；关停失败只记 warning，不阻断退出。
        try:
            from Scraper.PlaywrightRawScraper import close_shared_browsers
            closed = close_shared_browsers()
            if closed:
                logger.info(f"Shared browser pool closed on engine shutdown: {closed} browser(s).")
        except Exception as e:
            logger.warning(f"Close shared browsers on engine shutdown failed: {e}")

    def _stop_and_join_if_running(self, plugin_name: str) -> bool:
        entry = self.tasks.get(plugin_name)
        if not entry:
            return True

        plugin, thread, stop_event = entry
        stop_event.set()

        for _ in range(self.THREAD_JOIN_ATTEMPTS):
            thread.join(timeout=self.THREAD_JOIN_TIMEOUT)
            if not thread.is_alive():
                self.tasks.pop(plugin_name, None)
                # P1-五 修复：任务线程退出后释放插件持有的重资源。
                # 原实现 17 个任务无一实现 module_fini，且 CrawlPipeline.shutdown()
                # 无调用方 → 每次热重载泄漏 Playwright Chromium 实例。
                # 兜底策略（按优先级）：
                #   1) 模块定义了 module_fini() —— 显式调用；
                #   2) 模块属性中存在 pipeline / flow / crawler 等对象且带
                #      shutdown()/close() 方法 —— 自动调用。
                self._release_plugin_resources(plugin)
                return True

        logger.warning(f"Plugin {plugin.plugin_name} thread (ID:{thread.ident}) still alive after join attempts.")
        return False

    @staticmethod
    def _release_plugin_resources(plugin) -> None:
        """释放插件持有的重资源（Playwright 浏览器、HTTP 会话等）。"""
        module = getattr(plugin, 'module_inst', None)
        if module is None:
            return

        # 1) 显式清理钩子
        fini = getattr(module, 'module_fini', None)
        if callable(fini):
            try:
                fini()
                logger.info(f"Plugin {plugin.plugin_name}: module_fini() executed.")
            except Exception as e:
                logger.warning(f"Plugin {plugin.plugin_name}: module_fini() failed: {e}")

        # 2) 兜底：自动发现带 shutdown()/close() 的资源对象。
        #    - 属性名匹配大小写不敏感：任务模块常用大写常量（如 FLOW）持有资源；
        #    - 支持一层嵌套探查：如 FLOW(CommonIntelligenceCrawlFlow) 自身没有
        #      shutdown/close，但其 .pipeline(CrawlPipeline) 有 shutdown()，
        #      可级联关闭内部 PlaywrightFetcher 的常驻 Chromium。
        resource_attr_names = ('pipeline', 'flow', 'crawler', 'browser', 'playwright', 'scraper')
        close_method_names = ('shutdown', 'close')

        def _try_close(obj, path: str) -> bool:
            """对单个对象尝试调用一个关闭方法，调用过（无论成败）即返回 True。"""
            for close_name in close_method_names:
                close_fn = getattr(obj, close_name, None)
                if callable(close_fn):
                    try:
                        close_fn()
                        logger.info(f"Plugin {plugin.plugin_name}: {path}.{close_name}() executed.")
                    except Exception as e:
                        logger.warning(f"Plugin {plugin.plugin_name}: {path}.{close_name}() failed: {e}")
                    return True  # 同一对象只调用一个关闭方法
            return False

        closed_ids = set()  # 防止同一对象被重复关闭（如 FLOW 与 FLOW.pipeline 均命中）

        for attr_name in dir(module):
            if attr_name.lower() not in resource_attr_names:
                continue
            try:
                obj = getattr(module, attr_name, None)
            except Exception as e:
                # 属性访问异常（如模块加载一半的残留对象）按无该资源处理，跳过即可
                logger.debug(f"Scan plugin resource attr {attr_name} failed: {e}")
                continue
            if obj is None or id(obj) in closed_ids:
                continue
            if _try_close(obj, attr_name):
                closed_ids.add(id(obj))
                continue
            # 嵌套探查：对象自身无关闭方法时，向下找一层资源属性
            for sub_name in dir(obj):
                if sub_name.lower() not in resource_attr_names:
                    continue
                try:
                    sub_obj = getattr(obj, sub_name, None)
                except Exception as e:
                    # 子属性访问异常按无该资源处理，跳过即可
                    logger.debug(f"Scan plugin resource attr {attr_name}.{sub_name} failed: {e}")
                    continue
                if sub_obj is None or id(sub_obj) in closed_ids:
                    continue
                if _try_close(sub_obj, f"{attr_name}.{sub_name}"):
                    closed_ids.add(id(sub_obj))

    # ---------------- existing behaviors ----------------

    def scan_existing_files(self):
        try:
            os.makedirs(self.watch_dir, exist_ok=True)
            plugins = self.plugin_manager.scan_path(self.watch_dir)
            # use submit_reload to unify behavior (serialize through manager)
            for p in plugins:
                self.submit_reload(p.module_path, 'scan')
            # 配置文件不是插件（scan_path 也不会收录），仅记录内容签名用于后续变更检测，
            # 避免启动时对每个 crawler_config_* 都触发一次全量任务重载
            for fname in os.listdir(self.watch_dir):
                if self._is_config_file(fname):
                    fpath = os.path.join(self.watch_dir, fname)
                    try:
                        self._sig[PluginManager.plugin_name(fpath)] = _file_sig(fpath)
                    except FileNotFoundError:
                        # 扫描与抓取并发的竞态：签名前文件已被移走属正常，跳过
                        pass
        except Exception as e:
            logger.error(f"Scan directory {self.watch_dir} crashed: {e}", exc_info=True)

    def on_model_enter(self, plugin: PluginWrapper):
        logger.info(f">>> Plugin {plugin.plugin_name} in thread {threading.get_ident()} plugin_obj={id(plugin)} - started.")

    def on_model_quit(self, plugin: PluginWrapper):
        logger.info(f"<<< Plugin {plugin.plugin_name} in thread {threading.get_ident()} plugin_obj={id(plugin)} - terminated.")

    def __drive_module(self, plugin: PluginWrapper, stop_event: threading.Event):
        self.on_model_enter(plugin)

        module_logger = logging.getLogger(plugin.plugin_name)
        old_logger = set_tls_logger(module_logger)

        # 热旋防护：连续失败计数 + 指数退避（1s 封顶 60s），
        # 连续 MAX_PLUGIN_CONSECUTIVE_FAILURES 次失败后置 FAILED 状态停止驱动，
        # 避免插件抛异常后 tight loop 刷爆日志。
        consecutive_failures = 0
        max_consecutive_failures = int(getattr(self.config, "max_plugin_consecutive_failures", 10) or 10)
        backoff_sec = 1.0

        try:
            plugin.module_init(ServiceContext(
                module_logger=module_logger,
                module_config=self.config,
                crawler_governor=self.crawler_governance
            ))

            # 约定：start_task 应该“短阻塞/一次迭代”，并检查 stop_event
            while not stop_event.is_set():
                try:
                    plugin.start_task(stop_event)
                    consecutive_failures = 0
                    backoff_sec = 1.0
                except Exception as e:
                    consecutive_failures += 1
                    logger.error(
                        f"Plugin {plugin.plugin_name} start_task crashed "
                        f"({consecutive_failures}/{max_consecutive_failures}): {e}",
                        exc_info=True
                    )
                    if consecutive_failures >= max_consecutive_failures:
                        logger.error(
                            f"Plugin {plugin.plugin_name} reached max consecutive failures; "
                            f"marking FAILED and stopping driver."
                        )
                        setattr(plugin, "state", "FAILED")
                        break
                    stop_event.wait(backoff_sec)
                    backoff_sec = min(backoff_sec * 2, 60.0)

        except Exception as e:
            logger.error(f"Plugin {plugin.plugin_name} crashed: {e}", exc_info=True)
        finally:
            set_tls_logger(old_logger)

        # 清理钩子:模块可选实现 module_fini() 释放 Playwright 浏览器/文件句柄等资源,
        # 热重载与关闭时均调用。PluginWrapper.invoke 内部已捕获所有异常。
        try:
            if plugin.has_function('module_fini'):
                plugin.invoke('module_fini')
        except Exception as e:
            logger.error(f"Plugin {plugin.plugin_name} module_fini crashed: {e}", exc_info=True)

        self.on_model_quit(plugin)


class FileHandler(FileSystemEventHandler):
    def __init__(self, task_manager, debounce_ms=300):
        self.task_manager = task_manager
        self.debounce_ms = debounce_ms
        self._last_ts = {}
        self._lock = threading.Lock()
        self._sig = {}  # plugin_name -> (mtime_ns, size)

    def on_created(self, event):
        if self.__file_accept(event) and self._debounced(event.src_path):
            self._log_event("accepted", event)
            self.task_manager.submit_reload(event.src_path, 'fs')

    def on_modified(self, event):
        if self.__file_accept(event) and self._debounced(event.src_path):
            self._log_event("accepted", event)
            self.task_manager.submit_reload(event.src_path, 'fs')

    def on_deleted(self, event):
        if self.__file_accept(event) and self._debounced(event.src_path):
            self._log_event("accepted", event)
            self.task_manager.submit_remove(event.src_path, 'fs')

    def on_moved(self, event):
        if event.is_directory:
            return
        if self.__file_accept(event) and self._debounced(event.src_path):
            self._log_event("accepted", event)
            self.task_manager.submit_remove(event.src_path, 'fs')
        if hasattr(event, "dest_path") and event.dest_path.endswith(".py") and self._debounced(event.dest_path):
            self._log_event("accepted", event)
            self.task_manager.submit_reload(event.dest_path, 'fs')

    def _debounced(self, path: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_ts.get(path, 0)
            if (now - last) * 1000 < self.debounce_ms:
                return False
            self._last_ts[path] = now
            return True

    def _log_event(self, tag, event):
        try:
            st = os.stat(event.src_path)
            logger.info(f"[FS] {tag} type={event.event_type} src={event.src_path} "
                        f"mtime_ns={st.st_mtime_ns} size={st.st_size}")
        except FileNotFoundError:
            logger.info(f"[FS] {tag} type={event.event_type} src={event.src_path} (missing)")

    @staticmethod
    def __file_accept(event) -> bool:
        return (not event.is_directory and
                not os.path.basename(event.src_path).startswith(('~', '.')) and
                event.src_path.endswith('.py'))


# ----------------------------------------------------------------------------------------------------------------------

CRAWL_LOG_FILE = os.path.join(LOG_PATH, 'crawls.log')
HISTORY_LOG_FOLDER = os.path.join(LOG_PATH, 'crawls_history_log')


def config_log_level():
    # Disable 3-party library's log
    limit_logger_level("asyncio")
    limit_logger_level("werkzeug")
    limit_logger_level("pymongo.topology")
    limit_logger_level("pymongo.connection")
    limit_logger_level("pymongo.serverSelection")
    limit_logger_level('urllib3.util.retry')
    limit_logger_level('urllib3.connectionpool')

    # My modules


def main():
    Path(LOG_PATH).mkdir(parents=True, exist_ok=True)
    Path(HISTORY_LOG_FOLDER).mkdir(parents=True, exist_ok=True)

    # ------------------------------------ Logger ------------------------------------

    backup_and_clean_previous_log_file(CRAWL_LOG_FILE, HISTORY_LOG_FOLDER)
    setup_logging(CRAWL_LOG_FILE)

    config_log_level()

    log_backend = LoggerBackend(monitoring_file_path=CRAWL_LOG_FILE, cache_limit_count=100000,
                                link_file_roots={
                                    'conversation': os.path.abspath('conversation')
                                },
                                project_root=project_root,
                                with_logger_manager=True)
    log_backend.start_service(port=18000)

    # --------------------------------- Main Service ---------------------------------

    # 使用绝对路径,避免启动时 CWD 变化导致监视目录失效
    crawl_task_path = os.path.join(project_root, 'CrawlTasks')

    task_manager = TaskManager(crawl_task_path)

    # 热重载:监视 CrawlTasks 目录,文件新增/修改/删除自动 reload。
    # FileHandler 带 300ms 防抖 + TaskManager 内按文件签名去重,避免重复触发。
    event_handler = FileHandler(task_manager)

    observer = Observer()
    observer.schedule(event_handler, path=crawl_task_path, recursive=False)
    observer.start()

    # 治理后台鉴权 token：环境变量 CRAWLER_GOVERNANCE_TOKEN 优先，
    # 其次取配置 crawler.governance_token；未配置时后端 fail-closed 拒绝受保护端点
    governance_token = os.environ.get("CRAWLER_GOVERNANCE_TOKEN") or \
                       (task_manager.config.get("crawler.governance_token") or "")
    if not governance_token:
        logger.warning("Governance token not configured "
                       "(env CRAWLER_GOVERNANCE_TOKEN / config crawler.governance_token); "
                       "governance API will reject protected endpoints.")
    governance_backend = CrawlerGovernanceBackend(task_manager.crawler_governance, token=governance_token)
    governance_backend.start_service(blocking=False)

    # --------------------------------------------------------------------------------

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        # Ctrl+C 优雅退出主循环（有意忽略，由 finally 收尾）
        pass
    finally:
        observer.stop()
        observer.join()
        task_manager.shutdown()
        # 注:CrawlerGovernanceBackend / LoggerBackend 均无 stop_service 方法,
        # 它们使用 daemon 线程随进程退出自动终止,故保持注释。
        # governance_backend.stop_service()
        # log_backend.stop_service()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(e)
        traceback.print_exc()
