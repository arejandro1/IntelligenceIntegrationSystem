# 情报整合系统 - Intelligence Integration System (IIS)

## 链接

[24小时情报聚合](/intelligences/clusters)

[完整情报列表](/intelligences?offset=0&count=20&threshold=6)

[实体趋势](/statistics/entity_frequency/page)

[情报搜索](/intelligences/search)

## 说明

该系统用以收集国内外主流媒体的公开信息，通过AI进行分类、评分、翻译，旨在筛除无价值信息，高效整合全球公开情报。

本系统属于公开来源情报 (Open-source intelligence，OSINT) 的一个实践，当前通过RSS采集新闻以避免潜在的法律问题。

本项目为开源项目，项目地址：[Intelligence Integration System](https://github.com/SleepySoft/IntelligenceIntegrationSystem/tree/dev)

系统当前为测试状态，不能保证————但会尽量做到————7 x 24小时在线。

请勿尝试抓取本网站数据以免增加系统负担，因为我会定时导出数据并供直接下载，你也可以拉取代码并自行部署本系统，故没有抓取的必要。

## 声明

所有情报均来源于媒体发布信息，不代表本人立场。据我观察，某些国外媒体（特别是德国之声，dw）的新闻较为反华，请仔细鉴别。

注意：情报分类中的“本国”和“国内”并不特指中国。为了保证情报分析的通用性，所以并没有加入特定国别的判定。

## 数据下载

+ [自动备份与上传](https://pan.baidu.com/s/1Fpf32ZJAVITglTAqKkH1GQ?pwd=yucs)

+ [不定期手工导出](https://pan.baidu.com/s/122mewzpNkd6A8UjMDpIMsg?pwd=tfx7)

数据可通过MongoDB的mongoimport工具导入：

```
mongoimport --uri=mongodb://localhost:27017 --db=IntelligenceIntegrationSystem --collection=intelligence_cached --file=intelligence_cached.json
mongoimport --uri=mongodb://localhost:27017 --db=IntelligenceIntegrationSystem --collection=intelligence_archived --file=intelligence_archived.json
mongoimport --uri=mongodb://localhost:27017 --db=IntelligenceIntegrationSystem --collection=intelligence_low_value --file=intelligence_low_value.json
```

---

<div align="center">
<a href="https://beian.miit.gov.cn/" target="_blank">京ICP备2026047887号</a><br/>
<a href="https://beian.mps.gov.cn/#/query/webSearch?code=11010102007849" rel="noreferrer" target="_blank"><img src="/static/beian.png" style="vertical-align: middle; height: 20px; width: 20px;" />京公网安备11010102007849号</a>
</div>