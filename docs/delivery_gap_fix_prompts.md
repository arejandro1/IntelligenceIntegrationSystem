# IIS 交付收尾逐项修复提示词（2026-10 核查后生成）

> 使用方式：每条提示词自包含，复制到新会话单独执行（建议先 PLAN 核对现状再 ACT）。
> 依据：`docs/acceptance_checklist.md`（M0—M9 验收）、`docs/fix_remaining_prompt.md`（P2 批次 A—F）、
> 2026-10 代码实测核查（403 用例：390 通过 / 0 失败 / 1 错误 / 12 环境跳过）。
>
> **每条提示词执行时，需连同下面"全局约束"一并粘贴。**

## 全局约束（所有提示词通用）

1. 不修改 Git 子模块：`AIClientCenter/`、`IntelligenceCrawler/`、`VectorDB/`、`MyPythonUtility/`、`PyLoggingBackend/`。
2. 不调用真实付费 API；不执行 git commit/push/reset（批次 0 除外，由用户本人执行）。
3. 修改 `requirements*.txt` 前先向用户确认。
4. 新增 Python 代码使用类型标注；注释与日志用简体中文。
5. 每完成一项运行回归：`python _run_tests.py <相关模块>`（提示词 2 完成后可无参数全量跑）；Python 改动用 `ast.parse` 校验，HTML/JS 改动用浏览器或 `node --check` 校验。
6. 发现提示词描述与代码现状不符时，以代码现状为准并在结果中如实说明差异，不得强行照搬。

---

## 批次 0：版本控制入库（用户本人操作，非 AI 任务）

当前 `feature/china-watch-mvp` 分支有 90+ 已跟踪文件改动、50+ 未跟踪文件（含几乎全部新测试 `Test/test_m3_*`/`test_p0_*`/`test_p1_*`/`test_fix*`/`test_review_*`），4 个子模块有新提交指针。交付前必须入库：

```bash
git status --short                 # 核对改动清单
git submodule status               # 确认 4 个子模块的新提交是预期内的
git add -A                         # 或按模块分批 add，add 前过一遍未跟踪文件清单
git diff --cached --stat           # 确认没有混入 _data/_log/_config 真实配置（应已被 .gitignore 拦截）
git commit                         # 提交信息建议按里程碑分组或一次 chore 提交
```

注意：若子模块新提交并非预期，先回退子模块检出再提交主仓库。`_test_result.txt` 已被忽略，无需处理。

---

## 提示词 1：修复 test_m3_queue_mongo 测试桩（唯一测试错误）

**背景**：`python _run_tests.py Test.test_m3_queue_mongo` 有 1 个 ERROR：`test_duplicate_enqueue_no_op`。生产代码 `ServiceComponent/PersistentQueue.py` 的 `MongoQueueBackend.insert_if_absent`（约 343 行，批次三修复后）只认三种重复键信号：`e.code == 11000`、异常类名含 `DuplicateKey`、消息含 `duplicate key`（带空格）；而测试桩 `Test/test_m3_queue_mongo.py:43` 抛的是裸 `Exception("DuplicateKeyError")`，三条都不满足，被当作"存储故障"重新抛出。

**任务**：
1. `Test/test_m3_queue_mongo.py`：顶部 `from pymongo.errors import DuplicateKeyError`，第 43 行改为 `raise DuplicateKeyError("duplicate key error: E11000 ...", code=11000)`。
2. 只改测试桩，不改 `PersistentQueue.py` 生产逻辑。
3. 顺带核对：该文件 docstring 写明"使用 mock collection，不连接真实 MongoDB"，与 `AGENTS.md` 第 6 节"需真实 MongoDB"的描述冲突——文档更正见提示词 12，本提示词不改文档。

**验收**：`python _run_tests.py Test.test_m3_queue_mongo` 全部通过，0 错误。

---

## 提示词 2：增强 _run_tests.py（默认全量发现 + 模块级隔离）

**背景**：`_run_tests.py` 两个缺陷：① 无参数时只跑 `Test.test_m1_config` 一个模块，易误以为"全绿"；② 多模块加载无隔离，任一模块 import 期抛异常（如 `Test/test_vectordb_fixes.py` 顶层 `pytest.importorskip("VectorDB.VectorDBBService")` 在缺 chromadb 环境抛 pytest 的 `Skipped`）会中断整个运行。

