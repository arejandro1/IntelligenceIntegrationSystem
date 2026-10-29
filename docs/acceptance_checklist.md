# 验收清单（M0—M9）

> 状态（2026-09-21 修订）：标注 ✅ 的项已在离线测试（模拟环境）中验证；
> 标注 ⬜ 的项需要目标环境（群晖实机 / 真实 API / 真实渠道）验收。
> 模拟测试通过不等于生产验收。

## M0：开发护栏与架构文档

- [x] `.clineignore` 已创建，排除虚拟环境、缓存、密钥、真实配置、数据库、日志、下载文件、PDF 原始文件、模型文件、构建产物。
- [x] `.clinerules/` 6 个规则文件已创建/更新。
- [x] `.clinerules/workflows/` 3 个工作流文件已创建/更新。
- [x] `docs/` 5 个文档已创建（架构、数据流、实施计划、ADR、验收清单）。
- [x] `.env.example` 已创建，仅含示例键和占位值，无真实密钥。
- [x] `.gitignore` 已更新，`.env`、`.env.*`（保留 `!.env.example`）、`_config/ai_client_config.py` 等被忽略。
- [x] 无业务 `.py` 文件变化。
- [x] 无 `requirements*.txt` 变化。
- [x] 无子模块变化。
- [x] `.env` 被 Git 忽略。
- [x] `.env.example` 未被 Git 忽略。
- [x] `_config/ai_client_config.py` 被忽略且未被 Git 跟踪。
- [x] 文档峰谷时段一致。
- [x] AI 消费者 M1—M2 默认均为 false。
- [x] M0—M9 顺序一致。
- [x] `git diff --check` 无错误。
- [x] 未执行 git commit / push / reset / clean / rebase。

## M1：轻量运行配置、依赖分层和功能开关

- [x] 轻量运行配置方案已实施（requirements-server.txt 分层；✅ 离线测试）。
- [x] 依赖分层已完成，最小配置可运行只采集模式（✅ test_m3_wiring）。
- [x] 功能开关默认值正确：`api_enabled=false`、`api_dry_run=true`、`use_legacy_ai_worker=false`、`use_off_peak_drainer=false`（✅ test_m1_config / config_example.json 断言）。
- [x] 配置缺失/非法时 fail-closed 测试通过（✅ test_p0_config：非法布尔/数值启动即报错）。
- [x] 峰谷时段、Guard、价格、政策版本和生效日期均配置化，未写死（✅ test_m3_wiring）。
- [x] P0-3 补充：配置唯一来源为 config.json；无人读取的环境变量键已从 .env.example 移除（✅ test_p0_config 防回归断言）。
- [x] 无真实 API 调用（默认配置下；✅ test_m8_dry_run）。
- [x] 未修改子模块。

## M2：数据结构、SourceRegistry 和规则预筛

- [x] Target 数据结构已定义（✅ ServiceComponent/ChinaWatchDataModels.py）。
- [x] SourceRegistry 来源注册表已实施，来源列表可配置（sources/china_watch_sources.yaml，✅ test_m4_sources）。
- [x] Hub 侧权威预筛已实施，三分类（拒绝/元数据/需 AI）正确（✅ test_m2_prefilter）。
- [x] 预筛三分类边界测试通过（✅）。
- [x] 规则可配置性测试通过（✅）。
- [x] 预筛规则未硬编码在业务代码中（✅）。
- [x] M1 默认开关状态保持（✅）。
- [x] 未修改子模块。

## M3：DeepSeek 分级路由、峰谷调度、持久化队列、费用台账和预算限制

