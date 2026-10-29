# 审查修复决策记录（P2-11 / P2-12）

> 本文档记录两项需要产品决策的"断头路"问题的处理结论与依据。
> 日期：2026-08-19

---

## P2-11：相似历史消息降权/去重链路（决策：暂不接线，标注预留）

**现状**：
- V25 prompt 定义了 `{{SIMILAR_MESSAGES}}` 占位符及围绕它的降分硬性规则（`prompts_v2x.py`）
- `IntelligenceAnalyzerProxy.py` 永远将占位符置空（不填充）
- `IntelligenceHub._ai_analysis_worker` 不传 `similar_messages` 参数
- `IntelligenceScoringEngine` 的相似惩罚阈值成为永不触发的死代码

**决策**：**暂不接线**，理由：
1. 接线需要在每条情报分析前执行向量库 top-N 检索，按当前 VectorDB 在第一阶段处于关闭状态（`intelligence_hub.vectordb.enabled=false`）的架构约束，无法实施；
2. M3 启用 OffPeakQueueDrainer 后，分析走 DeepSeek 分级路由，若在分析前加向量检索依赖，会破坏"AI 延期队列 → 闲时消费"的解耦设计；
3. 单点 LLM 判断 + URL 规范化去重（本次已修复 utm_*/大小写/尾斜杠绕过）已能覆盖主要重复场景。

**后续行动**：
- M3 完成护栏接线、M9 扩展全部来源后，若重复报道问题仍显著，再评估"归档后异步向量查重 + 二次降权标记"方案（不阻塞分析主链路）。

---

## P2-12：china_watch 预算/峰谷/预筛护栏（决策：按 M3 里程碑接线，本期标注预留）

**现状**：
- `ApiTimePolicy`、`BudgetGuard`、`PricingPolicyRegistry`、`DeepSeekModelRouter`、`RulePreFilter`、`PushNotifier` 组件均已实现且有完整测试
- `IntelligenceHubStartup` 只读取了 `api_enabled/api_dry_run`，上述组件无活代码实例化
- `IntelligenceHub.time_policy` 恒为 None

**决策**：**不在本次修复批次接线**，理由：
1. `.clinerules/00-project-charter.md` 明确规定 M3（DeepSeek 分级路由、峰谷调度、持久化队列、费用台账和预算限制）是独立里程碑，且"一次只实施一个批准里程碑"；
2. 当前项目处于 M0-M2 阶段（API_ENABLED=false, API_DRY_RUN=true），旧 AI Worker 与 OffPeakQueueDrainer 均未启用，此时接线护栏代码属于超前实施；
3. M1 测试套件（`test_m1_config.py`）已验证 `api_enabled=false` 时无 AI 调用，当前默认配置下不存在真实资金风险。

**后续行动**：
- 进入 M3 实施时，在 `IntelligenceHubStartup.start_intelligence_hub_service()` 中按 config 实例化各护栏组件并传入 Hub；
- 接线时必须遵守 `10-architecture.md` 的互斥规则（旧 Worker 与 Drainer 不得同时启用）；
- M3 验收标准已包含在 `docs/acceptance_checklist.md`。

---

## 其他记录在案但未处理的 P3 项

以下项目属于持续改进项，不在本批次处理，记录备查：