**任务**（只改 `_run_tests.py`，保持 `_test_result.txt` 输出契约不变：`SUCCESS=/TESTS_RUN=/FAILURES=/ERRORS=` 四行）：
1. 无参数时：扫描 `Test/test_*.py`（glob，按文件名排序），转为模块名列表全量运行。带参数时保持现有行为（按给定模块名运行）。
2. 模块级隔离：逐模块分别 `loadTestsFromName` + 独立 `TextTestRunner` 运行到同一输出缓冲，单模块 import 失败不影响后续模块；最后汇总总用例数/失败/错误/跳过，按汇总结果决定退出码。
3. import 期异常分类：若异常类名为 `Skipped`（pytest importorskip 抛出）或属于 `unittest.SkipTest`，该模块记为"整模块跳过"并在末尾汇总打印；其他异常记为该模块加载错误（计入 ERRORS，打印 traceback）。
4. 末尾汇总打印每个模块的 通过/失败/错误/跳过 计数一行，便于扫读。

**验收**：
- `python _run_tests.py` 无参数跑全部 `Test/test_*.py`；本机环境下 `test_vectordb_fixes` 显示为模块跳过而非中断；其余 31 个模块 403 用例 0 失败 0 错误（提示词 1 已先完成）。
- `python _run_tests.py Test.test_m1_config` 带参数行为不变。

---

## 提示词 3：B2 常驻浏览器关闭接线（爬虫热重载资源收尾）

**背景**：`Scraper/PlaywrightRawScraper.py:144-209` 实现了模块级共享浏览器池 `_shared_browsers` + `_get_shared_browser()`（按 线程ident+headless+代理签名 为键）+ 公开的 `close_shared_browsers()`（约 189-209 行），但 **`close_shared_browsers()` 全仓无调用方**。`PlaywrightRenderedScraper.py` 委托同一 `request_by_browser`，自动共享同一池。`CrawlerServiceEngine.py:370-449` 的 `_release_plugin_resources` 已有 `module_fini` + 插件属性自动发现（pipeline/flow/crawler/browser/playwright/scraper 的 shutdown/close）兜底，但发现不到 Scraper 模块级池。目前浏览器仅靠 atexit 钩子和 `IntelligenceCrawler/BrowserMonitor.py` 的 `report_leaks/force_close_real` 在进程退出时兜底。

**任务**：
1. 先梳理生命周期：池的键含线程 ident，爬虫任务线程随热重载生灭；`Scraper/` 是主仓库代码不随 `CrawlTasks/` 热重载。确认"插件热重载时关整池"是否会误杀其他仍在运行任务的浏览器。
2. 接线方案（按安全性二选一，并在代码注释说明理由）：
   - **保守（推荐）**：只在爬虫服务整体关停路径调用 `close_shared_browsers()`（找到 `CrawlerServiceEngine.py` 中 TaskManager/主流程的 shutdown 位置）；热重载不关池，注释说明"池按签名复用，跨热重载保留属设计"。
   - **进阶**：给池补充"按线程 ident 关闭子集"的能力，任务线程结束时只关自己那条线程的浏览器。
3. 不得把 Playwright 并发放大（保持并发 1 约束）。
4. 检查 `Test/` 下是否有覆盖 `_release_plugin_resources` 的现有测试（如 test_review_crawler 相关），有则扩展断言关闭被调用；没有则不为本项新建测试文件，用 `ast.parse` + 回归兜底。

**验收**：`python _run_tests.py`（或相关模块）回归通过；关停路径上 `close_shared_browsers()` 有明确调用方；注释写清热重载与整池关闭的边界。

---

## 提示词 4：C1 login/monitor 页面补 favicon

**背景**：`static/favicon.svg` 已存在，`<link rel="icon">` 已加到 `templates/base.html:10`、`entity_frequency.html`、`intelligence_dashboard.html`、`intelligence_cluster_list.html`、`intelligence_graph.html`；但 `templates/login.html` 与 `templates/monitor_dashboard.html` 没有 favicon 链接。

**任务**：在上述两个模板的 `<head>` 内补 `<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">`，写法与 `base.html:10` 完全一致（注意两个页面若用 `url_for('static', ...)` 则保持一致风格）。

**验收**：grep 全仓模板确认每个独立 HTML 页都有 icon 链接；页面无其他改动。

---

## 提示词 5：D2 streamlit 版本收敛（先确认，再动手）

**背景**：`requirements.txt:96` 的 `streamlit` 未钉版本，注释称"freeze 未收录，暂无参照版本，保持不钉"。项目规则：改 requirements 前须用户确认。

**任务**：
1. 先调查再决定：`grep -rn "import streamlit\|from streamlit" --include=*.py` 确认仓库里是否真的用到 streamlit；`pip show streamlit` 看当前环境装没装、什么版本。
2. 三种处置按调查结果选择，**执行前把结论告知用户并取得确认**：
   - 代码完全未引用且环境未安装 → 从 requirements.txt 移除（注明移除理由）；
   - 有引用且环境已安装 → 钉到当前安装版本，注释更新；
   - 有引用但未安装 → 保留不钉，注释写明"待首次安装后补钉"。
3. 若 `requirements-server.txt` 也有 streamlit，同步处理。

**验收**：用户已确认处置方式；requirements 注释与事实一致。

