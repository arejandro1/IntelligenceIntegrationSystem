# 架构决策记录（ADR）

> 状态：M0 开发护栏与架构文档（当前里程碑）
> 以下 ADR 均为 M0 阶段的架构决策，M1—M9 实施时须遵守；部分决策标注"计划/待实施"的内容尚未落地。

## ADR-001：群晖不运行本地 LLM

- **状态**：已接受
- **背景**：Synology DS920+ 为 Intel J4125 四核四线程、20GB 内存、无独立 GPU。
- **决策**：不在群晖运行本地大模型。
- **理由**：无 GPU，CPU 推理不满足对时效与质量的要求。
- **后果**：AI 推理统一通过云端 DeepSeek API 完成。

## ADR-002：VectorDB 第一阶段配置关闭

- **状态**：已接受（实施细节待 M1 规划）
- **背景**：现有系统依赖 VectorDB、本地 Embedding、BGE-M3、ChromaDB 和 FAISS。
- **决策**：第一阶段关闭但不删除 VectorDB、本地 Embedding、BGE-M3、ChromaDB 和 FAISS。
- **唯一配置来源**：`intelligence_hub.vectordb.enabled`。
- **约束**：不得新增第二个 VectorDB 开关；关闭状态下相关启动逻辑不执行，相关代码保留不删除。
- **后果**：降低群晖资源占用，聚焦采集与预筛。

## ADR-003：Hub 是唯一权威预筛点

- **状态**：已接受（实施细节待 M2 规划）
- **背景**：多来源、多渠道数据需要统一的预筛标准。
- **决策**：所有来源数据必须先经过 Hub 侧规则预筛，才能进入 AI 延期队列；爬虫侧不执行最终判定。
- **后果**：预筛规则统一由 Hub 管理，可配置，不得硬编码。

## ADR-004：DeepSeek 仅非高峰调用

- **状态**：已接受（实施细节待 M3 规划）
- **背景**：DeepSeek 高峰时段价格为基准价格 2 倍。
- **决策**：
  - 只在非高峰时段调用 DeepSeek API。
  - 高峰期间 Flash、Pro、API 重试和付费健康检查均为 0。
  - 高峰期间继续采集、预筛、去重、存储和排队。
  - 第一阶段不允许紧急绕过。
  - 正式生效日期和模型基准单价配置化。
  - 政策生效日期为空时不得进行正式调用。
- **计费时区**：Asia/Shanghai（北京时间）。
- **高峰时段**：[09:00, 12:00)、[14:00, 18:00)。
- **非高峰时段**：[00:00, 09:00)、[12:00, 14:00)、[18:00, 24:00)。
- **Guard 默认值**：`OFF_PEAK_START_GUARD_MINUTES=1`、`OFF_PEAK_END_GUARD_MINUTES=10`。
- **实际允许启动请求窗口**：[00:00, 08:50)、[12:01, 13:50)、[18:01, 24:00)。
- **后果**：所有时段、Guard、价格、政策版本和生效日期必须配置化。

## ADR-005：旧 AI Worker 与 Drainer 互斥

- **状态**：已接受（实施细节待 M1/M3 规划）
- **背景**：新旧两套 AI 消费逻辑并存。
- **决策**：
  - 旧 AI Worker（LEGACY）和 OffPeakQueueDrainer（新）不得同时启用。
  - 两者同时为 true 必须启动失败。
  - 两者同时为 false 是合法的只采集模式。
- **M1、M2 默认状态**：`API_ENABLED=false`、`API_DRY_RUN=true`、`USE_LEGACY_AI_WORKER=false`、`USE_OFF_PEAK_DRAINER=false`。
- **M3 启用后**：`API_ENABLED=true`、`API_DRY_RUN=false`、`USE_LEGACY_AI_WORKER=false`、`USE_OFF_PEAK_DRAINER=true`。
- **后果**：不得在 M1—M2 中默认启用旧 AI Worker。