- [x] Flash/Pro 分级路由已实施（✅ test_m3_wiring）。
- [x] 峰谷调度已实施，非高峰才调用（✅ test_m8_time_policy）。
- [x] MongoDB 持久化延期队列已实施（✅ test_m3_queue_mongo，使用 mock collection 不连接真实 MongoDB）。
- [x] 费用台账已实施并持久化（✅ P1-6 修订：Mongo 后端 + 重启恢复，test_p1_ledger_budget）。
- [x] 预算限制已实施（✅ test_m8_budget / test_p1_ledger_budget）。
- [x] 重启恢复测试通过（✅ 租约回收 + P1-5 未交付重放，test_p1_archive_recovery）。
- [x] 幂等测试通过（✅ 入队与台账双层，test_m3_queue / test_p1_ledger_budget）。
- [x] 租约超时自动释放测试通过（✅）。
- [x] 并发认领测试通过（✅ test_m3_queue_mongo）。
- [x] 新旧 AI 消费者互斥测试通过（✅ test_m3_queue / test_p0_config，含字符串开关严格解析）。
- [x] 峰谷时段与 Guard 边界测试（FakeClock）通过（✅ test_m8_time_policy）。
- [x] DRY_RUN 模式真实调用次数为 0（✅ test_m8_dry_run）。
- [x] 高峰时段真实调用次数为 0（含重试：pre_attempt_hook 阻断，✅ test_p1_ledger_budget）。
- [x] 政策生效日期为空时禁止正式调用（✅ test_m3_wiring fail-fast）。
- [ ] 经人工启用后方可 `API_ENABLED=true`（⬜ 需用户在目标环境执行并验收真实调用）。
- [x] M3 启用后开关状态组合在示例配置中说明（✅）。
- [x] 未修改子模块。

## M4：首批 8 个来源

- [x] 首批来源清单已确认（sources yaml，✅ test_m4_sources）。
- [x] 来源已接入（✅ 配置与任务模块存在；实际抓取需目标环境网络验收 ⬜）。
- [x] 来源数据经过 Hub 侧预筛（✅ 接线测试）。
- [x] 采集并发符合限制（普通爬虫 2、Playwright 1）（✅ 配置断言；实机负载 ⬜）。
- [x] 未修改子模块。

## M5：PDF 处理

> 补记（2026-10-29 核验）：以下各项均为**组件级**离线验证。经排查主仓库当前不存在
> PDF 采集/下载/内容提取流程（来源均为 RSS 文本，`CollectedData.content` 为 str，
> `pdf_worker` 配置暂无生产代码消费），`PdfSecurityValidator` 未接入生产链路；
> 待 PDF 采集流程落地时在"下载落盘后、内容提取前"接入，调用示例见组件 docstring。

- [x] PDF 文件头校验（`%PDF`）已实施（✅ test_m8_pdf）。
- [x] PDF 大小上限已配置化并生效（✅）。
- [x] 伪装文件拒绝测试通过（✅）。
- [x] PDF worker 并发为 1（✅ 配置）。
- [x] PDF 路径穿越防护已实施（✅ test_m8_path）。
- [x] 未修改子模块。

## M6：推送服务

- [x] 推送服务已实施（✅ P1-7 修订为真实发送：通用 HTTP webhook + Mongo 持久化 push_tasks 队列，test_m6_push）。
- [x] 推送渠道：通用 webhook 已实现（仓库未指定专用渠道，选择理由见 docs/decisions.md）；真实渠道连通性 ⬜。
- [x] 推送失败重试有限且带退避（✅ test_m6_push）。
- [x] 只有发送成功才确认已推送（✅ test_m6_push）。
- [x] 未配置渠道时明确显示禁用，不伪报成功（✅ test_m6_push）。
- [x] 日志不输出推送密钥/Token（✅ WebhookSender 不打印 token/响应体）。
- [x] 未修改子模块。

## M7：Docker Compose 和群晖部署

- [ ] 四容器结构已部署（⬜ 需群晖实机验收；P0-4 修订后默认三应用容器 + 外部 MongoDB，本地 Mongo 由 docker-compose.local-mongo.yml 叠加）。
- [x] MongoDB 容器未公开映射端口到公网（✅ 编排审查；实机 ⬜）。
- [x] 管理界面未直接公开公网（127.0.0.1 绑定 + 反代说明，✅ 编排审查）。
- [x] 容器使用非 root 用户运行（✅ 编排审查）。
- [x] MongoDB WiredTiger 缓存目标 0.5GB 已配置（✅ local-mongo 编排 `--wiredTigerCacheSizeGB 0.5`）。
- [ ] 磁盘预留达标（⬜ 目标环境检查）。
- [x] 磁盘 70%/85%/95% 告警与降级策略已实现（✅ P1-8 DiskSpaceGuard + test_p1_disk_guard；实机触发 ⬜）。
- [x] 日志轮转已配置（✅ 容器 logging 滚动 + PyLoggingBackend）。
- [x] 群晖上不运行本地大模型（✅ 设计约束保持）。
- [x] P0-4：数据库 CPU 兼容性方案已修订（外部 Mongo 为默认，不降级 EOL 版本，✅ 文档核验）。

