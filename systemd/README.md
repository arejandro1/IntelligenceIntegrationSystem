# IIS systemd 服务配置

本目录提供 IIS 三个核心服务的独立 systemd 单元文件，替代旧的单一 `iis.service`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `iis.env.example` | 三个服务共享的环境变量模板；复制为 `iis.env` 并填写实际值（`iis.env` 含敏感令牌，不入库） |
| `iis-vectordb.service` | 向量数据库服务（VectorDBBService） |
| `iis-web.service` | Web 核心服务（IntelligenceHubLauncher） |
| `iis-crawler.service` | 爬虫引擎服务（CrawlerServiceEngine） |
| `iis.target` | 一键拉起全部三个服务 |

## 安装步骤

先准备环境变量文件（首次部署）：

```bash
cp systemd/iis.env.example systemd/iis.env
# 编辑 iis.env，把 PROJECT_DIR / PYTHONPATH 改为实际路径，并按需生成 VECTOR_ADMIN_TOKEN
```

然后安装单元文件：

```bash
sudo ln -s /home/sleepy/Documents/IntelligenceIntegrationSystem/systemd/iis-vectordb.service /etc/systemd/system/
sudo ln -s /home/sleepy/Documents/IntelligenceIntegrationSystem/systemd/iis-web.service      /etc/systemd/system/
sudo ln -s /home/sleepy/Documents/IntelligenceIntegrationSystem/systemd/iis-crawler.service  /etc/systemd/system/
sudo ln -s /home/sleepy/Documents/IntelligenceIntegrationSystem/systemd/iis.target           /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable iis.target
sudo systemctl start iis.target
```

## 常用命令

```bash
# 查看全部
sudo systemctl status iis-vectordb iis-web iis-crawler

# 单独重启某个服务
sudo systemctl restart iis-web

# 日志
sudo journalctl -u iis-web -f

# 查看 watchdog 是否正常工作（应能看到 WATCHDOG=1 通知）
sudo systemctl show iis-web --property=WatchdogTimestamp
```

## systemd watchdog

`iis-web.service` 已启用 `WatchdogSec=30`。IIS 主进程启动后会启动一个守护线程，
每 10 秒向 systemd 发送 `WATCHDOG=1`。若主线程或 GIL 被长时间占住导致喂狗线程无法运行，
systemd 将在 30 秒后自动重启 `iis-web`。

可通过 `_config/config.json` 关闭：

```json
"intelligence_hub": {
  "systemd_watchdog": {
    "enabled": false,
    "interval_sec": 10
  }
}
```

在 Windows 或非 systemd 环境下该功能自动变为 no-op。

## 安全加固：ProtectHome 与 ReadWritePaths

三个单元均启用 `ProtectSystem=full` 与 `ProtectHome=read-only`：

- `ProtectHome=read-only` 使 /home 整体只读：进程可读代码，但不能写 home 下任何路径；
- 程序运行需要写 `_data/`（向量库、SQLite、爬虫治理记录）与 `_log/`（日志），
  因此每个单元用 `ReadWritePaths` 单独放行这两个目录：

  ```
  ReadWritePaths=<项目>/_data <项目>/_log
  ```

**为什么没有使用 `ProtectHome=true`？**
`ProtectHome=true` 会把 /home 挂载为完全不可访问。本部署中 Python 解释器
（`/home/sleepy/anaconda3`）与项目代码都在 /home 下，设为 true 后进程根本无法启动。
`read-only + ReadWritePaths` 是当前目录结构下可行的最强约束。

## 资源限制

- `MemoryMax`：web 2G / vectordb 4G / crawler 3G（参考值，请按机型内存调整；
  vectordb 需常驻 bge-m3 模型与 ChromaDB 索引，crawler 需运行 Playwright 浏览器实例）。
  超过上限后由 systemd OOM 杀死并自动重启。
- `TimeoutStopSec`：web/vectordb 为 30s；crawler 放宽到 60s
  （17 个抓取任务需轮询 stop_event 逐个退出，30s 偏紧容易强杀）。

## VECTOR_ADMIN_TOKEN 配置

VectorDB 的 `/api/admin/*` 管理端点（备份/恢复等）需要 admin token：

1. 生成 token：

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

2. 写入 `systemd/iis.env`：

   ```
   VECTOR_ADMIN_TOKEN=<上一步生成的值>
   ```

3. `sudo systemctl restart iis-vectordb`。

未配置时管理端点保持 fail-closed（503）；在环回绑定（127.0.0.1）前提下，
数据端点（health/search/upsert）不受影响，但启动日志会打印显眼告警。

## 迁移说明

旧的 `iis.service` 与 `launch_iis_system.sh` 进程组管理模式已被替换。
systemd 现在直接管理每个进程，`launch_iis_system.sh` 仅保留供手工调试使用。
