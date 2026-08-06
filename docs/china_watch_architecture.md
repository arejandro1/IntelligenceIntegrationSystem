# 美国涉华公开信息监测系统 — 架构文档（M0）

> 状态：M0 开发护栏与架构文档（当前里程碑）
> 本文件记录目标架构，M1—M9 为计划事项，尚未实施。

## 1. 项目目标

将 IntelligenceIntegrationSystem（IIS）v2 改造为**美国涉华公开信息监测系统**，实现：

- 从美国相关公开来源采集涉华信息；
- 在群晖（Synology DS920+）上长期稳定运行，不依赖台式机；
- AI 分析统一通过云端 DeepSeek API 完成，群晖不运行本地大模型；
- 第一阶段关闭但**不删除** VectorDB、本地 Embedding、BGE-M3、ChromaDB 和 FAISS；
- 生产部署最终采用四容器结构（M7 实施）。

## 2. 现有架构概览

IIS v2 现有核心组件：

| 组件 | 职责 |
|------|------|
| `IntelligenceHub.py` | 核心引擎：队列、AI 分析线程、后处理线程、定时任务 |
| `IntelligenceHubStartup.py` | 启动组装：读取配置、初始化 MongoDB/VectorDB/AIClientManager |
| `CrawlerServiceEngine.py` | 爬虫服务入口：插件化任务管理、热重载 |
| `CrawlTasks/` | 具体媒体源抓取任务 |
| `ServiceComponent/` | 业务组件层（数据模型、分析代理、查询、统计等） |
| `AIClientCenter/` | AI 客户端管理中心（子模块） |
| `IntelligenceCrawler/` | 爬虫框架（子模块） |

改造原则：**不直接修改子模块**，新功能优先通过主仓库适配器、包装器和配置实现。

## 3. 目标数据流

```
采集（爬虫，并发2）
  → 原始缓存（MongoDB）
  → Hub侧权威预筛（规则预筛）
      ├─ 拒绝 → 拒绝审计
      ├─ 元数据归档（不调用AI）
      └─ AI延期队列（MongoDB持久化）
          → 闲时Drainer（OffPeakQueueDrainer）
          → DeepSeek Flash（普通内容）
              └─ 重点内容 → DeepSeek Pro（复核）
          → 费用台账
          → 归档（MongoDB）
          → 推送
```

详见 `docs/china_watch_data_flow.md`。

## 4. 新增模块

以下为 M1—M9 计划新增模块（均待实施）：

| 模块 | 归属里程碑 | 职责 |
|------|-----------|------|
| 轻量运行配置与依赖分层 | M1 | 功能开关、依赖分层、最小运行配置 |
| 数据结构与 SourceRegistry | M2 | 数据模型、来源注册表、规则预筛 |
| 规则预筛 | M2 | Hub 侧唯一权威预筛，拒绝/元数据/需AI 三分类 |
| DeepSeek 分级路由 | M3 | Flash 普通内容 / Pro 重点内容复核 |
| 峰谷调度 | M3 | 仅在非高峰时段调用 API |
| 持久化延期队列 | M3 | MongoDB 持久化，支持重启恢复、幂等、租约 |
| 费用台账 | M3 | 记录每次调用的模型、Token 与费用 |
| 预算限制 | M3 | 日/月预算与调用次数上限 |
| 首批 8 个来源 | M4 | 首批美国涉华信息来源 |
| PDF 处理 | M5 | PDF 下载、头/大小校验、内容提取 |
| 推送服务 | M6 | 推送渠道接入 |
| Docker Compose 与群晖部署 | M7 | 四容器结构部署 |

## 5. 峰谷时间策略

计费时区：Asia/Shanghai（北京时间）。

**高峰时段（2 倍价格）：**

- [09:00, 12:00)
- [14:00, 18:00)

**非高峰时段：**

- [00:00, 09:00)
- [12:00, 14:00)
- [18:00, 24:00)

**系统策略：**

1. 只在非高峰时段调用 DeepSeek API。
2. 高峰期间 Flash、Pro、API 重试和付费健康检查均为 0。
3. 高峰期间继续采集、预筛、去重、存储和排队。
4. 第一阶段不允许紧急绕过。
5. 正式生效日期和模型基准单价配置化。
6. 政策生效日期为空时不得进行正式调用。

**Guard 默认值：**

- `OFF_PEAK_START_GUARD_MINUTES=1`
- `OFF_PEAK_END_GUARD_MINUTES=10`

**实际允许启动请求窗口：**

- [00:00, 08:50)
- [12:01, 13:50)
- [18:01, 24:00)

所有时段、Guard、价格、政策版本和生效日期必须配置化，业务代码不得写死。

## 6. AI 消费者互斥

| 配置 | M1/M2 默认 | M3 启用后 |
|------|-----------|-----------|
| `API_ENABLED` | false | true |
| `API_DRY_RUN` | true | false |
| `USE_LEGACY_AI_WORKER` | false | false |
| `USE_OFF_PEAK_DRAINER` | false | true |

互斥规则：

- 旧 AI Worker（LEGACY）和 OffPeakQueueDrainer（新）**不得同时启用**；
- 两者同时为 true 必须启动失败；
- 两者同时为 false 是合法的只采集模式；
- M1—M2 中不得默认启用旧 AI Worker。

## 7. VectorDB 关闭

- 第一阶段关闭但不删除 VectorDB、本地 Embedding、BGE-M3、ChromaDB 和 FAISS。
- **VectorDB 唯一配置来源为 `intelligence_hub.vectordb.enabled`**。
- 不得新增第二个 VectorDB 开关。
- 关闭状态下相关启动逻辑不执行，相关代码保留不删除。

## 8. 四容器架构（M7 计划）

| 容器 | 职责 |
|------|------|
| china-watch-mongo | MongoDB 数据库 |
| china-watch-hub | 情报中心：队列、预筛、调度、Web 管理 |
| china-watch-crawler | 采集服务 |
| china-watch-notifier | 推送服务 |

- Docker 化位于 M7，不得提前到 M0 或 M1。
- MongoDB 容器不得公开映射端口到公网。
- 管理界面不得直接公开公网，须通过内网访问或反代保护。
- 容器必须使用非 root 用户运行。

## 9. 风险和回滚

### 风险

| 风险 | 缓解措施 |
|------|----------|
| 高峰时段误调用 API 产生费用 | 峰谷策略 fail-closed；Guard 窗口；政策生效日期为空禁止调用 |
| 队列丢失 | MongoDB 持久化 + 重启恢复 + 幂等 |
| 新旧 AI 消费者同时运行 | 启动时互斥校验，同时为 true 启动失败 |
| 磁盘耗尽 | 日志轮转 + 70%/85%/95% 三级磁盘降级 |
| 密钥泄露 | 密钥不进源码、日志脱敏、`.env` 被忽略 |
| SSRF 攻击 | 禁止内网/私有 IP、重定向重新校验、DNS 解析后再校验 |

### 回滚方式

- 每个里程碑通过 git 分支隔离，回滚采用 `git revert` 或恢复上一里程碑提交（在复审批准后进行）。
- 配置变更的升级/降级均通过配置开关控制，不修改业务代码即可切换行为。
- M0 仅在 `feature/china-watch-mvp` 分支维护文档与规则，无业务代码变更。