## M8：连续运行、资源和安全测试

- [ ] 连续运行测试通过（⬜ 需目标环境 7 天以上连续运行验收）。
- [ ] 内存占用符合 20GB 约束（⬜ 实机监控）。
- [ ] 磁盘占用符合预留规划（⬜ 实机监控）。
- [x] 磁盘降级策略测试通过（✅ test_p1_disk_guard）。
- [x] SSRF 防护测试通过（✅ test_m8_ssrf；webhook 发送同样经 SSRFGuard）。
- [x] 路径穿越防护测试通过（✅ test_m8_path）。
- [x] 日志脱敏验证通过（✅ 既有断言）。
- [x] 密钥泄露检查通过（✅ .env.example 无真实密钥；台账/日志不含 key）。
- [x] 未修改子模块。

## M9：扩展全部来源

- [x] 全部目标来源清单已确认（17 个来源注册，✅ test_m4_sources）。
- [ ] 全部来源已接入并通过采集验证（✅ 模块与配置就绪；实网抓取 ⬜）。
- [x] 各来源数据经过 Hub 侧预筛（✅）。
- [x] 并发限制维持（✅ 配置）。
- [x] 未修改子模块。

## 2026-09 稳定性修复批次（P0—P2）补充验收

- [x] P0-1 深度健康检查按运行模式探活（✅ test_review_webapi / test_p0_health：collect_only/dry_run/drainer 模式无 AI 线程判健康；线程死亡/Mongo 不可达判 503；等待非高峰/预算耗尽不判故障）。
- [x] P0-2 认证库迁移与写权限（✅ test_p0_authdb：_data 可写路径、旧库自动迁移账号保留、失败显式报错、登录日志写入）。
- [x] P0-3 配置生效规则统一（✅ test_p0_config：严格布尔、互斥、.env.example 无误导键）。
- [x] P0-4 群晖数据库方案修订（✅ 文档核验：AVX 要求 + 外部/本地双模式 compose；实机 ⬜）。
- [x] P1-5 分析完成未归档恢复断点（✅ test_p1_archive_recovery：complete≠delivered、重放不重复调 AI、历史兼容、租约、DEAD 受控重试、持久化队列不可用拒绝启动）。
- [x] P1-6 持久化费用台账与逐请求护栏（✅ test_p1_ledger_budget：幂等记账、跨日跨月、预算恢复、原子预留结算、失败尝试限流、PARSE_FAILED/TIMEOUT 费用未知显式记录、高峰重试为 0）。
- [x] P1-7 PushNotifier 真实可用（✅ test_m6_push；真实渠道 ⬜）。
- [x] P1-8 备份/磁盘/部署可复现性（✅ 备份脚本 + DiskSpaceGuard 测试 + notifier 最小依赖 Dockerfile；恢复演练 ⬜ 仅隔离测试库允许）。
- [x] P2-9 向量四态区分（✅ test_p0_health：disabled/initializing/degraded/ok；statistics 与 deep ping 一致暴露）。
- [x] P2-10 文档与失实描述修正（✅ 本文件 + implementation_plan/synology_deployment/AGENTS/GenerateProjectIntroDocx）。

## 2026-10 修复批次（批次一—五）补充验收

> 状态（2026-10-04 修订）：以下条目均在离线模拟环境验证（内存后端、可注入时钟、
> 模拟接口，未调用真实付费接口、未连接生产数据库）。模拟测试通过不等于实机验收。

### 批次一：统一人工智能调用控制

- [x] 旧、新消费者都关闭时，即使总调用开关开启也不启动任何分析线程（✎ test_fix1_ai_gate：startup 漏检 use_legacy_ai_worker 已修复）。
- [x] 总开关关闭或模拟模式开启时，后台翻译线程不创建；护栏阻止时真实调用次数为 0（✎ test_fix1_ai_gate）。
- [x] 翻译重试受护栏约束，被阻止不计失败、不打死信（✎ test_fix1_ai_gate）。
- [x] 旧分析路径调用后进入同一费用台账（不再"只查预算不记账"）（✎ test_fix1_ai_gate）。
- [x] 布尔开关严格解析（字符串 "false" 不得为真，非法值 fail-fast）（✎ test_fix1_ai_gate / test_p0_config）。

### 批次二：费用预估、重试和持久化记账

