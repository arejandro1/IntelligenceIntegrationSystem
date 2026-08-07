# Synology DS920+ 部署指南（M7）

> 状态：待实施（文档已就绪，部署执行需在群晖上进行）
> 硬件：Synology DS920+ / Intel J4125 / 20GB RAM / 无 GPU

---

## 1. 部署前检查

| 检查项 | 要求 |
|--------|------|
| DSM 版本 | 7.2 或更高（内置 Container Manager） |
| Docker/Container Manager | 已安装 |
| 磁盘空间 | ≥100GB（建议 200GB） |
| SSH | 建议开启（外网访问建议配合反向代理） |

## 2. 四容器结构

| 容器 | 镜像 | 端口 | 职责 |
|------|------|------|------|
| china-watch-mongo | mongo:7 | 仅内部 27017 | MongoDB 数据库 |
| china-watch-hub | Dockerfile.hub | 127.0.0.1:5000→5000 | 情报中心（队列/预筛/调度/Web） |
| china-watch-crawler | Dockerfile.crawler | — | 采集服务 |
| china-watch-notifier | Dockerfile.notifier | — | 推送服务 |

安全要求：
- MongoDB 端口**不映射到公网**
- 管理界面仅内网访问或通过反向代理保护
- 所有容器使用非 root 用户运行（user: "1000:1000"）

## 3. 部署步骤

```bash
# 1) SSH 登录群晖
ssh admin@192.168.x.x

# 2) 创建项目目录
mkdir -p /volume1/docker/china-watch
cd /volume1/docker/china-watch

# 3) 传输项目（在本地执行）
# scp -r ./* admin@192.168.x.x:/volume1/docker/china-watch/

# 4) 创建 .env
cp .env.example .env
nano .env

# 5) 启动容器
sudo docker compose up -d

# 6) 验证
sudo docker compose ps
sudo docker compose logs -f china-watch-hub
```

## 4. 反向代理配置（可选）

推荐使用群晖内建反向代理（DSM → 控制面板 → 登录门户 → 高级 → 反向代理），将域名 HTTPS 443 → http://127.0.0.1:5000，加上 SSL 证书。

## 5. 磁盘空间与日志轮转

- 预留磁盘：最低 100GB，建议 200GB
- 磁盘告警：
  - 70% 告警，通知运维
  - 85% 停止低优先级推送
  - 95% 停止采集与队列消费
- 日志轮转：容器日志由 Docker 自动轮转，`_log/` 将挂载到宿主机

## 6. MongoDB 备份

```bash
docker exec china-watch-mongo mongodump --archive=/backup/mongo-$(date +%F).gz --gzip
```

## 7. 验证命令

```bash
# 容器健康状态
sudo docker compose ps

# Hub 日志确认
sudo docker compose logs china-watch-hub | grep "Service PID"

# Web 访问
curl http://127.0.0.1:5000/