| 项目 | 说明 | 建议 |
|------|------|------|
| 死代码清理 | `prompts_v1x.py`、`Crawl4AI.py`（Ellipsis 占位）、`createV2RatingList()`、RSS 路由注释块 | P3 批次集中清理 |
| 日志统一 | 活代码多处 `print`（`IntelligenceAnalyzerProxy.py` 等） | 迁移到 logger |
| 日志脱敏 | `AIServiceTokenRotator.py` 记录 key 前 16 字符 | 改 `key[:4]+…+key[-4:]` |
| 依赖锁定 | `requirements-server.txt` 多个包未固定版本 | 锁定 + pip-audit |
| Flask-Cors CVE | `Flask-Cors==4.0.0` 有 CVE-2024-1681 | ✅ 已修复（2026-08-28）：requirements_freeze.txt 升级到 4.0.1 |
| urllib3 旧分支 | `urllib3==1.26.20` | 升级到 2.x 需评估兼容性 |
| MongoDB 认证 | compose 未设 root 账号 | M7 部署时补充 |
| SECRET_KEY | 已支持配置化（本次 Startup 已回写），需运维配置固定值 | 部署文档强调 |
| 安全响应头 | 缺 X-Content-Type-Options/X-Frame-Options/CSP | 反代层或 after_request 添加 |
| CDN 本地化 | 前端框架走公共 CDN 无 SRI | 群晖内网部署需本地化到 static/vendor/ |
| 导航统一 | `base.html` 与 `_standalone_nav.html` 两套并存 | UI 批次统一 |
| `utcnow()` 弃用 | 多处使用，3.12+ 警告 | 渐进迁移到 `now(timezone.utc)` |
| `verify=False` | `RequestsScraper.py` 全局禁用证书校验 | 修复证书后移除 |
| systemd ProtectHome | `iis-crawler.service` 与工作目录写需求冲突 | 去掉或加 ReadWritePaths |
| `ArbitraryRPC.py:289` | f-string 漏花括号 | 单行修复 |
| UUID 大小写 | 存储原大小写、查询 lower | 统一 lower 化 |
| `PersistentQueue.insert_if_absent` | 捕获所有异常误报幂等命中 | 只捕 DuplicateKeyError |
| DynamicGraph 召回 | 未传 event_period 时间窗 | 传参即可 |
| 聚合监控线程 | VectorDB 重启后每 5s 刷错误 | 识别 404 时 break |
| top_n 契约 | 客户端 150 / 服务端 clamp 100 | 配置化统一 |
| VECTOR_ADMIN_TOKEN | 部署模板缺失，失败静默 | .env.example 补充 |
| 图谱 LLM 评估 | `ai_client=None` 半状态机 | 保持禁用并已注释说明 |
| 登录暴破 | 无账户锁定 | 建议失败 N 次锁定 |
| 评分关键词误伤 | "十大"命中"二十大"等 | 换词组级信号 |
| TAXONOMY 枚举 | 裸 str 无约束 | 加 field_validator |

---

## 本批次修复清单（已完成）

