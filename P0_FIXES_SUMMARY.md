# P0 审查问题修复总结（2026-08-27）

> 来源：7 代理全面审查报告（UI/UX、核心引擎、情报准确度、爬虫链、查询/聚合/发布、安全、VectorDB/运维/文档）
> 范围：13 个 P0 全部修复 + 审查点名的高价值 P1/前端项
> 约束：未触碰 Git 子模块；未执行 git commit；未调用真实 API

---

## 一、13 个 P0 修复明细

### P0-1 归档/AI 分析瞬时失败 = 永久隔离 → ✅ 已修
- **文件**：`IntelligenceHub.py`
- **修复**：`_load_unarchived_data` 恢复查询同时捞「无标记」与「E 标记」文档；E 文档重投前累加 `APPENDIX.__RETRY_COUNT__`，达 `archive_retry_max`(默认 5) 跳过并告警；归档 insert 失败先 `_dump_failed_archive` 落盘 `_data/failed_archive/<uuid>.json` 再打 E，重试成功后自动清理落盘文件。

### P0-2 优雅关闭死代码 + 关停误标 E → ✅ 已修
- **文件**：`IntelligenceHub.py`、`IntelligenceHubStartup.py`
- **修复**：AI worker / post_process 在 `shutdown_flag.is_set()` 时不再打 E/S 标记（数据保持无标记，重启恢复）；`shutdown()` 补齐 join 全部工作线程（含 `_analysis_threads` 句柄）+ `scheduler.shutdown(wait=False)`；启动链路接线 SIGTERM。

### P0-3 重启无界全量加载 → OOM → ✅ 已修
- **文件**：`IntelligenceHub.py`
- **修复**：恢复扫描改游标 `batch_size=500`、时间升序、总量上限 `max_unarchived_load`(默认 10000)，超限截断告警；`unarchived_queue.put` 带 5s 超时防阻塞。

### P0-4 瞬时抓取失败永久 SKIPPED → ✅ 已修
- **文件**：`Scraper/RequestsScraper.py`、`Workflow/RssFeedsBasedCrawlFlow.py`
- **修复**：`fetch()` 返回 `errors`/`status_code`/`permanently_gone`(仅 404/410)；flow 层把「带错误的空结果」映射为可重试 `ProcessProblem`，仅明确终态走 SKIPPED。顺带完成：SSRF 入口校验 + 重定向逐跳校验（`Tools/SSRFGuard.py`）、`verify=True`（certifi）、压缩炸弹防护（gzip/br 解压上限）。

### P0-5 CrawlPipeline 单篇坏文炸整轮 → ✅ 已修
- **文件**：`IntelligenceCrawler/CrawlPipeline.py`、`Workflow/IntelligenceCrawlFlow.py`
- **修复**：`extract_articles` 检查 `result.success` 与内容非空才调 handler；handler 捕获 ValidationError 映射为单篇 `fail_perm` 终态、不再 re-raise 中断整轮；fetch `last_error` → `fail_temp`（可重试），真空页面才 skip。

### P0-6 Gunicorn 管道写满挂死 → ✅ 已修
- **文件**：`IntelligenceHubLauncher.py`
- **修复**：子进程 stdout/stderr 由 PIPE 改为重定向 `_log/gunicorn_child.log` 文件句柄（追加模式），stop 时关闭句柄。

### P0-7 "十大/观察/一周"子串误杀 → ✅ 已修
- **文件**：`ServiceComponent/IntelligenceScoringEngine.py`
- **修复**：删除宽泛 `recap_keywords`，只保留 V25 prompt 指示 LLM 写出的 `low_novelty_hints` 标记语（"总结类/回顾类/新增事实有限"等）；`_count_sub_events` 改严格编号模式；「无/无法判断」维度记 None 不计权重（按有值维度归一化），RATE 中英文别名归一。

### P0-8 URL 去重规范化只做一半 → ✅ 已修
- **文件**：`IntelligenceHub.py`
- **修复**：`submit_collected_data` 将 `_normalize_url()`（utm_* 剥离/大小写/尾斜杠/fragment）结果**写回** `informant` 字段本身，存储值与查重查询值一致。

### P0-9 翻译失明（日/韩/俄/阿） → ✅ 已修
- **文件**：`ServiceComponent/AsyncTranslationPatch.py`
- **修复**：`looks_non_zh` 增加假名（に出現即判非中文）、谚文/西里尔/阿拉伯占比 > 0.1 判定。

### P0-10 翻译失败永不向量化无限重试 → ✅ 已修
- **文件**：`ServiceComponent/AsyncTranslationPatch.py`
- **修复**：失败计数持久化 `APPENDIX.__TRANSLATE_FAILED__`，达 `max_translate_retries`(3) 打 `__TRANSLATE_DEAD__` 死信（backfill 跳过）；失败时按原文触发 `on_patched` 兜底向量化（语义搜索可见）；部分成功记 `__TRANSLATE_PARTIAL__` 同样走 on_patched；原生中文打 `native@日期` 标记防 backfill 饿死。

### P0-11 search_mode 大小写绕过公开限制 → ✅ 已修
- **文件**：`IntelligenceHubWebService.py`
- **修复**：`VALID_SEARCH_MODES` 白名单，非法值（'Mongo'/空串）直接 400；限流、30 天归档窗口、per_page/页码上限移出 mode 分支，对全部合法模式无条件执行；输出带偏移 ISO 8601。

### P0-12 systemd ProtectHome 与工作目录冲突 → ✅ 已修
- **文件**：`systemd/iis-*.service`
- **修复**：保留 `ProtectHome=read-only` + `ReadWritePaths=<项目>/_data <项目>/_log`（不能改 true，Python 解释器与代码均在 /home 下）。

