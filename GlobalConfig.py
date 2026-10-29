import logging
import os


PRJ_PATH = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(PRJ_PATH, '_log')
DATA_PATH = os.path.join(PRJ_PATH, '_data')
CONFIG_PATH = os.path.join(PRJ_PATH, '_config')
EXPORT_PATH = os.path.join(PRJ_PATH, '_export')
PRODUCTS_PATH = os.path.join(PRJ_PATH, '_products')


def resolve_active_path(old_path: str, new_path: str) -> str:
    """
    Checks for the existence of a legacy path to determine the active working path.

    If the old path exists, it returns the old path and logs a warning to prompt
    the user for migration. Otherwise, it defaults to the new path.

    Args:
        old_path (str): The string representation of the legacy path (file or dir).
        new_path (str): The string representation of the new standard path.

    Returns:
        str: The path that should be used by the application.
    """
    import logging
    from pathlib import Path

    logger = logging.getLogger(__name__)

    # Use Path for robust cross-platform checks
    legacy_target = Path(old_path)

    if legacy_target.exists():
        # Log a warning to notify the user about the migration requirement
        logger.warning(
            f"Legacy path detected at '{old_path}'. "
            f"Please migrate your data to the new location: '{new_path}'."
        )
        # Return old path to maintain backward compatibility until migration
        return old_path

    # If old path implies no legacy data, use the new path
    return new_path


DEFAULT_PROXY = {
    "http": "socks5://127.0.0.1:10808",
    "https": "socks5://127.0.0.1:10808"
}


# Not that if you're using proxy or vpn. The situation will be opposite.
DEFAULT_INTERNAL_TIMEOUT_MS = 20000         # Internal network timeout
DEFAULT_NATIONAL_TIMEOUT_MS = 35000         # National network timeout


# 默认 token 从环境变量注入；未配置时为空字符串，配合 deny_on_empty_config fail-closed。
# 禁止使用硬编码弱凭证作为静默回退。
DEFAULT_RPC_API_TOKEN = os.getenv('DEFAULT_RPC_API_TOKEN', '')
DEFAULT_COLLECTOR_TOKEN = os.getenv('DEFAULT_COLLECTOR_TOKEN', '')
DEFAULT_PROCESSOR_TOKEN = os.getenv('DEFAULT_PROCESSOR_TOKEN', '')


CONFIG_FILE = 'config.json'
ALTER_CONFIG_FILE = 'config_example.json'
DEFAULT_CONFIG_FILE = resolve_active_path(CONFIG_FILE, os.path.join(CONFIG_PATH, CONFIG_FILE))
DEFAULT_ALTER_CONFIG_FILE = resolve_active_path(ALTER_CONFIG_FILE, os.path.join(CONFIG_PATH, ALTER_CONFIG_FILE))


USER_DB_FILE = 'Authentication.db'
# P0-2 修复：认证数据库必须位于可写目录。
# 历史路径有两处：项目根 Authentication.db 与 _config/Authentication.db。
# Docker 部署中 _config 以只读挂载（ro），SQLite 写登录日志（含 journal/WAL
# 临时文件）会失败，登录将不可用。新默认路径为 _data/Authentication.db
# （_data 是可写持久化挂载）。
#
# 注意（导入无副作用原则）：本函数只做路径解析，不执行文件复制。
# 旧库迁移由 migrate_legacy_user_db() 完成，由 UserManager 在实际打开
# 默认路径前调用（首次使用时迁移一次，幂等，失败抛错）。
#
# 解析优先级：
#   1. 环境变量 AUTH_DB_PATH（显式指定，容器/特殊部署用）；
#   2. _data/Authentication.db（新默认，可写）。
def resolve_user_db_path() -> str:
    env_path = os.getenv('AUTH_DB_PATH', '').strip()
    if env_path:
        return env_path
    return os.path.join(DATA_PATH, USER_DB_FILE)


DEFAULT_USER_DB_PATH = resolve_user_db_path()