## ADR-006：延期队列使用 MongoDB 持久化

- **状态**：已接受（M3 已实现，单元测试通过）
- **背景**：进程重启后队列不能丢失。
- **决策**：AI 延期队列必须使用 MongoDB 持久化。
- **实现**（M3）：
  - `ServiceComponent/PersistentQueue.py`：定义 `QueueBackend` 协议（依赖注入，便于测试）。
  - `MemoryQueueBackend`：内存后端，用于单机/测试，语义与 MongoDB 一致。
  - `MongoQueueBackend`：生产持久化后端，惰性导入 `pymongo`，使用 `find_one_and_update` 原子认领。
  - `PersistentQueue`：队列门面，封装入队/认领/完成/失败/重启恢复。
  - `OffPeakQueueDrainer` 重构为使用 `PersistentQueue`（默认内存后端，可注入 MongoDB）。
- **要求**：
  - 队列项包含：content_hash、payload、优先级、入队时间、重试次数、状态、worker_id、租约到期时间。
  - 幂等：同一 content_hash 重复入队返回 False，不产生重复（Memory/Mongo 均已测试）。
  - 重启恢复：`recover_on_restart()` 回收过期租约，使未完成项重新可被认领（已测试）。
  - 租约机制：处理中项带租约，超时自动释放（已用 FakeClock 测试）。
  - 并发认领：同一项不得被两个 worker 同时认领（原子 claim，已测试）。
  - 超限进 DEAD：超过 max_retries 的项进入 DEAD 状态，不再重试（已测试）。
- **测试**：`Test/test_m3_queue.py`（20 用例：幂等/租约/并发认领/重启恢复/重试/互斥）。

## ADR-007：不直接修改 Git 子模块

- **状态**：已接受
- **背景**：`AIClientCenter/`、`IntelligenceCrawler/`、`VectorDB/`、`MyPythonUtility/`、`PyLoggingBackend/` 为 Git 子模块。
- **决策**：不直接修改 Git 子模块。
- **后果**：新功能优先通过主仓库适配器、包装器和配置实现；子模块接口变化时优先在主仓库适配层兼容。

## ADR-008：Docker 化位于 M7

- **状态**：已接受
- **背景**：生产部署最终采用四容器结构。
- **决策**：Docker 化位于 M7，不得提前到 M0 或 M1。
- **四容器**：`china-watch-mongo`、`china-watch-hub`、`china-watch-crawler`、`china-watch-notifier`。
- **安全约束**：
  - MongoDB 容器不得公开映射端口到公网。
  - 管理界面不得直接公开公网。
  - 容器必须使用非 root 用户运行。
- **后果**：M0—M6 阶段不实施 Docker 化。

## ADR-009：最低磁盘 100GB，建议 200GB

- **状态**：已接受（M7 部署时落实）
- **背景**：群晖长期运行，日志、MongoDB 数据、PDF 缓存等会持续增长。
- **决策**：
  - 最低预留磁盘：100GB。
  - 建议预留磁盘：200GB。
- **磁盘告警与降级策略**：

  | 使用率 | 动作 |
  |--------|------|
  | 70% | 告警，通知运维检查清理 |
  | 85% | 严重告警，停止低优先级任务（如推送） |
  | 95% | 紧急降级，停止采集与队列消费，仅保留核心查询与告警 |

- **后果**：日志必须轮转，避免无限增长耗尽磁盘。

## ADR-010：探活端点分工——浅探活（进程存活）与深探活（业务就绪）

- **状态**：已接受（2026-09 P0-1 修复实施）
- **背景**：深度健康检查曾把 `_analysis_threads` 为空判为 503，导致只采集、
  DRY_RUN、OffPeakDrainer 模式被 Launcher 反复重启。
