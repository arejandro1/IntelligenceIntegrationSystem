#!/usr/bin/env python3
"""
Universal WSGI Server Management and Monitoring Script
Function: Starts the IntelligenceHubLauncher application using either Waitress or Gunicorn,
          monitors its health status, and automatically restarts when necessary.
Design: Uses adapter pattern to support multiple WSGI servers with common interface.
"""

import os
import sys
import time
import logging
import requests
import threading
import platform
import subprocess
from abc import ABC, abstractmethod

from GlobalConfig import LOG_PATH
# P1-7 修复：删除顶层导入 IntelligenceHubStartup。
# 该模块在模块级执行 run()（初始化整个 Hub：AI 线程、定时任务等）。
# 顶层导入 + Gunicorn worker 再 import = 两个进程各建一份 Hub，
# 双份 AI 线程重复烧 token、双份定时任务。
# 现在：仅 Waitress/Flask 适配器在 start_server() 时惰性导入；
# Gunicorn 路径由 gunicorn 子进程自行 import（天然单实例）。

# ==================== CONFIGURATION SECTION ====================

# Server selection (auto-detection with manual override)
SERVER_TYPE = None  # Set to 'waitress', 'gunicorn', or None for auto-detection

# =================== Common server parameters ===================

HOST = "0.0.0.0"
PORT = 5000
THREADS = 4
WORKERS = 1

# ================== Gunicorn server parameters ==================

WORKER_CLASS = 'gevent'  # Worker type (gevent for async)
ACCESS_LOG = './access.log'  # Gunicorn access log
ERROR_LOG = './error.log'  # Gunicorn error log
MANAGER_LOG = './server_manager.log'  # Manager application log

# Application configuration
FLASK_APP_MODULE = "IntelligenceHubStartup"  # Your Python file name
FLASK_APP_INSTANCE = "wsgi_app"  # Your Flask app instance name
# P0 部署缺陷修复：业务路由注册在 create_wsgi_app() 中（模块级 wsgi_app 仅含 /static）。
# gunicorn 直接加载 wsgi_app 会导致全部业务路由 404，故改用 app factory 语法
# "module:create_wsgi_app()"（gunicorn 支持 factory 调用）。
WSGI_APP_FACTORY = f"{FLASK_APP_MODULE}:create_wsgi_app()"


# ================== Health check configuration ==================

# 探活端点分工（P0-1 修订）：
# - HEALTH_CHECK_URL 默认带 deep=1（业务就绪探活）：检查按运行模式的
#   消费者线程（legacy AI 线程 / Drainer 线程 / 无消费者为预期状态）、
#   后处理线程、MongoDB 可用性与队列积压；等待非高峰/预算耗尽/DRY_RUN
#   不判故障。全部本地检查，短超时，不调用付费 API。
# - Docker HEALTHCHECK 使用浅探活 /maintenance/ping（进程存活），
#   与本脚本的 deep 探活分工明确，避免只采集/DRY_RUN/Drainer 模式下
#   的重启循环。
HEALTH_CHECK_URL = "http://127.0.0.1:5000/maintenance/ping?deep=1"
HEALTH_CHECK_TIMEOUT = 10
CHECK_INTERVAL = 30
RESTART_COOLDOWN = 300  # Seconds between restart attempts
# 深度健康检查需连续失败达到阈值才判定服务异常，避免单次抖动/慢响应触发误重启
HEALTH_CHECK_FAIL_THRESHOLD = 3

# Restart configuration
MAX_RESTART_ATTEMPTS = 5
RESTART_WINDOW_SEC = 3600  # 滑动窗口：1 小时内最多重启 MAX_RESTART_ATTEMPTS 次，超限退出交外部 supervisor

# ======================= Log configuration =======================

LOG_LEVEL = logging.INFO
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = "./server_manager.log"

# ====================== Platform detection ======================

IS_WINDOWS = platform.system().lower() == 'windows'


