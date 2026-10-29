# Synology DS920+ 部署指南（M7 / P0-4 修订版）

> 状态：文档与编排文件已就绪；**尚未在群晖实机验证**（需要目标环境验收）。
> 硬件：Synology DS920+ / Intel J4125 / 20GB RAM / 无 GPU。

---

## 0. 数据库硬件兼容性（重要结论，P0-4）

- MongoDB 5.0 及以上版本（含 6.x / 7.x / 8.x）**要求 CPU 支持 AVX 指令集**；
- DS920+ 的 Intel Celeron J4125（Gemini Lake）**不支持 AVX**，官方 mongo
  镜像（mongo:7 等）在该机型上启动即崩溃（`illegal instruction`）；
- MongoDB 4.4 是最后一个不要求 AVX 的版本，但其官方维护已于
  2024-02 结束，**不作为默认解决方案**。

因此本部署指南提供两种数据库模式：

| 模式 | 适用硬件 | 说明 |
|------|---------|------|
| A. 外部 MongoDB（默认） | DS920+ 等无 AVX 机型 | 连接运行在兼容硬件上的 MongoDB 实例 |
| B. 本地容器 MongoDB | 支持 AVX 的宿主机 | 叠加 `docker-compose.local-mongo.yml` |

## 1. 部署前检查

| 检查项 | 要求 |
|--------|------|
| DSM 版本 | 7.2 或更高（内置 Container Manager） |
| Docker/Container Manager | 已安装 |
| 磁盘空间 | ≥100GB（建议 200GB） |
| MongoDB（模式 A） | 已在 AVX 兼容硬件上可用，且已启用认证 |
| AVX 验证（模式 B） | `grep -c avx /proc/cpuinfo` 结果 > 0 |
| SSH | 建议开启（外网访问建议配合反向代理） |

## 2. 容器结构

| 容器 | 镜像 | 端口 | 职责 |
|------|------|------|------|
| china-watch-hub | Dockerfile.hub | 127.0.0.1:5800→5000 | 情报中心（队列/预筛/调度/Web） |
| china-watch-crawler | Dockerfile.crawler | — | 采集服务 |
| china-watch-notifier | Dockerfile.notifier | — | 推送服务（消费 push_tasks 队列） |
| china-watch-mongo（模式 B） | mongo:7 | 仅内部 27017 | MongoDB 数据库 |

安全要求：
- MongoDB 端口**不映射到公网**（模式 B 仅绑定 127.0.0.1）；
- 管理界面仅内网访问或通过反向代理保护；
- 所有应用容器使用非 root 用户运行（user: "1000:1000"）。

> **注意：本容器编排不包含 VectorDB 服务。** bge-m3 模型与 ChromaDB 索引对群晖
> 内存压力过大，docker-compose 仅含 hub / crawler / notifier（+ 可选 mongo）。
> 因此容器化部署下**语义搜索、相似推荐、情报聚类功能不可用**
> （Hub 侧 vectordb 会显示 disabled/degraded，属预期）。
> 如需这些能力，请在另一台资源充足的机器上运行
> `python -m VectorDB.VectorDBBService --db-path ... --model ...`，
> 并将 Hub 配置 `intelligence_hub.vectordb.*` 指向该服务。

## 3. 部署步骤（模式 A：外部 MongoDB，DS920+ 默认）

```bash
# 1) SSH 登录群晖
ssh admin@192.168.x.x

# 2) 创建项目目录并传输项目（在本地执行 scp）
mkdir -p /volume1/docker/china-watch
cd /volume1/docker/china-watch
# scp -r ./* admin@192.168.x.x:/volume1/docker/china-watch/

# 3) 创建 .env 并填写外部 MongoDB 连接信息
cp .env.example .env
vi .env
#   MONGO_HOST=<外部 MongoDB 主机>
#   MONGO_PORT=27017
#   MONGO_USER=<账号>
#   MONGO_PASSWORD=<密码>

# 4) 启动（不含本地 mongo 容器）
sudo docker compose up -d

# 5) 验证
sudo docker compose ps
sudo docker compose logs -f china-watch-hub
curl http://127.0.0.1:5800/maintenance/ping          # 进程存活（Docker HEALTHCHECK 同款）
curl http://127.0.0.1:5800/maintenance/ping?deep=1   # 业务就绪（消费者/归档线程/Mongo）
```

外部 MongoDB 准备（在 AVX 兼容机器上）：
- 安装 MongoDB 7.x 并启用认证（`security.authorization: enabled`）；
- 创建专用账号（建议仅授予 `IntelligenceIntegrationSystem` 库的
  readWrite 权限，不用 root）；
- 网络要求：仅对群晖内网开放 27017，绑定内网 IP 或走 VPN/专线；
- 群晖侧 `.env` 与 `_config/config.json` 的
  `intelligence_hub.mongodb.*` 保持一致（环境变量优先级更高）。