### P0-13 VECTOR_ADMIN_TOKEN 缺失全部 503 → ✅ 已修
- **文件**：`VectorDB/VectorDBBService.py`、`.env.example`、`systemd/iis.env`
- **修复**：`_require_admin_token` 仅套 `/api/admin/*`；数据端点不受影响；token 为空时管理端点 fail-closed + 启动显眼告警；`.env.example` 补充变量与生成方式。

---

## 二、同批完成的高价值项（审查 P1/前端）

| 项 | 文件 | 修复 |
|----|------|------|
| 治理后台按钮必失效且谎报状态 | `IntelligenceCrawler/crawler_governance_frontend.html`、`CrawlerServiceEngine.py` | 前端 token 输入框（sessionStorage）+ 全请求带 `X-Governance-Token`；检查 `res.ok` 后才更新状态 |
| 详情页发布时间恒 N/A | `static/js/article_detail_renderer.js` | 优先读 `APPENDIX.__TIME_PUB__` 回退 v1 `PUB_TIME` |
| 游客详情页占位符噪音 | `article_detail_renderer.js` | 未登录隐藏正文/TIPS，替换登录引导 |
| 上下文超长误判敏感 | `IntelligenceHub.py`、`IntelligenceAnalyzerProxy.py` | `CONTEXT_LENGTH_EXCEEDED` 单独识别，截断 20000 字符重试一次 |
| 相似消息机制全链路断裂 | `IntelligenceHub.py`、`IntelligenceAnalyzerProxy.py` | 分析前向量召回（best-effort 5s 超时，绝不阻断），注入 prompt + 评分引擎相似惩罚真正接线 |
| 低值判定污染负样本库 | `IntelligenceHub.py` | 改按 `TAXONOMY=='无情报价值'` 判定；低值库入库前查重 |
| 发布时间伪造为"现在" | `Workflow/IntelligenceCrawlFlow.py`、`IntelligenceHubDefines_v2.py` | 提取不到日期置 None（CollectedData.pub_time 允许 None） |
| collector token 入库/导出 | `IntelligenceHub.py` | 校验通过后立即 `pop('token')`，不进 cache/归档/导出 |
| 登录 XFF 伪造绕过限流 | `IntelligenceHubWebService.py` | `_get_client_ip` 可信代理白名单（默认仅回环），login 复用 |
| 向量检索故障静默为空 | `IntelligenceHub.py` | `last_vector_error` + statistics `vector_search_degraded` |
| `vectorize_drop_count` 未初始化 | `IntelligenceHub.py` | `__init__` 初始化并纳入 statistics |
| Launcher 重启计数永不衰减 | `IntelligenceHubLauncher.py` | 滑动窗口计数 |
| RSS 死代码 | `IntelligenceHubWebService.py` | `/rssfeed.xml` 重新上线：公开钳制 count≤200 + 游客白名单字段 |
| 空关键词非法 `$or: []` | `ServiceComponent/IntelligenceQueryEngine.py` | 空关键词返回 `{}` |
| 分页排序不稳定 | `IntelligenceQueryEngine.py` | 排序补 `_id` tiebreak |
| 安全响应头缺失 | `IntelligenceHubWebService.py` | CSP/X-Frame-Options/X-Content-Type-Options 等 after_request 注入 |
| pub_time 伪造/时区剥离 | `Tools/DateTimeUtility.py` | 解析失败置 None；naive datetime 补 UTC |
| AI 输出告警静默丢弃 | `IntelligenceHub.py` | 截断/JSON 修复告警写入 `APPENDIX.__ANALYSIS_WARNING__` 随归档保留 |

---

## 三、验证结果（2026-08-27 实测）

| 验证项 | 命令 | 结果 |
|--------|------|------|
| 主测试套件（M1–M8 + review 3 件套） | `python _run_tests.py ...` | **178 项全部通过，0 失败**（11 项因本机缺 tldextract/feedparser 等重依赖跳过） |
| 功能验证脚本 | `python _verify_fixes.py` | UnicodeSanitizer/Pydantic validator/URL 归一化 3/3 通过 |
| AST 语法校验 | 14 个核心修改文件 | 全部 OK |
| `Test/test_vectordb_fixes.py` | pytest | 跳过（本机缺 chromadb；生产环境已装则运行） |
| `Test/test_m3_queue_mongo.py` | — | 未运行（需真实 MongoDB，按 M 规则单独执行） |

关键回归点实测通过：search_mode 白名单（非法值 400）、游客钳制全模式生效、XFF 不可信即忽略、RSS XML 输出、SSRF 全家族（环回/私网/链路本地/十进制/十六进制/八进制 IP/v4-mapped-v6/重定向内网）、404/410 终态标记、503 可重试错误透出、None pub_time 合法、日/韩/俄/阿判定需翻译、正常标题不再被回顾词误杀、URL 去重写回一致。

---

## 四、遗留项（未在本批处理）

- **P2/P3 清单**：见 `docs/review_fix_decisions.md`（相似消息降权接线决策、M3 护栏接线时机、依赖收敛、UI 一致性等）。
- **环境依赖**：`tldextract`/`feedparser`/`chromadb` 未装的精简环境，相关测试自动跳过属预期；生产部署（群晖）依赖完整。
- **`_run_tests.py` 与 pytest 的 importorskip 兼容**：`test_vectordb_fixes.py` 需用 pytest 运行（unittest 加载器不处理模块级 importorskip）。

---

**修复完成日期**：2026-08-27
**测试**：178 通过 / 0 失败 / 12 环境跳过