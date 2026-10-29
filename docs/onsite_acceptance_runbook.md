# 实机验收 Runbook（目标环境执行）

> 用途：代码与离线测试已全部完成（本地全量 403 用例 0 失败）。本文件汇总
> 只能在目标环境（群晖/真实网络/真实 API）执行的验收项，逐项执行后回填
> `docs/acceptance_checklist.md` 对应 ⬜ 并注明日期与环境。
>
> 生成日期：2026-10-29。执行人：____

## 0. 前置条件

- [ ] 代码已部署到目标环境（git clone + `git submodule update --init --recursive`）
- [ ] 依赖按 `requirements-server.txt`（生产）或 `requirements.txt`（开发）安装
- [ ] `_config/config.json`、`_config/ai_client_config.py` 已按实际填写
- [ ] `.env` 已从 `.env.example` 复制并填写（MongoDB、Token 等）
- [ ] 已创建管理员账号：`python Scripts/UserManagerConsole.py`

## 1. M3 真实 API 启用验收

- [ ] 人工启用：`api_enabled=true`、`api_dry_run=false`、`use_off_peak_drainer=true`、
      `use_legacy_ai_worker=false`（config.json 的 intelligence_hub 段）
- [ ] 小额预算（如日预算 5 元）试运行 24 小时
- [ ] 核对费用台账（ApiUsageLedger）记录与实际 DeepSeek 账单一致
- [ ] 核对高峰时段真实调用次数为 0（峰谷策略生效）
- [ ] 回填 acceptance_checklist.md M3 对应 ⬜

## 2. M4/M9 实网抓取验收

- [ ] 17 个来源（sources/china_watch_sources.yaml）逐源确认抓取成功
- [ ] 普通爬虫并发 2、Playwright 并发 1 实机生效（观察进程/日志）
- [ ] 代理配置（global_site_proxy）在目标网络环境可用
- [ ] 连续 3 天采集成功率抽查

## 3. M6 推送渠道验收

- [ ] 配置真实 webhook（intelligence_hub.push_notifier.*）
- [ ] 触发一条高价值情报推送，确认渠道收到
- [ ] 模拟渠道故障，确认有限重试 + 退避生效
- [ ] 检查日志无 webhook token 泄露

## 4. M7 群晖部署验收

- [ ] 按 docs/synology_deployment.md 部署三应用容器 + 外部 MongoDB
      （模式 A）或本地 mongo 容器（模式 B，AVX 主机）
- [ ] MongoDB 端口未映射公网；管理界面仅内网/反代访问
- [ ] 容器非 root 运行确认（`docker exec ... id`）
- [ ] 磁盘预留达标；WiredTiger 缓存 0.5GB（模式 B）
- [ ] 注意：容器编排不含 VectorDB，语义搜索/聚类不可用属预期
      （如需向量能力，另机部署 VectorDBBService 并配置 intelligence_hub.vectordb.*）

## 5. M8 连续运行与资源验收

- [ ] ≥7 天连续运行无异常重启（Launcher 深度探活 ?deep=1 不触发误重启）
- [ ] 内存占用 ≤ 20GB 约束确认
- [ ] 磁盘 70%/85%/95% 三级降级真实触发与恢复（可用大文件填充测试盘验证，
      恢复后确认消费者自动恢复认领）
- [ ] 日志轮转与历史归档正常（_log/history_log/）

## 6. 补充验证项

- [ ] 真实 MongoDB 后端验证 IN_FLIGHT 恢复与对账重放
      （重启服务，观察未确认任务被重放且不重复调用 AI）
- [ ] Playwright 真实浏览器 route 拦截（SSRF 防护）实测：
      构造会跳转内网的页面，确认后续导航/子请求被阻断
- [ ] 向量链路（如部署 VectorDB）：装全依赖后重跑 `python _run_tests.py`，
      确认本地被跳过的 13 个用例（chromadb/tldextract/feedparser/playwright）转绿
- [ ] VectorDB 压测：`Test/vectordb_stress_test.py`（需先启动向量服务）

## 7. 验收记录

| 日期 | 项目 | 环境 | 结果 | 签名 |
|------|------|------|------|------|
|      |      |      |      |      |