- [x] 预估输入费用从最终实际发送的消息估算（含提示词与正文），生产形态正文不再被当空字符串（✎ test_fix2_ledger_estimation）。
- [x] 预估输出上限与实际请求一致（8192）（✎ test_fix2_ledger_estimation）。
- [x] 日预算不足以覆盖保守上界时网络调用次数为 0（✎ test_fix2_ledger_estimation）。
- [x] 每次实际网络请求（含内部重试）独立限流、计次、记账（✎ test_fix2_ledger_estimation）。
- [x] 已取得的调用名额不在发送前被当作额度耗尽拒绝（活跃预留跳过预算复查）（✎ test_fix2_ledger_estimation）。
- [x] 区分"从未发出"（退款+取消）与"已发送结果未知"（保留占用+IN_FLIGHT）（✎ test_fix2_ledger_estimation）。
- [x] 重启前未知费用占用 0.4 元，重启后按预估恢复，不归零（✎ test_fix2_ledger_estimation）。
- [x] 台账读取失败≠空台账、写入失败≠重复记录；降级期间暂停新增付费请求，恢复自动解除（✎ test_fix2_ledger_estimation）。
- [x] 缺失用量记 MISSING_USAGE，不补零记 OK（✎ test_fix2_ledger_estimation）。
- [x] 宿主机 UTC 时，UTC 9/30 16:30 归入北京 10/1 日账与月账（✎ test_fix2_ledger_estimation）。

### 批次三：入队与归档恢复

- [x] 入队结果区分新增成功/确定重复/存储故障（数据库错误不返回"重复"或"成功"）（✎ test_fix3_enqueue_recovery）。
- [x] 缓存写入后、入队前崩溃可补投，不被缓存查重永久阻止（✎ test_fix3_enqueue_recovery）。
- [x] 分析结果保存后崩溃：重放交付，AI 只调用 1 次（✎ test_fix3_enqueue_recovery）。
- [x] 内存入队后（DELIVERED 未最终确认）崩溃：对账重投，复用已保存结果不重复付费（✎ test_fix3_enqueue_recovery）。
- [x] 归档写入后、最终确认前：对账确认关闭，不重复投递/归档（✎ test_fix3_enqueue_recovery）。
- [x] complete/deliver 状态更新失败正确处理（不忽略返回值）（✎ test_fix3_enqueue_recovery）。
- [x] 正常归档与低价值信息保存路径均覆盖最终确认（✎ test_fix3_enqueue_recovery）。

### 批次四：磁盘保护

- [x] 相对路径以项目根为基准，默认配置解析到真实 _data（不拼 _data/_data）（✎ test_fix4_disk_guard）。
- [x] 初始状态与检测失败显示 unknown/check_error，不冒充已确认正常（✎ test_fix4_disk_guard）。
- [x] 阈值边界 69.9/70/84.9/85/94.9/95 全覆盖（✎ test_fix4_disk_guard）。
- [x] 95% 时消费者停止认领新任务（实际行为验证，模型调用为 0）；恢复后继续；在途请求完成（✎ test_fix4_disk_guard）。

### 批次五：采集和构建防护

- [x] 初始地址合法但后续导航/重定向/子请求指向内网、环回、链路本地时阻断（✎ test_fix5_browser_ssrf，受控 fake route 测试）。
- [x] 导航请求每次完整校验；同 host 子资源缓存放行；DNS rebinding 限制已在代码注释与部署文档说明（连接层限制属部署措施）（✎ test_fix5_browser_ssrf）。
- [x] 默认启用 HTTPS 证书校验（ignore_https_errors 默认 False）（✎ test_fix5_browser_ssrf）。
- [x] .dockerignore 排除 _demo_admin_cred.txt 等本地凭据与临时产物（构建上下文检查，非仅 Git 忽略）（✎ test_fix5_browser_ssrf）。

### 待实机验收补充（不自行调用真实付费接口）

- [ ] 群晖实机验证磁盘三级降级真实触发与恢复（模拟环境无法验证真实磁盘水位）。
- [ ] 真实 MongoDB 后端验证 IN_FLIGHT 恢复与对账重放（test_m3_queue_mongo 现用 mock collection；真实后端验证需另行适配）。
- [ ] Playwright 实测浏览器 route 拦截（本机未装 playwright，签名与源码断言已覆盖；真实浏览器行为待实机验证）。