## 4. 部署步骤（模式 B：本地容器 MongoDB，AVX 主机）

```bash
# 前置：确认 CPU 支持 AVX
grep -c avx /proc/cpuinfo   # 必须 > 0

cp .env.example .env
vi .env   # 填写 MONGO_INITDB_ROOT_USERNAME / MONGO_INITDB_ROOT_PASSWORD
          # （MONGO_USER/MONGO_PASSWORD 保持一致即可）

sudo docker compose -f docker-compose.yml -f docker-compose.local-mongo.yml up -d
```

模式 B 附加配置：
- WiredTiger 缓存已限制 0.5GB（`--wiredTigerCacheSizeGB 0.5`）；
- 备份目录挂载 `./_export/backup:/backup`（配合备份脚本）；
- hub/notifier 经 compose 内部网络访问 `china-watch-mongo`，
  `depends_on: service_healthy` 保证启动顺序。

## 5. 反向代理配置（可选）

推荐使用群晖内建反向代理（DSM → 控制面板 → 登录门户 → 高级 → 反向代理），将域名 HTTPS 443 → http://127.0.0.1:5800，加上 SSL 证书。

## 6. 磁盘空间、降级策略与日志

- 预留磁盘：最低 100GB，建议 200GB；
- 磁盘三级降级（P1-8 已实现并经 2026-10 批次四修复，Hub 内置 DiskSpaceGuard
  周期检测，阈值见 `_config/config.json` 的 `intelligence_hub.disk_guard`；
  `watch_path` 相对路径以项目根为基准，示例值 `_data` 即项目根下的真实
  `_data` 目录，不会拼出 `_data/_data`）：
  - ≥70%：告警日志（通知运维检查清理）；
  - ≥85%：停止推送任务入队（采集/分析/归档继续）；
  - ≥95%：停止采集提交，暂停认领新分析任务与新增付费请求；在途请求
    自然完成（不中断已发出的调用），已付费分析结果的交付与重放继续执行
    （保护已支付成果），核心查询与告警保留；空间回落到 95% 以下后自动恢复消费；
  - 检测失败（目录不可读/异常）：状态展示“检测异常”（check_error），
    不再显示为“已确认正常”；该状态不触发降级（fail-open），但运维可见；
  - 状态经 `/maintenance/ping?deep=1` 与 statistics 暴露；
- 日志轮转：容器日志由 Docker 滚动（max-size 50m × 5）；
  `_log/` 挂载宿主机由 PyLoggingBackend 自动轮转归档；
- 数据保留策略：
  - MongoDB 队列/台账（ai_delay_queue / api_usage_ledger / push_tasks）
    为审计依据，**清理脚本不得删除 PENDING/未交付项**，仅可按时间清理
    已 DELIVERED/COMPLETED 超过保留期（默认 180 天，运维按需执行）；
  - 推送历史即 push_tasks 集合中的 COMPLETED 文档，随上条策略清理。

## 7. 备份与恢复（P1-8，含认证）

备份（在部署目录执行，脚本自动带认证读取 .env）：

```bash
bash Scripts/backup_china_watch.sh            # 默认输出 _export/backup/<时间戳>/
bash Scripts/backup_china_watch.sh /volume1/backup
```

覆盖内容：MongoDB 全库（归档/队列/台账/推送）、认证库
`_data/Authentication.db`、config.json 快照（含敏感信息，注意保管）；
脚本自动保留最近 14 次备份。

恢复（**只允许对隔离测试库演练**，脚本尾部含完整命令）：
1. `gunzip -c <archive>.gz | mongorestore --nsFrom=... --nsTo=<测试库>...`；
2. 复制 `Authentication.db` 到测试部署的 `_data/`；
3. 校验：集合计数 + 使用备份账号做一次成功/失败登录（验证 login_log 写入）。

## 8. 验证命令

```bash
# 容器健康状态（Docker 使用浅探活，Launcher 使用 deep=1 业务就绪探活）
sudo docker compose ps

# Hub 日志确认
sudo docker compose logs china-watch-hub | grep "Service PID"

# Web 访问（进程存活 + 业务就绪）
curl http://127.0.0.1:5800/maintenance/ping
curl http://127.0.0.1:5800/maintenance/ping?deep=1

# 推送服务状态（未配置渠道时日志会明确显示禁用，不伪报成功）
sudo docker compose logs china-watch-notifier | grep -i push
```

## 9. 本文档的验证边界（如实声明）

- 本文档描述的部署方案**未在群晖实机执行过**；
- mongo:7 在 J4125 上无法运行的结论基于 MongoDB 官方硬件要求
  （5.0+ 需要 AVX）与 Intel 规格表（J4125 无 AVX），属文档核验而非实机复现；
- 外部 MongoDB 模式、本地 Mongo 模式、备份恢复流程均需在目标环境
  验收后标记为完成（见 docs/acceptance_checklist.md M7 节）。