- **决策**：
  - `/maintenance/ping`（浅）：Docker HEALTHCHECK / 外部监控用，只验证
    Flask 进程能应答；
  - `/maintenance/ping?deep=1`（深）：Launcher 用，按 `consumer_mode()`
    检查实际启用的消费者（legacy 线程 / Drainer 线程 / 无消费者为预期），
    并检查后处理线程、Mongo ping（短超时）与队列积压；
  - 等待非高峰、预算耗尽、DRY_RUN、API 禁用是预期状态，仅作为
    `ai_call_gate` 信息项暴露，不参与健康判定；
  - 任何探活不调用付费 API。
- **后果**：模式误判消除；线程死亡与数据库不可达仍会触发 503 重启。

## ADR-011：认证数据库迁移至 _data（可写目录）

- **状态**：已接受（2026-09 P0-2 修复实施）
- **背景**：Docker 部署 `_config` 只读挂载，SQLite 无法写登录日志
  （含 journal/WAL），登录不可用。
- **决策**：
  - 默认路径 `_data/Authentication.db`；`AUTH_DB_PATH` 可覆盖；
  - `GlobalConfig.resolve_user_db_path()` 只解析路径（导入无副作用）；
  - `migrate_legacy_user_db()` 在 UserManager 打开默认路径前执行旧库
    复制（幂等；失败抛错，禁止静默新建空库丢账号）；
  - `_init_db` 失败 fail-fast，不吞异常。
- **后果**：配置文件保持只读挂载；历史位置 `_config/Authentication.db`
  与项目根旧库在首次启动时自动复制并打显眼日志，旧文件保留。

## ADR-012：配置唯一来源为 config.json，布尔严格解析

- **状态**：已接受（2026-09 P0-3 修复实施）
- **决策**：
  - china_watch 开关与策略参数唯一来源 `_config/config.json`
    （`intelligence_hub.*`）；Mongo 连接允许 `MONGO_*` 环境变量覆盖
    （优先级 env > config.json，容器部署需要）；
  - `.env.example` 移除无人读取的 `API_DRY_RUN`/`USE_*` 等键；
  - `ServiceComponent/ConfigParsing.parse_bool` 严格解析：字符串
    `false` 为 False，非法值启动即报错（封堵 `bool("false")` 陷阱）；
  - OffPeakDrainer 模式不要求 `_config/ai_client_config.py`
    （DeepSeek 密钥经 `DEEPSEEK_API_KEY` 注入），仅旧 AI Worker 运行时
    要求该文件。
- **后果**：`.env` 只保存确实被读取的环境变量；示例配置默认关闭真实
  付费调用。

## ADR-013：群晖数据库采用外部 MongoDB（AVX 约束）

- **状态**：已接受（2026-09 P0-4 修订；待目标环境验收）
- **背景**：MongoDB 5.0+ 要求 CPU AVX；DS920+（J4125）无 AVX，
  mongo:7 容器无法运行；MongoDB 4.4 已 EOL（2024-02），不作为默认方案。
- **决策**：
  - `docker-compose.yml` 默认连接外部 MongoDB（`.env` 的 `MONGO_*`），
    不包含本地 mongo 容器与 depends_on（外部模式不被本地依赖阻塞）；
  - `docker-compose.local-mongo.yml` 为 AVX 兼容宿主机提供本地容器
    （叠加使用，WiredTiger 0.5GB、备份卷挂载、健康检查依赖）。
- **后果**：DS920+ 部署需要一台 AVX 兼容机器（或托管服务）承载数据库；
  不以降级停止维护版本换取"单机可跑"。

## ADR-014：分析完成与归档完成分离（delivery_status）

- **状态**：已接受（2026-09 P1-5 修复实施）
- **背景**：Drainer 先 COMPLETED 再投内存队列，崩溃/回调失败/队列满
  时分析结果永久丢失；旧恢复路径依赖未启动的旧工作线程。