def migrate_legacy_user_db(target_path: str = None,
                           legacy_paths: list = None) -> str:
    """P0-2：把旧认证库复制到新位置（幂等，返回最终路径）。

    - 目标已存在 → 直接返回（幂等，不覆盖）；
    - 旧库存在而目标不存在 → 复制（copy2，保留原文件）；
    - 复制失败 → 抛 RuntimeError，禁止静默新建空库导致账号丢失；
    - 均不存在 → 返回目标路径（首次部署，由 UserManager 建表）。

    legacy_paths 缺省为两个历史位置（_config/ 与项目根），
    测试可注入隔离路径。
    """
    import logging
    import shutil

    logger = logging.getLogger(__name__)

    target = target_path or DEFAULT_USER_DB_PATH
    if os.path.exists(target):
        return target

    if legacy_paths is None:
        legacy_paths = [
            os.path.join(CONFIG_PATH, USER_DB_FILE),   # 旧标准位置（只读挂载风险点）
            os.path.join(PRJ_PATH, USER_DB_FILE),      # 更早的项目根位置
        ]

    for legacy in legacy_paths:
        if not os.path.exists(legacy):
            continue
        try:
            target_dir = os.path.dirname(target)
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(legacy, target)
            logger.warning(
                "认证数据库已自动迁移：%s -> %s（旧文件保留，可确认后手动删除）。"
                "迁移原因：_config 在容器部署中通常只读挂载，SQLite 无法写入登录日志。",
                legacy, target)
            return target
        except OSError as e:
            raise RuntimeError(
                f"认证数据库迁移失败：{legacy} -> {target}：{e}。"
                f"请手动复制该文件到 {target}（或设置 AUTH_DB_PATH 环境变量"
                f"指向可写位置）后重启。拒绝在迁移失败时新建空库导致账号丢失。") from e

    return target


USING_VPN = True
USING_PROXY = False


if not USING_PROXY:
    APPLIED_PROXY = {}
else:
    APPLIED_PROXY = DEFAULT_PROXY


if USING_VPN or USING_PROXY:
    # 【语义说明：以下互换是有意为之，不是笔误】
    # 启用 VPN/代理后，出网流量实际经由代理隧道转发，两个方向的超时需求互换：
    # - 访问"内网(internal)"目标也要绕行代理隧道，原本 20s 的直连短超时不再适用，
    #   需改用更宽裕的 35000ms，避免代理抖动/慢启动误杀内网请求；
    # - 访问"国内公网(national)"目标反而经代理出口转发更稳定，可用较短的 20000ms。
    # 两值总量不变，仅方向互换（与文件顶部 DEFAULT_* 常量处注释呼应）。
    APPLIED_INTERNAL_TIMEOUT_MS = DEFAULT_NATIONAL_TIMEOUT_MS
    APPLIED_NATIONAL_TIMEOUT_MS = DEFAULT_INTERNAL_TIMEOUT_MS
else:
    APPLIED_INTERNAL_TIMEOUT_MS = DEFAULT_INTERNAL_TIMEOUT_MS
    APPLIED_NATIONAL_TIMEOUT_MS = DEFAULT_NATIONAL_TIMEOUT_MS

# 模块加载时输出当前生效的内外网超时，便于部署时排查"超时被互换"类问题。
# 使用下划线前缀命名，避免被 `from GlobalConfig import *` 泄漏到导入方命名空间。
_module_logger = logging.getLogger(__name__)
_module_logger.info(
    "超时配置生效：USING_VPN=%s, USING_PROXY=%s, 内网(internal)=%dms, 国内(national)=%dms"
    "（启用 VPN/代理时内外网超时有意互换，见 GlobalConfig 注释）",
    USING_VPN, USING_PROXY, APPLIED_INTERNAL_TIMEOUT_MS, APPLIED_NATIONAL_TIMEOUT_MS)


DEFAULT_IHUB_PORT = 5000
DEFAULT_MONGO_DB_URL = "mongodb://localhost:27017/"
DEFAULT_AI_PROCESSOR_URL = "http://localhost:5001/process"

MODEL_SILICON_FLOW_QWEN = 'Qwen/Qwen3-235B-A22B'
MODEL_OLLAMA_DEEP_SEEK_R1 = 'deepseek-r1:14b'
MODEL_OLLAMA_QWEN3_14B = 'qwen3:14b'
MODEL_SELECT = MODEL_OLLAMA_QWEN3_14B

OPEN_AI_API_BASE_URL_LOCAL_OLLAMA = "http://localhost:11434"
OPEN_AI_API_BASE_URL_SILICON_FLOW = "https://api.siliconflow.cn"
OPEN_AI_API_BASE_URL_SELECT = OPEN_AI_API_BASE_URL_LOCAL_OLLAMA
