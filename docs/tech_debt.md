# 技术债登记册

> 汇总已识别但暂不处理的技术债。每项含现状、影响、建议处置与触发条件。
> 新建大型重构前先看这里，避免重复评估。
> 最近更新：2026-10-29

## T1 巨型文件拆分（已决策暂缓，2026-10-29 用户确认）

| 文件 | 行数 | 建议拆分方向 |
|------|------|--------------|
| `IntelligenceHub.py` | ~2500 | 队列工作线程 / 定时任务 / 聚合与向量接线 / 查询门面 拆为 4 模块 |
| `IntelligenceHubWebService.py` | ~2440 | 路由注册 / 页面渲染 / RPC 接线 / 公共搜索限制 拆为 4 模块 |
| `templates/entity_frequency.html` | ~1340 | JS 抽到 static/js/，样式抽到 static/css/ |

- 影响：评审与定位成本高，但功能稳定、测试覆盖到位（403 用例）。
- 触发条件：对这两个文件做较大功能改动时，顺便拆分；或专门立项。
- 注意：拆分必须保持 `IntelligenceHub` 构造签名与公开方法不变（WebService 大量依赖）。

## T2 `import *` 残留（5 处）

`CrawlerServiceEngine.py:16`、`IntelligenceHub.py:27`、`IntelligenceHubStartup.py:18`、
`IntelligenceHubWebService.py:21`、`_config/ai_client_config_example.py:8`。

- 现状：已补显式标准库 import 兜底（防 GlobalConfig 加 `__all__` 即炸），风险已缓解。
- 建议：下次大改对应文件时收敛为显式导入；不建议单独为其重构。

## T3 依赖安全遗留（详见 docs/dependency_audit.md 第五节）

- `cryptography 45.0.3 → 50.0.0`：大版本，需评估 pyOpenSSL 25.1.0 兼容性；
- `ecdsa` / `python-jose`：无修复版本，建议评估从依赖树移除或替换为 pyjwt+cryptography；
- `streamlit` 未钉版：仅 `ModelTraining/validation2_review_app.py` 引用，
  建议移入 `ModelTraining/` 独立 requirements 或钉版（需用户确认）。

## T4 已知小项

- `ServiceComponent/UserManager.py:40-42` 裸 `except Exception: pass` 可能掩盖迁移异常，建议复核；
- `IntelligenceQueryEngine` 排序固定为 `APPENDIX.__TIME_ARCHIVED__` 倒序（:118、:739 附近），
  如需多排序维度再做配置化；
- `IntelligenceHub.py:1849` 附近 informant 查重性能优化（`TODO: Optimise informant check`）；
- 根目录 `_情报研判结果.txt` 为一次性分析产物，已被 .gitignore 忽略，不入库；
- `Test/vectordb_stress_test.py` 压测脚本依赖实时 VectorDB 服务，未纳入自动化套件，
  建议作为手动/夜间流程记录。

## T5 子模块远端备份

- 5 个子模块（AIClientCenter/IntelligenceCrawler/VectorDB/MyPythonUtility/PyLoggingBackend）
  的 origin 指向上游 SleepySoft 仓库，当前账号无推送权限；
- 2026-10-29 已在本地完成子模块提交（HEAD 即各自 main），但远端备份需在 GitHub
  创建同名 fork 后推送（fork 后把子模块 origin 改指向 fork 或新增 remote 推送）。