| 编号 | 问题 | 文件 | 修复 |
|------|------|------|------|
| P0-1 | Token 泄露 | `_config/config_example.json`, `_config/readme.txt` | 占位符 + 禁止真实值标注 |
| P0-2 | Gemini Key 硬编码 | `_config/ai_client_config_example.py` | 改环境变量读取 |
| P0-3 | 归档失败静默吞 | `IntelligenceHub.py` | 删除内层 try/except |
| P0-4 | Pydantic validator 禁用 + 伴生地雷 | `IntelligenceHubDefines_v2.py`, `IntelligenceHub.py`, `Tools/DateTimeUtility.py` | 删多余装饰器 + isinstance 短路 + 类型防御 |
| P0-5 | UnicodeSanitizer 正则失效 | `Scrubber/UnicodeSanitizer.py` | `\U0001F000` 8位格式 + 删代理对 + 保留 \t\n\r |
| P0-6 | 日志/治理端口暴露 | `PyLoggingBackend/LoggerBackend.py`, `IntelligenceCrawler/CrawlerGovernanceBackend.py` | 绑 127.0.0.1 + debug=False + 移除 CORS 全开 |
| P1-7 | Gunicorn 双实例 | `IntelligenceHubLauncher.py` | 删顶层导入，惰性导入 |
| P1-8 | compose 爬虫配置缺失 | `docker-compose.yml`, `Workflow/RssFeedsBasedCrawlFlow.py` | 挂载 _config/_data + CHINA_WATCH_SUBMIT_URL 环境变量 |
| P1-9 | 聚类输入截断 | `VectorDB/VectorStorageEngine.py` | get() 加 offset + 截断告警 |
| P1-10 | _jobs 内存泄漏 | `VectorDB/aggregation/cluster_manager.py` | 提交前调 cleanup_jobs() |
| P1-四 | XFF 可伪造 | `IntelligenceHubWebService.py` | 可信代理白名单 |
| P1-四 | 公开搜索窗口绕过 | `IntelligenceHubWebService.py` | 强制钳制（忽略游客传参） |
| P1-四 | Mongo 零索引 | `IntelligenceHubStartup.py` | UUID/归档时间/INFORMANT 索引 |
| P1-五 | 热重载泄漏 Playwright | `CrawlerServiceEngine.py` | module_fini + 资源兜底清理 |
| P2 | /collect 队列满阻塞 | `IntelligenceHub.py` | put(timeout=5) 拒绝 |
| P2 | AI 等待循环无退出 | `IntelligenceHub.py` | shutdown 检查 + 优雅退出 |
| P2 | 向量化队列满静默丢 | `IntelligenceHub.py` | warning + 丢弃计数 |
| P2 | 死定时任务 | `IntelligenceHub.py` | 移除 backfill 注册 |
| P2 | Error 可变默认值 | `IntelligenceHub.py` | field(default_factory) |
| P2 | RPC 白名单死方法 | `IntelligenceHubWebService.py` | 移除 4 项 |
| P2 | 评分先于归一化 | `IntelligenceHub.py` | 先 check_sanitize 再评分 |
| P2 | URL 去重绕过 | `IntelligenceHub.py` | URL 规范化（utm/大小写/尾斜杠/fragment） |
| P3 | .dockerignore 不全 | `.dockerignore` | 补 ai_client_config/Authentication.db/sf_keys/db 等 |

**验证**：全部修改文件通过 `ast.parse` 语法校验；`_run_tests.py` 全部 10 项测试通过。

---

## P2 收尾批次完成记录（2026-10-12）

> 对应 `docs/fix_remaining_prompt.md` 各批次，逐项如实记录（含此前已完成项的核实结论）。
> 本批次同时完成：`Test/test_m3_queue_mongo.py` 测试桩改抛 `DuplicateKeyError(code=11000)`
> （原裸 `Exception("DuplicateKeyError")` 不满足 `PersistentQueue.insert_if_absent`
> 的三种重复键信号）；`_run_tests.py` 升级为默认全量发现 + 模块级隔离运行
> （pytest `Skipped` 继承 `BaseException`，加载期用 `except BaseException` 捕获，
> `test_vectordb_fixes` 缺 chromadb 时记为整模块跳过而非中断）。

### 此前批次已完成（本次核实）

| 编号 | 项 | 核实结论 |
|------|----|----------|
| A1 | entity_frequency.html 时区偏移拼接 | 已实现 `browserTimezoneOffsetSuffix()`（templates/entity_frequency.html:409 起） |
| A2 | intelligence_search 时区标签/ISO 转换 | 已实现（static/js/intelligence_search.js:383 起，`tz-suffix-label` 填充 GMT 偏移） |
| A3 | UTC+8 用例验证 | 此前已有 node 断言脚本验证偏移 +08:00（本次未重跑） |
| B1 | Playwright 共享常驻浏览器池 | 已实现（Scraper/PlaywrightRawScraper.py:144 起模块级池） |
| C2 | 页脚 © 2025 硬编码 | 核实全仓模板无 `© 2025` 硬编码，按任务约定跳过 |
| C3 | export_mongodb 进度条假反馈 | 已有"任务已提交"提示与 resetUI 复位 |
| C4 | 双导航命名不一 | 命名已统一为"情报聚类"（base.html:43 与 _standalone_nav.html:6 一致）；两模板文件仍并存，未物理合并 |
| D1 | urllib3 升级阻塞说明 | TODO 已写入 requirements.txt:46-47（freeze 实为 2.3.0） |
| D3 | torch CPU 索引注释 | 已写入 requirements.txt:35-37 |

