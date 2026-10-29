# IIS 遗留项修复任务 Prompt（P2 收尾批次）

> 使用方式：在新会话中将本文件全文作为任务粘贴给 Cline（建议先切 PLAN 模式核对现状，再切 ACT 实施）。

---

## 任务

修复 IIS 全面审查报告中确认遗留的 P2/收尾项。13 个 P0 与绝大部分 P1 已修复验证（见 `P0_FIXES_SUMMARY.md`、`docs/review_fix_decisions.md`），本任务只处理下列遗留项。

## 全局约束（必须遵守）

1. 不修改 Git 子模块：`AIClientCenter/`、`IntelligenceCrawler/`、`VectorDB/`、`MyPythonUtility/`、`PyLoggingBackend/`。涉及子模块的项只能通过主仓库适配层处理，或在文档标注跳过原因。
2. 不调用真实付费 API；不执行 git commit/push/reset。
3. 修改 `requirements*.txt` 前需向用户确认。
4. 新增 Python 代码使用类型标注；注释与日志用简体中文。
5. 每批次完成后运行 `python _run_tests.py` 回归；JS 用 `node --check`；Python 用 `ast.parse` 校验。
6. 遵守 `.clinerules/`：并发约束（爬虫 2 / Playwright 1 / PDF 1 / AI 1）；一次只实施一个批准批次。

## 修复清单

### 批次 A：时间处理统一（前端对齐后端 ISO 8601 带偏移）

- [x] A1 `templates/entity_frequency.html`（约 1177-1193、1273 行）：前端把浏览器本地日期直接拼 `T00:00:00+00:00` 冒充 UTC。改为按浏览器实际时区偏移拼接（`-new Date().getTimezoneOffset()/60` 转为 `±HH:MM`），保证时间窗边界不漂移。（✅ 此前已完成：`browserTimezoneOffsetSuffix()` 已实现，见 templates/entity_frequency.html:409 起）
- [x] A2 `templates/intelligence_search.html`（约 190/203/272/285 行 "(UTC)" 标签）+ `static/js/intelligence_search.js` flatpickr：采集的是本地时间，标签却写 UTC。二选一：标签改为显示本地时区名；或提交前把本地时间转换为带偏移 ISO 字符串（与 A1 同法）。（✅ 此前已完成：intelligence_search.js:383 起已实现偏移拼接 + `tz-suffix-label` 填充 GMT 偏移）
- [x] A3 验证：构造 UTC+8 用例，确认查询窗口起点/终点与用户所选一致（可用 console.log 对比 `new Date(iso)` 与选取值）。（✅ 此前已有 node 断言脚本验证 UTC+8 偏移为 +08:00；本次未重跑）

### 批次 B：爬虫性能（主仓库 Scraper/ 可改，子模块不动）

- [x] B1 `Scraper/PlaywrightRawScraper.py`（`:77` 每次 `chromium.launch(**options)`）与 `Scraper/PlaywrightRenderedScraper.py`：改为类级/模块级共享常驻 browser，每次抓取新建 context（隔离 cookie/storage），并提供 `close()` 供任务停止时释放。参考 `IntelligenceCrawler/Fetcher.py` 的常驻模式（只读参考，不修改）。（✅ 此前已完成：模块级共享浏览器池 `_shared_browsers`，见 PlaywrightRawScraper.py:144 起）
- [x] B2 保持 Playwright 并发为 1（不引入并发放大）；验证热重载路径（`CrawlerServiceEngine.py` 已有 `module_fini` + 资源兜底清理，确认新常驻 browser 会被兜底关闭）。（✅ 本次完成：`_handle_shutdown` 末尾整池关闭（try/except 保护）；池跨热重载保留属设计，热重载路径不关池，并发不变）

### 批次 C：前端收尾