---

## 提示词 6：D4 依赖安全审计（pip-audit → docs/dependency_audit.md）

**任务**：
1. `pip install pip-audit`（仅本地检查工具，不改项目依赖）。
2. 运行 `pip-audit`（优先对当前环境；如支持再对 `-r requirements.txt` 跑一次），**只检查不修复**。
3. 结果写入 `docs/dependency_audit.md`：高危/严重逐条列出（包名、版本、CVE/GHSA 编号、修复版本、处置建议——升级/替换/接受风险）；低危汇总备查。已知在案的两条要呼应：`Flask-Cors==4.0.0` CVE-2024-1681（freeze 已升 4.0.1，注明状态）、`urllib3==1.26.20`（`requirements.txt:45-47` 已有 TODO：freeze 实为 2.3.0，升级需评估 requests 兼容性）。
4. 不改动任何 requirements 文件。

**验收**：`docs/dependency_audit.md` 存在且内容完整；审计命令与日期记录在文档头部。

---

## 提示词 7：E1 ProcessCotrolException 文件头注明

**背景**：`ProcessCotrolException` 有两份拷贝：`Tools/ProcessCotrolException.py`（主仓库，已做签名兼容：保留旧签名参数默认值）与 `IntelligenceCrawler/` 子模块内同名文件（不可改）。主仓库导入统一已完成（`IntelligenceHub.py`、`Workflow/CommonFlowUtility.py`、`Workflow/RssFeedsBasedCrawlFlow.py` 均从 `Tools.` 导入），缺文件头警示。

**任务**：在 `Tools/ProcessCotrolException.py` 文件头 docstring 顶部追加注明（简体中文）："本项目内 `IntelligenceCrawler/` 子模块存在同名内部副本，为其内部实现，主仓库代码一律从 `Tools.ProcessCotrolException` 导入，勿再引用子模块内副本。"不改任何代码逻辑。

**验收**：`ast.parse` 通过；全文 grep 确认无新增从子模块路径导入该类的主仓库代码。

---

## 提示词 8：E2 CrawlerServiceEngine 补显式 import os

**背景**：`CrawlerServiceEngine.py:15` 保留 `from GlobalConfig import *`（清单允许），已补显式 `import time/queue/hashlib/threading` 等，但 `:26-27` 起大量使用的 `os`（`os.getpid()`、`os.path...`）未显式导入，靠 `GlobalConfig.py:2` 顶层 `import os` 经 `import *` 隐式泄漏（GlobalConfig 无 `__all__`，加 `__all__` 即炸）。同批 `IntelligenceHubWebService.py`、`IntelligenceHubStartup.py` 均已显式 import os。

**任务**：
1. `CrawlerServiceEngine.py` 顶部 import 区补 `import os`。
2. 顺带用 `python -m pyflakes CrawlerServiceEngine.py`（无 pyflakes 则 `pip install pyflakes`，仅检查）扫一遍其他隐式依赖；只补标准库/明确在用的显式 import，**不重构 `import *` 本身**，不处理无关警告。

**验收**：`ast.parse` 通过；`python _run_tests.py`（提示词 2 完成后则无参数全量）回归无新增失败。

---

## 提示词 9：E5 prompts_v2x 硬性规则去重（规则 4/5 合并）

**背景**：`docs/fix_remaining_prompt.md` 原写"规则 4 与 9 重叠"，实测（`prompts_v2x.py:612-613`）是**规则 4 与规则 5** 重叠：规则 4"总结性文章所有维度≤4 分且 REASON/TIPS 必须说明"，规则 5"回顾/总结必须低分"——规则 5 是规则 4 的弱化子集。另两处原清单问题经核查已不存在（"刚/则"错字无残留；GEOGRAPHY 查询侧 `ServiceComponent/IntelligenceQueryEngine.py:604-607` 已兼容中英文分号）。

**任务**：
1. 先 `grep -rn "硬性规则" Test/ ServiceComponent/ IntelligenceHub.py` 及搜索规则编号引用（如 `"规则 4"`），确认没有代码/测试按编号引用规则；有引用则改为同步更新。
2. 删除规则 5（第 613 行），后续规则 6—15 重新编号为 5—14。规则 4 文本保持不变。
3. 注意：这是 AI prompt 变更，会影响分析行为；在结果说明中明确标注"prompt 语义变化：仅删除冗余弱规则，约束强度不变"。
4. 不改 V25 其他内容；版本号/日期头部如有维护惯例（对照文件头部现状）再决定是否加注。

**验收**：`ast.parse` 通过；`python _run_tests.py` 中涉及 prompt 的测试（如 test_m2_prefilter、test_fix1_ai_gate）回归通过。

---

## 提示词 10：E4 recycled/ 禁止引用标注