- **决策**：
  - 队列项增加 `delivery_status`（PENDING/DELIVERED）；complete 只代表
    分析完成；on_complete 回调成功才 `deliver()`；
  - 启动与每轮维护执行 `replay_undelivered()`：只重交付、绝不重复调 AI；
    归档幂等由 Hub 查重保证；历史记录（无该字段）视为未交付，交付一次即止；
  - 队列满不再打 E 标记（该路径依赖旧线程），保持未交付等待重放；
  - `use_off_peak_drainer=true` 且 Mongo 队列不可用时启动失败，
    禁止静默降级内存队列；
  - DEAD 终态提供 `requeue_dead()` 受控重试。
- **后果**："分析花钱但结果丢失"的恢复断点被封堵。

## ADR-015：费用台账持久化与逐请求护栏

- **状态**：已接受（2026-09 P1-6 修复实施）
- **决策**：
  - `ApiUsageLedger` 支持 Mongo 后端（`api_usage_ledger` 集合），
    以 attempt_id 幂等、与 task_id 分离；`BudgetGuard.restore_from_ledger`
    启动恢复日/月累计；
  - Drainer 每项处理前 `try_reserve`（预算原子预留）→ 成功后按实际
    usage `settle`；真实模式台账必须持久化（不可用即拒绝启动）；
  - 每分钟开始次数按真实时间滑动窗口计算，失败尝试计入；
  - 每次真实 HTTP 尝试（含重试）经 `pre_attempt_hook` 护栏，
    高峰边界/预算耗尽/停止信号立即终止重试；
  - 成功响应但解析失败：异常附带 `api_usage`，按 PARSE_FAILED 记账；
    超时/失败按 TIMEOUT_COST_UNKNOWN / FAILED_NO_USAGE 显式记录
    （费用未知不按 0 静默）。
- **后果**：重启后预算不归零；费用缺口（解析失败/超时/未知价格）
  在台账中可见。

## ADR-016：推送渠道采用可配置通用 HTTP webhook

- **状态**：已接受（2026-09 P1-7 修复实施；真实渠道连通待验收）
- **背景**：仓库未指定专用推送渠道；原 `push()` 只记日志不发送，
  notifier 容器创建对象后即退出，属占位实现。
- **决策**：
  - 实现可配置通用 webhook（POST JSON，超时 + 指数退避有限重试 +
    Bearer/X-Webhook-Token 鉴权 + SSRF 校验）；后续渠道可在 Sender 层扩展；
  - 推送任务持久化于 Mongo `push_tasks` 集合（复用 PersistentQueue
    幂等/租约/DEAD 语义）；Hub 归档成功后按 `min_total_score` 入队；
  - notifier 容器常驻消费（`python -m ServiceComponent.PushNotifier`，
    SIGTERM 优雅退出）；只有发送成功才 complete（确认已推送）；
  - 未配置渠道：日志明确显示禁用，不伪报成功；
  - 已知边界：外部 webhook 无幂等能力时，"发送成功但响应丢失"的
    重试会造成重复推送——payload 携带 item_id 供接收方去重。
- **后果**：闭环"归档→推送"可恢复、可审计；重复发送边界文档化。

## ADR-017：磁盘三级降级与备份策略落地

- **状态**：已接受（2026-09 P1-8 修复实施；实机触发待验收）
- **决策**：
  - `ServiceComponent/DiskSpaceGuard`：≥70% 告警、≥85% 停推送入队、
    ≥95% 拒绝新采集提交（阈值配置化 `intelligence_hub.disk_guard.*`）；
  - 状态经 statistics 与 deep ping 暴露；检测函数可注入（测试不依赖真实磁盘）；
  - 备份脚本 `Scripts/backup_china_watch.sh`：带认证 mongodump +
    Authentication.db + config 快照，保留 14 份；恢复只允许对隔离测试库演练；
  - 数据保留：队列/台账/推送历史清理不得删除 PENDING/未交付项，
    仅可清理超期的 DELIVERED/COMPLETED 记录（默认 180 天，运维执行）。
- **后果**：文档承诺的 70/85/95 策略从"承诺"变为"已实现 + 已测试"。