- [x] C1 favicon：新增 `static/favicon.ico`（或 svg），`templates/base.html` 与独立页模板加 `<link rel="icon">`。（✅ 本次完成补齐缺口：login.html / monitor_dashboard.html 补 icon 链接，写法与 base.html:10 一致；favicon.svg 与其余页面此前已有）
- [x] C2 页脚硬编码 © 2025：先全局搜索 `© 2025`/`©2025`，改为 Jinja `{{ now.year }}` 或后端注入年份变量（若实际未硬编码则跳过并记录）。（✅ 核实全仓模板无 `© 2025` 硬编码，按任务约定跳过）
- [x] C3 `templates/export_mongodb.html`（报告指认约 163-166 行）导出进度条假反馈：先核实现状；改为真实进度轮询或明确"任务已提交，请稍后到导出目录查看"提示，并确保 `resetUI` 复位全部控件。（✅ 此前已完成：已有"任务已提交"提示与 resetUI 复位）
- [x] C4 双导航（`base.html` vs `_standalone_nav.html`）与"情报聚类/情报聚合"命名不一：统一命名与链接结构（低风险合并，不改功能）。（✅ 此前已完成：命名已统一为"情报聚类"（base.html:43 与 _standalone_nav.html:6 一致）；两套模板文件仍并存，未做物理合并）

### 批次 D：依赖卫生（改 requirements 前须用户确认）

- [x] D1 `requirements.txt:46-47` urllib3==1.26.20：评估升级 2.x 与 requests/其他依赖的兼容矩阵；能升则升（同时更新 requirements-server.txt），不能升则在 TODO 注释写明阻塞原因。（✅ 此前已完成"不能升则写 TODO"分支：TODO 已写明 freeze 实为 2.3.0、升级需先评估 requests 兼容性）
- [ ] D2 `requirements.txt` 未钉版本的包：`pip freeze` 对照后补钉版本号。（未做：本批次禁止修改 requirements；streamlit 未钉行的处置建议见 2026-10-12 交付报告任务 11 调查——代码无引用、环境未安装，建议移除或注释）
- [x] D3 torch：在 requirements 中加注释"群晖无 GPU，Linux 安装请使用 CPU 索引源 `--index-url https://download.pytorch.org/whl/cpu`"。（✅ 此前已完成：CPU 索引源注释已写入 requirements.txt:35-37）
- [x] D4 运行 `pip-audit`（仅本地检查，不安装修复），结果写入 `docs/dependency_audit.md`：高危列出处置建议，低危记录备查。（✅ 本次完成：pip-audit 2.10.1 审计当前环境，19 包 105 条记录，报告已写入 docs/dependency_audit.md；Flask-Cors 已于 freeze 升 4.0.1 故未再报出；urllib3 本机 2.7.0 仍有 3 漏洞，建议升级目标定为 ≥2.8.0）

### 批次 E：代码卫生