### 本次完成（2026-10-12）

| 编号 | 项 | 修改 |
|------|----|------|
| B2 | 常驻浏览器关闭接线（保守方案） | CrawlerServiceEngine.py `_handle_shutdown` 末尾调用 `close_shared_browsers()`（局部导入 + try/except 记 warning；热重载路径不调用，注释说明池跨热重载保留属设计） |
| C1 | login.html / monitor_dashboard.html 补 favicon | 与 base.html:10 写法完全一致 |
| E1 | ProcessCotrolException 副本注明 | Tools/ProcessCotrolException.py 文件头 docstring 注明子模块副本勿引用（主仓库统一从 `Tools.` 导入此前已达成） |
| E2 | import * 泄漏 | CrawlerServiceEngine.py 顶部补显式 `import os`（其余标准库本已显式，GlobalConfig 仅传配置常量） |
| E4 | recycled/ 禁止引用标注 | README.md 目录结构段落与 AGENTS.md 3.2 节各加一句 |
| E5 | prompts_v2x 硬性规则去重 | 删除 V25 规则 5（规则 4 的弱化子集），规则 6—15 重编号为 5—14，规则 4 文本不动；"刚/则"错字与 GEOGRAPHY 分号问题此前已核查不存在/已兼容。prompt 语义变化：仅删除冗余弱规则，约束强度不变 |
| F1 | implementation_plan 状态回填 | M4—M9 总览表与各节按 `docs/acceptance_checklist.md` 回填（代码与离线测试 ✅；实机 ⬜），头部追加回填注记 |
| F2 | AGENTS.md 第 6 节演示脚本注明 | 核实说明已存在（第 6 节末行），无需新增；改名未执行需用户批准 |
| F3 | 完成记录与勾选回填 | 本小节 + `docs/fix_remaining_prompt.md` 全量勾选同步 |
| D4 | pip-audit 依赖安全审计 | pip-audit 2.10.1 审计当前环境（非 requirements 全集），19 包 105 条记录，报告写入 `docs/dependency_audit.md`（只检查不修复） |

### 未完成/部分完成

- D2 requirements 未钉版本补钉：本批次禁止修改 requirements，未执行；streamlit（requirements.txt:96）经调查代码零引用、环境未安装，处置建议见交付报告任务 11。
- E3 文本字段超长截断：**原记录有误，复核更正为"此前已完成"**——`ServiceComponent/IntelligenceHubDefines_v2.py` 的 `PROCESSED_FIELD_MAX_LENGTH`（:184）与 `_truncate_overlong_fields` model_validator（:347）已实现超长截断 + warning + `APPENDIX.__FIELD_TRUNCATED__`（:437）记录，不拒收。P2 批次未改动该项，仅更正记录。

### 验证命令与结果摘要（2026-10-12）

- `python _run_tests.py`（无参数，全量 32 模块）：**run=403 fail=0 err=0 skip=12**；
  `test_vectordb_fixes` 因缺 chromadb 记为整模块跳过（不中断、不计失败）；
  `_test_result.txt` 末尾 `SUCCESS=/TESTS_RUN=/FAILURES=/ERRORS=` 契约保持。
- `python _run_tests.py Test.test_m1_config`（带参数）：run=10 fail=0 err=0，行为与旧版一致。
- `python _run_tests.py Test.test_m3_queue_mongo`：run=9 fail=0 err=0（测试桩修复生效）。
- 任务 5 相关回归：test_m2_prefilter（16 用例）、test_fix1_ai_gate（18 用例）在全量中通过。
- 全部修改的 Python 文件通过 `ast.parse` 语法校验；未调用真实付费 API；未修改子模块与 requirements。