class ServerAdapter(ABC):
    """Abstract base class for WSGI server adapters"""

    def __init__(self, host, port, workers, threads):
        self.host = host
        self.port = port
        self.workers = workers
        self.threads = threads
        self.process = None
        self.server_thread = None
        self.server_running = False
        # 保存可停止的底层 server 对象（werkzeug make_server / waitress create_server）
        self._server = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def start_server(self):
        """Start the WSGI server"""
        pass

    @abstractmethod
    def stop_server(self):
        """Stop the WSGI server"""
        pass

    @abstractmethod
    def get_server_info(self):
        """Get server information for logging"""
        pass

    def is_running(self):
        """Check if server is running"""
        return self.server_running


# ==================== END CONFIGURATION ====================

class FlaskAppManager(ServerAdapter):
    """
    Adapter for Flask's built-in development server.
    Note: This is not suitable for production deployment.
    """

    def __init__(self, host, port, workers, threads):
        """
        Initialize Flask development server adapter.

        Args:
            host (str): Host address to bind to
            port (int): Port number to listen on
            workers (int): Not used in Flask dev server (kept for interface compatibility)
            threads (int): Not used in Flask dev server (kept for interface compatibility)
        """
        super().__init__(host, port, workers, threads)
        self.host = host
        self.port = port
        # 调试模式默认关闭，避免未捕获异常暴露 Werkzeug 交互式调试器。
        # 仅在显式设置 FLASK_DEBUG=1/true/yes/on 时开启（仅限开发环境）。
        self.debug = os.getenv('FLASK_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
        self.logger = logging.getLogger("FlaskAppManager")

    def start_server(self):
        """
        Start Flask development server.

        Returns:
            bool: True if server started successfully, False otherwise
        """
        try:
            self.logger.info(f"Starting Flask development server on {self.host}:{self.port}")
            self.logger.info(f"Flask server configuration: {self.get_server_info()}")
            self.logger.warning("Flask development server is for testing purposes only - not for production use")

            # Run Flask server in a separate thread
            def run_flask_server():
                try:
                    # P0 部署缺陷修复：经 create_wsgi_app() 引导后再 serve
                    from IntelligenceHubStartup import create_wsgi_app
                    app = create_wsgi_app()
                    # 用 make_server 持有 server 对象，stop_server() 可调用 shutdown()
                    # 释放端口，避免旧线程残留导致重启失败（Address already in use）。
                    from werkzeug.serving import make_server
                    self._server = make_server(
                        self.host,
                        self.port,
                        app,
                        threaded=True,
                    )
                    self._server.serve_forever()
                except Exception as e:
                    self.logger.error(f"Flask server error: {str(e)}")
                finally:
                    self.server_running = False
                    self._server = None

            self.server_thread = threading.Thread(target=run_flask_server)
            self.server_thread.daemon = True
            self.server_thread.start()
            self.server_running = True

            # Wait for server to start
            time.sleep(3)

            # Verify server is running
            if self.server_thread.is_alive():
                self.logger.info("Flask development server started successfully")
                return True
            else:
                self.logger.error("Flask server thread failed to start")
                return False

        except Exception as e:
            self.logger.error(f"Failed to start Flask server: {str(e)}")
            return False

    def stop_server(self):
        """
        Stop Flask development server.
        通过 make_server 返回的 server.shutdown() 使 serve_forever() 退出，
        并 join 等待旧线程结束，确保端口被释放后才允许重启。
        """
        self.server_running = False
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception as e:
                self.logger.warning(f"Flask server shutdown error: {str(e)}")
        if self.server_thread is not None and self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
        self.logger.info("Flask development server stopped")

    def get_server_info(self):
        """
        Get Flask server configuration information.

        Returns:
            str: Server configuration details
        """
        return f"host={self.host}, port={self.port}, debug={self.debug}"


class WaitressServer(ServerAdapter):
    """Adapter for Waitress server"""

    def start_server(self):
        """Start Waitress server"""
        try:
            from waitress import serve

            self.logger.info(f"Starting Waitress server on {self.host}:{self.port}")
            self.logger.info(f"Waitress configuration: {self.get_server_info()}")

            # Run Waitress in a separate thread
            def run_waitress():
                try:
                    # P1-7 修复：惰性导入，避免模块导入阶段初始化整个 Hub
                    # P0 部署缺陷修复：必须经 create_wsgi_app() 引导（注册全部业务路由），
                    # 直接 import wsgi_app 只有 /static 路由。
                    from IntelligenceHubStartup import create_wsgi_app
                    app = create_wsgi_app()
                    # 用 create_server + run() 两步式启动，便于 stop_server() 调用
                    # close() 停止监听并释放端口。
                    from waitress.server import create_server
                    self._server = create_server(
                        app,
                        host=self.host,
                        port=self.port,
                        threads=self.threads,
                    )
                    self._server.run()
                except Exception as e:
                    self.logger.error(f"Waitress server error: {str(e)}")
                finally:
                    self.server_running = False
                    self._server = None

            self.server_thread = threading.Thread(target=run_waitress)
            self.server_thread.daemon = True
            self.server_thread.start()
            self.server_running = True

            # Wait for server to start
            time.sleep(3)
            self.logger.info("Waitress server started successfully")
            return True

        except ImportError:
            self.logger.error("Waitress not installed. Please install with: pip install waitress")
            return False
        except Exception as e:
            self.logger.error(f"Failed to start Waitress: {str(e)}")
            return False

    def stop_server(self):
        """Stop Waitress server"""
        self.server_running = False
        server = self._server
        if server is not None:
            try:
                server.close()
            except Exception as e:
                self.logger.warning(f"Waitress server close error: {str(e)}")
        if self.server_thread is not None and self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
        self.logger.info("Waitress server stopped")

    def get_server_info(self):
        """Get Waitress configuration info"""
        return f"host={self.host}, port={self.port}, threads={self.threads}"


class GunicornServer(ServerAdapter):
    """Adapter for Gunicorn server"""

    def start_server(self):
        """Start Gunicorn server using subprocess"""
        self._log_file = None
        try:
            # Build Gunicorn command
            gunicorn_cmd = [
                "gunicorn",
                "-b", f"{self.host}:{self.port}",
                "-w", str(self.workers),
                "-k", WORKER_CLASS,
                "--access-logfile", ACCESS_LOG,
                "--error-logfile", ERROR_LOG,
                "--pythonpath", ".",
                WSGI_APP_FACTORY
            ]

            self.logger.info(f"Starting Gunicorn server with command: {' '.join(gunicorn_cmd)}")
            self.logger.info(f"Gunicorn configuration: {self.get_server_info()}")

            # P0 修复：stdout/stderr 不能用 PIPE（无人读取时管道缓冲区写满会堵死子进程）。
            # 改为追加模式重定向到 _log 下的日志文件句柄。
            os.makedirs(LOG_PATH, exist_ok=True)
            self._log_file = open(
                os.path.join(LOG_PATH, 'gunicorn_child.log'), 'a', encoding='utf-8', buffering=1)

            # Start Gunicorn process
            self.process = subprocess.Popen(
                gunicorn_cmd,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                text=True
            )

            self.server_running = True
            self.logger.info(f"Gunicorn started successfully, PID: {self.process.pid}")

            # Wait for server to start
            time.sleep(5)
            return True

        except Exception as e:
            self.logger.error(f"Failed to start Gunicorn: {str(e)}")
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception as e:
                    # 关闭失败不影响启动失败的主流程（句柄由进程退出回收）
                    self.logger.debug(f"Close log file after Gunicorn start failed: {e}")
                self._log_file = None
            return False

    def stop_server(self):
        """Stop Gunicorn server"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
                self.logger.info("Gunicorn process terminated normally")
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.logger.warning("Gunicorn process forcibly terminated")
                except Exception as e:
                    self.logger.error(f"Failed to kill Gunicorn process: {str(e)}")
            except Exception as e:
                self.logger.error(f"Error terminating Gunicorn process: {str(e)}")
            finally:
                self.server_running = False
                # 关闭子进程输出重定向的日志文件句柄
                log_file = getattr(self, '_log_file', None)
                if log_file:
                    try:
                        log_file.close()
                    except Exception as e:
                        # 关闭失败不影响停止流程（句柄由进程退出回收）
                        self.logger.debug(f"Close log file during Gunicorn stop failed: {e}")
                    self._log_file = None
        else:
            self.logger.info("Gunicorn server stopped")

    def get_server_info(self):
        """Get Gunicorn configuration info"""
        return f"host={self.host}, port={self.port}, workers={self.workers}"


class ServerManager:
    """Main server manager that handles both Waitress and Gunicorn"""

    def __init__(self):
        self.logger = None
        self.server = None
        # 滑动窗口重启计数：记录最近 RESTART_WINDOW_SEC 内的重启时间戳
        self.restart_times = []
        self.last_restart_time = 0
        self.health_fail_count = 0  # 连续健康检查失败次数
        self.setup_logging()
        self.determine_server_type()

    def setup_logging(self):
        """Configure logging"""
        self.logger = logging.getLogger("ServerManager")

    def determine_server_type(self):
        """Determine which server to use based on configuration and platform"""
        global SERVER_TYPE
        server_type = SERVER_TYPE

        # Use explicitly configured server type if specified
        if server_type:
            server_type_lower = server_type.lower()
            if server_type_lower == 'waitress':
                self.server = WaitressServer(HOST, PORT, WORKERS, THREADS)
                self.logger.info(f"Using explicitly configured server: Waitress")
            elif server_type_lower == 'gunicorn':
                if IS_WINDOWS:
                    self.logger.warning("Gunicorn is not recommended on Windows. Consider using Waitress instead.")
                self.server = GunicornServer(HOST, PORT, WORKERS, THREADS)
                self.logger.info(f"Using explicitly configured server: Gunicorn")
            elif server_type_lower == 'flask':
                self.server = FlaskAppManager(HOST, PORT, WORKERS, THREADS)
                self.logger.info(f"Using explicitly configured server: Flask Development Server")
            else:
                self.logger.error(f"Unknown server type: {server_type}. Using auto-detection.")
                server_type = None

        # Auto-detect if not explicitly configured
        if not server_type:
            if IS_WINDOWS:
                self.server = WaitressServer(HOST, PORT, WORKERS, THREADS)
                self.logger.info("Auto-selected Waitress server (Windows platform)")
            else:
                # Try to use Gunicorn on Unix systems, fall back to Waitress if not available
                try:
                    import gunicorn
                    self.server = GunicornServer(HOST, PORT, WORKERS, THREADS)
                    self.logger.info("Auto-selected Gunicorn server (Unix platform)")
                except ImportError:
                    self.logger.warning("Gunicorn not available. Falling back to Waitress.")
                    self.server = WaitressServer(HOST, PORT, WORKERS, THREADS)

    def check_health(self):
        """Check service health status"""
        try:
            response = requests.get(HEALTH_CHECK_URL, timeout=HEALTH_CHECK_TIMEOUT)
            if response.status_code == 200:
                self.logger.debug("Health check successful")
                return True
            else:
                self.logger.warning(f"Health check returned non-200 status: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Health check request failed: {str(e)}")
            return False
        except Exception as e:
            self.logger.error(f"Unknown error during health check: {str(e)}")
            return False

    def restart_server(self):
        """Restart the server with cooldown and sliding-window attempt limits"""
        current_time = time.time()

        # Check restart cooldown
        if current_time - self.last_restart_time < RESTART_COOLDOWN:
            remaining = int(RESTART_COOLDOWN - (current_time - self.last_restart_time))
            self.logger.warning(f"Still in restart cooldown period, can restart in {remaining} seconds")
            return False

        # 滑动窗口上限：只统计最近 RESTART_WINDOW_SEC 内的重启次数，
        # 避免旧实现中计数永不衰减、累计 5 次后永久放弃的问题。
        self.restart_times = [t for t in self.restart_times
                              if current_time - t < RESTART_WINDOW_SEC]
        if len(self.restart_times) >= MAX_RESTART_ATTEMPTS:
            # 达到上限说明服务持续异常，进程内再重启无意义。
            # 以非零码退出主循环，交给 systemd Restart 策略 / 外部 supervisor 处理，
            # 而不是每 30 秒空打 warning。
            self.logger.error(
                f"滑动窗口（{RESTART_WINDOW_SEC}s）内重启已达上限 {MAX_RESTART_ATTEMPTS} 次，"
                f"服务持续异常，主程序退出（exit 1），交由外部 supervisor 处理。")
            sys.exit(1)

        self.logger.info("Attempting to restart server...")

        # Stop current server
        self.server.stop_server()

        # Start new server
        success = self.server.start_server()
        if success:
            self.restart_times.append(current_time)
            self.last_restart_time = current_time
            self.logger.info(
                f"Restart successful (window count {len(self.restart_times)}/{MAX_RESTART_ATTEMPTS})")
        else:
            self.logger.error("Restart failed")

        return success

    def run(self):
        """Main run loop"""
        self.logger.info("Universal WSGI Server Manager starting")
        self.logger.info(f"Platform: {platform.system()} {platform.release()}")
        self.logger.info(f"Health check URL: {HEALTH_CHECK_URL}")
        self.logger.info(f"Check interval: {CHECK_INTERVAL} seconds")
        self.logger.info(f"Max restart attempts: {MAX_RESTART_ATTEMPTS} per {RESTART_WINDOW_SEC}s sliding window")
        self.logger.info(f"Restart cooldown: {RESTART_COOLDOWN} seconds")

        # Initial startup
        if not self.server.start_server():
            # 初始启动失败必须以非零码退出，systemd/外部 supervisor 才能感知并介入
            self.logger.error("Initial startup failed, script exiting with code 1")
            sys.exit(1)

        # Main monitoring loop
        try:
            while True:
                # 深度健康检查：连续失败达到阈值才判定服务异常（避免单次抖动误重启）
                if not self.check_health():
                    self.health_fail_count += 1
                    self.logger.warning(
                        f"Health check failed ({self.health_fail_count}/{HEALTH_CHECK_FAIL_THRESHOLD})")
                else:
                    self.health_fail_count = 0
                health_broken = self.health_fail_count >= HEALTH_CHECK_FAIL_THRESHOLD

                if isinstance(self.server, GunicornServer):
                    # 子进程模式：重启会拉起全新进程重新 import IntelligenceHubStartup，
                    # Hub 可以真正重建，因此允许进程内重启。
                    process_dead = bool(self.server.process) and self.server.process.poll() is not None
                    if process_dead:
                        self.logger.error(f"Server process exited with code: {self.server.process.poll()}")
                    if process_dead or health_broken:
                        self.restart_server()
                        self.health_fail_count = 0
                else:
                    # 进程内模式（Waitress/Flask）：Hub 在 IntelligenceHubStartup 模块级
                    # run() 中初始化，restart 时 `from IntelligenceHubStartup import wsgi_app`
                    # 会命中 sys.modules 缓存，Hub 并不会重建——进程内“重启”是假重启。
                    # 因此选择更简单可靠的方案：检测到线程死亡或深度健康检查持续失败时，
                    # 以非零码退出整进程，交给 systemd/外部 supervisor 重启。
                    # 旧 Hub 的 shutdown 由 IntelligenceHubStartup 注册的 atexit 钩子完成。
                    thread_dead = bool(self.server.server_thread) and not self.server.server_thread.is_alive()
                    if thread_dead:
                        self.logger.error("Server thread has stopped")
                    if thread_dead or health_broken:
                        self.logger.error(
                            "进程内模式无法安全重建 Hub（sys.modules 缓存），"
                            "整进程退出（exit 1）交由外部 supervisor 重启。")
                        sys.exit(1)

                # Wait for next check
                time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            self.logger.info("Interrupt signal received, stopping server...")
        except Exception as e:
            self.logger.error(f"Unexpected error in monitoring loop: {str(e)}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources"""
        self.logger.info("Cleaning up resources...")
        self.server.stop_server()
        self.logger.info("Server manager exiting")


def create_server_adapter(server_type, host, port, workers, threads):
    """Factory function to create server adapters"""
    server_type_lower = server_type.lower()
    if server_type_lower == 'waitress':
        return WaitressServer(host, port, workers, threads)
    elif server_type_lower == 'gunicorn':
        return GunicornServer(host, port, workers, threads)
    elif server_type_lower == 'flask':
        return FlaskAppManager(host, port, workers, threads)
    else:
        raise ValueError(f"Unknown server type: {server_type}")


if __name__ == "__main__":
    manager = ServerManager()
    manager.run()
