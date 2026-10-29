#!/usr/bin/env bash
# China Watch 备份脚本（P1-8）
#
# 覆盖范围（对应 docs/synology_deployment.md 第 6 节）：
#   1. MongoDB 全库（含情报归档、AI 延期队列、费用台账、推送队列）；
#   2. 认证数据库 Authentication.db（SQLite，位于 _data/）；
#   3. config.json（不含密钥的副本需人工确认后再离线保存；
#      注意 config.json 含 token/密码，备份文件应加密或保存在受控目录）。
#
# 用法（在部署目录执行）：
#   ./Scripts/backup_china_watch.sh [备份目录]
#   备份目录默认 ./_export/backup
#
# 认证说明：读取 .env 中的 MONGO_USER/MONGO_PASSWORD/MONGO_HOST/MONGO_PORT
#（本地容器模式为 MONGO_INITDB_ROOT_*，脚本对两种变量名均兼容）。
#
# 恢复方法见脚本末尾注释与本文件 README 段落。
# 注意：恢复演练只允许在隔离测试库上进行，禁止直接对生产库操作。
set -euo pipefail

BACKUP_DIR="${1:-./_export/backup}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${BACKUP_DIR}/${STAMP}"

# --- 读取凭据（来自 .env，不回显） ---
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
MONGO_HOST="${MONGO_HOST:-china-watch-mongo}"
MONGO_PORT="${MONGO_PORT:-27017}"
MONGO_USER="${MONGO_USER:-${MONGO_INITDB_ROOT_USERNAME:-}}"
MONGO_PASSWORD="${MONGO_PASSWORD:-${MONGO_INITDB_ROOT_PASSWORD:-}}"

mkdir -p "${OUT_DIR}"
echo "[backup] 输出目录：${OUT_DIR}"

# --- 1) MongoDB 全库归档（gzip 压缩单文件） ---
echo "[backup] mongodump IntelligenceIntegrationSystem ..."
docker exec china-watch-mongo mongodump \
  --host "${MONGO_HOST}" --port "${MONGO_PORT}" \
  -u "${MONGO_USER}" -p "${MONGO_PASSWORD}" --authenticationDatabase admin \
  --db IntelligenceIntegrationSystem \
  --archive --gzip > "${OUT_DIR}/mongo-IntelligenceIntegrationSystem-${STAMP}.archive.gz"

# --- 2) 认证数据库（SQLite 文件复制） ---
if [ -f ./_data/Authentication.db ]; then
  echo "[backup] 复制认证数据库 ..."
  cp -p ./_data/Authentication.db "${OUT_DIR}/Authentication.db"
else
  echo "[backup] 警告：./_data/Authentication.db 不存在（未创建用户或使用旧路径）"
fi

# --- 3) 配置快照（含敏感信息，请注意保管） ---
if [ -f ./_config/config.json ]; then
  cp -p ./_config/config.json "${OUT_DIR}/config.json.snapshot"
  chmod 600 "${OUT_DIR}/config.json.snapshot" 2>/dev/null || true
fi

# --- 清理：仅保留最近 14 次备份（可按磁盘策略调整） ---
ls -1dt "${BACKUP_DIR}"/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf

echo "[backup] 完成。文件清单："
ls -l "${OUT_DIR}"

# ============================ 恢复方法（人工执行） ============================
#
# 前置：恢复目标必须是隔离测试库（新起 mongod / 空容器），验证无误后才可
# 用于生产灾备恢复。禁止直接对生产库盲恢复。
#
# 1) 恢复 MongoDB（以 STAMP=20260920-120000 为例）：
#    gunzip -c mongo-IntelligenceIntegrationSystem-20260920-120000.archive.gz | \
#      docker exec -i china-watch-mongo mongorestore \
#        -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
#        --nsInclude='IntelligenceIntegrationSystem.*' --nsFrom='IntelligenceIntegrationSystem.*' \
#        --nsTo='<目标测试库名>.*' --archive --drop
#
# 2) 恢复认证数据库：
#    cp -p Authentication.db  <部署目录>/_data/Authentication.db
#    （注意：会覆盖现有账号库，先确认目标库可丢弃）
#
# 3) 恢复后校验：
#    - mongosh 计数 intelligence_archived / ai_delay_queue / push_tasks；
#    - 用备份中的账号执行一次登录（成功 + 失败各一次，检查 login_log）。
# =============================================================================