**背景**：`recycled/` 有 46+ 个历史废弃文件，项目规则禁止删除/移动，但全仓 `.md` 无任何"禁止引用"标注，新会话/新人容易误引。

**任务**：在 `README.md` 的目录结构相关段落（无则在合适位置新增一小节）和 `AGENTS.md` 第 3.2 节目录说明中，各加一句（简体中文）："`recycled/` 为历史废弃代码存档，仅供追溯，禁止在新代码中引用；按项目规则不得删除或移动。"

**验收**：grep `recycled` 在两个文件中命中新标注；无其他改动。

---

## 提示词 11：F1 implementation_plan.md 状态回填

**背景**：`docs/implementation_plan.md:21-26` 里程碑总览表 M4—M9 的"是否允许真实 API 调用"列仍全为"待核验"，各节状态 likewise；而 `docs/acceptance_checklist.md`（2026-09-21/2026-10-04 修订）已记录真实状态：M4—M9 代码与离线测试均已完成并验证，仅剩实机验收项。

**任务**：
1. 逐项对照 `acceptance_checklist.md` 的 M4—M9 章节，把 `implementation_plan.md` 总览表与各节状态更新为事实状态，推荐措辞："代码与离线测试已完成（✅ 见验收清单）；真实 API 调用/实机验收待目标环境（⬜）"。**不得写成"已验收"**，实机项保持 ⬜。
2. 文件头部修订注记追加一行本次回填日期与说明（沿用现有 `> 状态（日期 修订）` 风格）。
3. 只改状态描述，不改里程碑原始计划内容（文件已声明"正文保留原始计划内容供追溯"）。

**验收**：两文档同一条目状态互相一致，无矛盾表述。

---

## 提示词 12：F3 文档同步（P2 完成记录 + 复选框 + AGENTS.md 过时描述）

**背景**：P2 批次 A—F 实际已完成 10 项（A1、A2、B1、C2、C3、C4、D1、D3、E3、F2），但 `docs/fix_remaining_prompt.md` 复选框全未勾选、`docs/review_fix_decisions.md` 无完成记录。另有两处过时描述：① `AGENTS.md` 第 6 节称 `test_m3_queue_mongo` "需要真实 MongoDB"，实测该文件 docstring 写明"使用 mock collection，不连接真实 MongoDB"；② `docs/acceptance_checklist.md:53` 同条"需真实 MongoDB 单独执行"同样过时。

**任务**（假设提示词 1—11 已全部完成；未完成的项按实际状态勾选/不勾）：
1. `docs/fix_remaining_prompt.md`：勾选实际已完成项的复选框；未做/部分做项保持原状并在行尾补一句现状（如 B2 的接线结论）。
2. `docs/review_fix_decisions.md` 末尾追加"P2 收尾批次完成记录"小节：日期、逐项结论（含本批次新完成的 1—11 项）、验证命令与结果摘要。
3. 更正 `AGENTS.md` 第 6 节与 `docs/acceptance_checklist.md:53` 的 test_m3_queue_mongo 描述为"内存 fake，不依赖真实 MongoDB"。

**验收**：三份文档与代码现状一致；grep 无残留"需真实 MongoDB"的旧表述指向该测试。

---

## 提示词 13：实机验收 runbook（非代码任务，目标环境执行）

代码侧收尾（提示词 0—12）完成后，以下项只能在目标环境验收，按 `docs/acceptance_checklist.md` 的 ⬜ 项逐项执行并回填勾选：

1. **M3 真实 API**：人工启用 `API_ENABLED=true`（配套 `API_DRY_RUN=false`、`USE_OFF_PEAK_DRAINER=true`），小额预算试运行，核对费用台账记账与实际账单一致。
2. **M4/M9 实网抓取**：17 个来源逐源验收抓取成功率；确认并发限制（爬虫 2 / Playwright 1）实机生效。
3. **M6 推送渠道**：配置真实 webhook，验证连通性、失败重试退避、日志不泄露 token。
4. **M7 群晖部署**：四容器（或 P0-4 修订后的三应用容器 + 外部 MongoDB）实机部署；磁盘预留检查；MongoDB 不公网映射；非 root 运行确认。
5. **M8 连续运行**：≥7 天连续运行；内存 ≤20GB；磁盘 70%/85%/95% 三级降级真实触发与恢复。
6. **补充项**：真实 MongoDB 验证 IN_FLIGHT 恢复与对账重放；Playwright 真实浏览器 route 拦截（SSRF 防护）实测。

每完成一项，在 `docs/acceptance_checklist.md` 勾选并注明验收日期与环境。

---

**建议执行顺序**：批次 0（入库）→ 1、2（测试基线转绿）→ 4、7、8、9、10（分钟级小修）→ 3（B2 需谨慎设计）→ 5、6（依赖卫生）→ 11、12（文档同步，最后做以反映最终状态）→ 13（实机验收）。
