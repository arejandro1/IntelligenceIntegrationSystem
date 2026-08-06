# 架构规则

## 目标数据流

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

## Hub侧唯一权威预筛

- 所有来源数据必须先经过 Hub 侧规则预筛，才能进入 AI 延期队列。
- 预筛规则统一由 Hub 管理，爬虫侧不执行最终判定。
- 预筛结果三类：
  1. 拒绝（含敏感词、参数错误）→ 拒绝审计；
  2. 仅元数据（无需AI）→ 直接元数据归档；
  3. 需AI分析 → 进入 AI 延期队列。
- 预筛规则必须可配置，不得硬编码在业务代码中。

## SourceRegistry

- SourceRegistry 是来源注册表的唯一数据源，维护每个信息源的：
  - 来源标识与名称；
  - 抓取方式（RSS / 列表页 / 其他）；
  - 频道URL与参数；
  - 爬虫任务关联；
  - 启用/停用状态；
  - 重点来源标记（用于Pro复核）。
- 来源注册表通过配置文件或MongoDB集合维护（M2实施），业务代码不得写死来源列表。

## 持久化延期队列

- AI延期队列必须使用MongoDB持久化，保证进程重启后队列不丢失。
- 队列项包含：原始数据引用、优先级、入队时间、重试次数、状态（待处理/处理中/完成/失败）。
- 支持按时间窗口批量取出（供闲时Drainer消费）。
- 队列幂等：同一数据重复入队不得产生重复AI调用。
- 重启恢复：重启后未完成项重新入队或标记待重试。
- 租约机制：处理中项带租约，超时自动释放，避免死锁。
- 并发认领：同一项不得被两个worker同时认领。

## DeepSeek Flash/Pro 分级路由

- 普通内容使用 DeepSeek Flash。
- 重点内容（由预筛规则标记）使用 DeepSeek Pro 复核。
- 分级策略配置化，业务代码不得写死。
- M3实施分级路由，M1—M2仅采集和存储，不产生真实API调用。

## 峰谷时段和 Guard

计费时区：Asia/Shanghai（北京时间）。

高峰时段（2倍价格）：
- [09:00, 12:00)
- [14:00, 18:00)

非高峰时段：
- [00:00, 09:00)
- [12:00, 14:00)
- [18:00, 24:00)

系统策略：
1. 只在非高峰时段调用DeepSeek API。
2. 高峰期间Flash、Pro、API重试和付费健康检查均为0。
3. 高峰期间继续采集、预筛、去重、存储和排队。
4. 第一阶段不允许紧急绕过。
5. 正式生效日期和模型基准单价配置化。
6. 政策生效日期为空时不得进行正式调用。

Guard 默认值：
- OFF_PEAK_START_GUARD_MINUTES=1（非高峰开始后1分钟才允许启动请求）
- OFF_PEAK_END_GUARD_MINUTES=10（非高峰结束前10分钟停止新请求）

实际允许启动请求窗口：
- [00:00, 08:50)
- [12:01, 13:50)
- [18:01, 24:00)

所有时段、Guard、价格、政策版本和生效日期必须配置化，业务代码不得写死。

## 四容器结构

生产部署最终采用四容器（M7实施）：

| 容器 | 职责 |
|------|------|
| china-watch-mongo | MongoDB数据库 |
| china-watch-hub | 情报中心：队列、预筛、调度、Web管理 |
| china-watch-crawler | 采集服务 |
| china-watch-notifier | 推送服务 |

- Docker化位于M7，不得提前到M0或M1。
- MongoDB容器不得公开映射端口到公网。

## VectorDB关闭原则

- 第一阶段关闭但不删除VectorDB、本地Embedding、BGE-M3、ChromaDB和FAISS。
- VectorDB唯一配置来源为 `intelligence_hub.vectordb.enabled`。
- 不得新增第二个VectorDB开关。
- 关闭状态下相关启动逻辑不执行，相关代码保留不删除。

## 新旧AI消费者互斥

- 旧AI Worker（LEGACY）和 OffPeakQueueDrainer（新）不得同时启用。
- 两者同时为true必须启动失败。
- 两者同时为false是合法的只采集模式。
- M1和M2默认状态：
  - API_ENABLED=false
  - API_DRY_RUN=true
  - USE_LEGACY_AI_WORKER=false
  - USE_OFF_PEAK_DRAINER=false
- M3完成全部测试并经人工启用后：
  - API_ENABLED=true
  - API_DRY_RUN=false
  - USE_LEGACY_AI_WORKER=false
  - USE_OFF_PEAK_DRAINER=true
- 不得在M1—M2中默认启用旧AI Worker。