- [x] E1 `ProcessCotrolException` 两份拷贝（`Tools/` 与 `IntelligenceCrawler/`，后者为子模块不可改）：主仓库侧统一从 `Tools/` 导入；如签名已分叉，在 `Tools/` 版本做兼容（保留旧签名参数默认值），并在文件头注明"IntelligenceCrawler 子模块内同名类为其内部副本，勿再引用"。（✅ 本次完成：文件头 docstring 已注明；主仓库统一从 `Tools.` 导入此前已达成（IntelligenceHub.py / Workflow/ / Test/ 均如此））
- [x] E2 `import *` 泄漏：`CrawlerServiceEngine.py:15` 等处 `from GlobalConfig import *`——为目标文件补显式 `import time/datetime/...`（不强制重构 import *，只消除隐式依赖，防 GlobalConfig 加 `__all__` 即炸）。（✅ 本次完成：顶部补显式 `import os`；time/sys/hashlib 等标准库本已显式，GlobalConfig 传递的仅剩 LOG_PATH 等配置常量，属预期传递而非标准库泄漏）
- [x] E3 `ServiceComponent/IntelligenceHubDefines_v2.py`：EVENT_TITLE/EVENT_BRIEF/EVENT_TEXT/TIPS 等文本字段加 `Field(max_length=...)` 超长截断（validator 中截断并记 warning，不拒收，避免丢数据）。（✅ 此前已完成，复核确认：`PROCESSED_FIELD_MAX_LENGTH`（:184）+ `_truncate_overlong_fields` model_validator（:347）截断并记 warning，截断事实写入 `APPENDIX.__FIELD_TRUNCATED__`（:437），不拒收；P2 收尾批次报告误标"未做"，本次更正勾选）
- [x] E4 `recycled/`（46+ 死文件）：不得删除或移动（项目规则禁止）。改为在 `README.md` 或 `docs/` 增加"recycled/ 为历史废弃代码，禁止引用"标注；如需进一步处理需用户单独批准。（✅ 本次完成：README.md 目录结构段落与 AGENTS.md 3.2 节各加标注）
- [x] E5 `prompts_v2x.py` V25 瑕疵：先定位"刚/则"错字、规则 4 与 9 重叠、GEOGRAPHY 分号分隔漏检（查询侧正则只认逗号）三处；若错字已不存在则只处理后两项：GEOGRAPHY 解析正则兼容中英文分号，prompt 保持不变或微调。（✅ 本次完成：删除重复硬性规则 5（规则 4 的弱化子集），规则 6—15 重编号为 5—14，规则 4 文本不动；错字已不存在、GEOGRAPHY 分号查询侧此前已兼容中英文分号。prompt 语义变化：仅删除冗余弱规则，约束强度不变）

### 批次 F：文档回填

- [x] F1 `docs/implementation_plan.md`：M4-M8 相关组件（PdfSecurityValidator、SSRFGuard、PushNotifier、ApiTimePolicy、BudgetGuard 等）代码与测试已存在，但状态仍"待核验"——逐项核对 `Test/test_m4_*.py`~`test_m8_*.py` 实际运行情况后更新状态列。（✅ 本次完成：M4—M9 状态按 `docs/acceptance_checklist.md` 事实回填为"代码与离线测试已完成（✅）；实机验收 ⬜"，头部注记追加回填日期）
- [x] F2 `Test/TestXxx.py` 演示脚本命名不符合 pytest 收集规则（`test_*.py`）：在 `AGENTS.md` 第 6 节注明"Test/TestXxx.py 为手动演示脚本，pytest 不会收集"；改名需用户批准。（✅ 核实说明已存在于 AGENTS.md 第 6 节（"Test/ 下不带 test_ 前缀的旧文件为手动演示脚本，不属于自动化套件"），无需重复新增；改名未执行，需用户批准）
- [x] F3 修复完成后，将本文件中各批次勾选状态同步，并在 `docs/review_fix_decisions.md` 追加"P2 收尾批次完成记录"。（✅ 本次完成：本文件勾选已同步；完成记录见 docs/review_fix_decisions.md"P2 收尾批次完成记录（2026-10-12）"）

## 明确不做（里程碑约束，需单独批准）

- M3 护栏接线（ConsumerMutex/BudgetGuard/ApiTimePolicy/OffPeakQueueDrainer 接入 `IntelligenceHubStartup`）——属 M3 里程碑。
- Docker/部署结构调整——属 M7。
- 巨型文件拆分（6 个 >1500 行）——大重构，单独任务。

## 验收标准

1. `python _run_tests.py` 全部通过（0 失败；环境缺依赖的跳过如实报告）。
2. 修改的 JS 全部通过 `node --check`。
3. 修改的 Python 全部通过 `ast.parse` 语法校验。
4. 输出变更文件清单 + 每批次验证结果（通过/失败/跳过如实记录）。
5. 未触碰 Git 子模块；未执行 git 提交类操作。

---

**优先级建议**：A（数据正确性）→ B（资源泄漏）→ C（体验）→ D（安全依赖）→ E/F（卫生与文档）。可分多次会话按批次执行。