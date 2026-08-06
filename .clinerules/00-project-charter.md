# 项目总则

## 项目目标

将 IntelligenceIntegrationSystem 改造成美国涉华公开信息监测系统。

## 生产环境

- Synology DS920+
- Intel J4125四核四线程
- 20GB内存
- 无独立GPU
- 不依赖台式机
- AI推理通过云端DeepSeek API完成

## M0—M9 里程碑边界

| 里程碑 | 内容 | 是否允许真实API调用 |
|--------|------|---------------------|
| M0 | 开发护栏与架构文档（当前） | 否 |
| M1 | 轻量运行配置、依赖分层和功能开关 | 否 |
| M2 | 数据结构、SourceRegistry和规则预筛 | 否 |
| M3 | DeepSeek分级路由、峰谷调度、持久化队列、费用台账和预算限制 | 否（测试后人工启用） |
| M4 | 首批8个来源 | 待核验 |
| M5 | PDF处理 | 待核验 |
| M6 | 推送服务 | 待核验 |
| M7 | Docker Compose和群晖部署 | 待核验 |
| M8 | 连续运行、资源和安全测试 | 待核验 |
| M9 | 扩展全部来源 | 待核验 |

M0 只记录 M1—M9 计划，不得实施它们。一次只实施一个批准里程碑。

## 第一阶段约束

- 不在群晖运行本地大模型。
- 关闭但不删除VectorDB、本地嵌入、BGE-M3、ChromaDB和FAISS。
- VectorDB唯一配置来源为 `intelligence_hub.vectordb.enabled`，不得新增第二个VectorDB开关。
- API调用前必须执行规则预筛。
- 普通内容使用Flash（DeepSeek Flash）。
- 重点内容才使用Pro（DeepSeek Pro）复核。
- DeepSeek仅非高峰时段调用（具体时段见 `10-architecture.md` 峰谷策略）。
- 普通爬虫并发为2。
- Playwright并发为1。
- PDF处理并发为1。
- AI worker并发为1。
- 不直接修改Git子模块。
- 不在仓库中保存真实密钥。
- 不自动执行git commit或git push。
- 一次只实施一个批准里程碑。
- 所有沟通和报告使用简体中文。
- 生产部署最终采用四容器结构（M7实施）：china-watch-mongo、china-watch-hub、china-watch-crawler、china-watch-notifier。
- Docker化位于M7，不得提前到M0或M1。

## 禁止项

- 禁止修改任何 `.py` 业务代码、`requirements*.txt`、`config*.json`、`IntelligenceHub*.py`、`CrawlerServiceEngine.py`、`prompts_v*.py`、`ServiceComponent/`、`Workflow/`、`CrawlTasks/`、`Tools/`、`Scraper/`、`templates/`。
- 禁止修改Git子模块：`AIClientCenter/`、`IntelligenceCrawler/`、`VectorDB/`、`MyPythonUtility/`、`PyLoggingBackend/`。
- 禁止安装依赖、创建虚拟环境、启动Python服务、启动MongoDB、启动Docker、调用真实付费API、修改系统环境变量、执行git commit/push/reset/clean/rebase、删除或移动项目文件。