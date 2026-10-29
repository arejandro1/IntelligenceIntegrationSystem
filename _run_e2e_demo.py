# -*- coding: utf-8 -*-
"""
端到端情报研判演示（方案4：本机临时跑全流程）
流程：抓取 RSS（代理/直连）→ RulePreFilter 预筛 → DeepSeek 真实研判 → 打印结果
真实 API Key 从群晖 .env 运行时读取，不落盘、不打印。
"""
import base64
import os
import sys
import time
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 结果同时输出到终端和 UTF-8 文件（避免 Windows GBK 乱码） ----
_RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_情报研判结果.txt")
_ORIG_STDOUT = sys.stdout


class _Tee:
    def __init__(self):
        self._f = open(_RESULT_FILE, "w", encoding="utf-8")

    def write(self, s):
        self._f.write(s)
        try:
            _ORIG_STDOUT.write(s)
            _ORIG_STDOUT.flush()
        except Exception:
            # 演示脚本的双写输出：控制台侧失败不影响结果文件写入（有意忽略）
            pass

    def flush(self):
        self._f.flush()
        try:
            _ORIG_STDOUT.flush()
        except Exception:
            # 演示脚本的双写输出：控制台侧失败不影响结果文件写入（有意忽略）
            pass


sys.stdout = _Tee()
print(f"结果将写入 {_RESULT_FILE}（UTF-8）")

# ---------------- 0. 从群晖 .env 读取 DeepSeek 配置（不打印 key） ----------------
HOST = "192.168.2.188"
USER = "z"
KEY = r"C:\Users\Administrator\.ssh\synology_deploy_key"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, key_filename=KEY, timeout=20)
_, stdout, _ = ssh.exec_command(
    "grep -E '^DEEPSEEK_(API_KEY|FLASH_MODEL|PRO_MODEL)=' /volume1/docker/china-watch/.env",
    timeout=15,
)
env_lines = stdout.read().decode("utf-8", "replace").strip().splitlines()
ssh.close()

env = {}
for line in env_lines:
    if "=" in line:
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")

DEEPSEEK_API_KEY = env.get("DEEPSEEK_API_KEY", "")
FLASH_MODEL = env.get("DEEPSEEK_FLASH_MODEL", "deepseek-chat")
PRO_MODEL = env.get("DEEPSEEK_PRO_MODEL", "deepseek-reasoner")

if not DEEPSEEK_API_KEY:
    print("错误：群晖 .env 未读取到 DEEPSEEK_API_KEY")
    sys.exit(1)
print(f"[OK] 已读取 DeepSeek 配置 FLASH={FLASH_MODEL} PRO={PRO_MODEL}（Key 已隐藏）")

# ---------------- 1. 抓取 RSS ----------------
# 代理：本机 Clash (国内可直连的源不用代理)
CLASH = "http://127.0.0.1:7897"
NO_PROXY = "http://127.0.0.1:8888"  # 占位，实际直连走 urllib 默认

SOURCES = [
    # (source_id, 显示名, url, 优先级, 是否走代理)
    ("voa", "VOA中文", "https://www.voachinese.com/api/zhengce", True, True),
    ("bbc", "BBC Asia-China", "https://feeds.bbci.co.uk/news/world/asia/china/rss.xml", True, True),
    ("dw", "DW涉华", "https://rss.dw.com/rdf/rss-en-china", False, True),
    ("aljazeera", "AlJazeera", "https://www.aljazeera.com/xml/rss/all.xml", False, True),
    ("abc_au", "ABC Australia", "https://www.abc.net.au/news/feed/51120/rss.xml", False, True),
    ("elpais", "ElPais", "https://feeds.elpais.com/mrss-s/pages/ep/site/english.elpais.com/portada", False, True),
    ("france24", "France24 China", "https://www.france24.com/en/tag/china/rss", False, False),
    ("chinanews", "CNS", "https://www.ecns.cn/rss/rss.xml", True, False),
    ("news_cn", "中新网", "http://www.chinanews.com.cn/rss/scroll-news.xml", True, False),
    ("aa", "Anadolu", "https://www.aa.com.tr/en/rss/default?cat=live", False, False),
]

MAX_PER_SOURCE = 5  # 每个来源最多取 5 条，控制 token


def fetch_rss(url: str, use_proxy: bool, timeout: int = 10):
    """抓取 RSS 并解析，返回 [{title, link, desc}]"""
    try:
        import urllib.request
        if use_proxy:
            # 手工走 Clash HTTP 代理（urllib 默认不用环境代理，这里显式设置）
            proxy_handler = urllib.request.ProxyHandler({"http": CLASH, "https": CLASH})
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        items = []
        # 兼容 RSS 2.0 / Atom
        for item in root.iter():
            if item.tag in ("item", "entry"):
                title = ""
                link = ""
                desc = ""
                for child in item:
                    tag = child.tag.split("}")[-1].lower()
                    if tag == "title":
                        title = (child.text or "").strip()
                    elif tag == "link":
                        if child.get("href"):
                            link = child.get("href")
                        else:
                            link = (child.text or "").strip()
                    elif tag in ("description", "summary"):
                        desc = (child.text or "").strip()
                if title:
                    items.append({"title": title, "link": link, "desc": desc[:600]})
        return items
    except Exception as e:
        return [{"title": f"<抓取失败: {type(e).__name__}>", "link": "", "desc": str(e)[:200]}]


def strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


articles = []
for source_id, display_name, url, is_priority, use_proxy in SOURCES:
    items = fetch_rss(url, use_proxy)
    count = 0
    for it in items:
        if it["title"].startswith("<抓取失败"):
            print(f"[抓取失败] {display_name}: {it['desc'][:80]}")
            continue
        title = strip_html(it["title"])
        desc = strip_html(it["desc"])
        if len(title) < 8:
            continue
        articles.append({
            "source": source_id,
            "source_name": display_name,
            "title": title,
            "link": it["link"],
            "desc": desc[:500],
            "priority": is_priority,
        })
        count += 1
        if count >= MAX_PER_SOURCE:
            break
    print(f"[抓取] {display_name}: {count} 条")

print(f"\n共抓取 {len(articles)} 条有效条目\n")

# ---------------- 2. RulePreFilter 规则预筛 ----------------
from ServiceComponent.RulePreFilter import RulePreFilter
from ServiceComponent.SourceRegistry import SourceRegistry

# 用真实来源注册表 + 预筛规则
registry = SourceRegistry(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sources', 'china_watch_sources.yaml'))
RULES = {
    'reject_patterns': ['赌博', 'gambling', 'casino', '色情', 'porn'],
    'metadata_only_patterns': [],
    'min_content_length': 10,
    'require_title': True,
}
prefilter = RulePreFilter(registry, RULES)
passed = []
for a in articles:
    # 构造 raw_data 进行三级预筛
    raw = {
        'UUID': 'e2e-' + str(abs(hash(a['title']))) + '-' + str(len(passed)),
        'source': a['source'],
        'title': a['title'],
        'content': a['title'] + ' ' + a['desc'],
    }
    result = prefilter.screen(raw)
    if result.decision == 'RULE_REJECTED':
        print(f"[预筛拒绝] {a['source']}  {a['title'][:40]}  ({result.reason})")
        continue
    passed.append(a)

print(f"\n预筛后剩余 {len(passed)} 条进入 AI 研判\n")

# ---------------- 3. DeepSeek 真实研判 ----------------
from ServiceComponent.DeepSeekClient import DeepSeekClient

client = DeepSeekClient(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1", timeout=90)

PROMPT_TEMPLATE = """你是美国涉华公开信息监测系统的高级情报分析师。请对以下报道做结构化研判。

【报道来源】{source}
【标题】{title}
【摘要】{desc}

请用简体中文输出：
1. 一句话核心情报
2. 涉华态度倾向（正面/中性/负面/对立）及其依据
3. 涉及的关键实体（人物/机构/国家）
4. 对美国涉华政策的潜在含义
5. 情报价值评级（高/中/低）
请保持简洁，直接输出编号列表。"""

results = []
for i, a in enumerate(passed):
    model = PRO_MODEL if a["priority"] else FLASH_MODEL
    prompt = PROMPT_TEMPLATE.format(source=a["source"], title=a["title"], desc=a["desc"])
    print(f"[AI研判 {i+1}/{len(passed)}] model={model} {a['source']} | {a['title'][:40]}")
    try:
        resp = client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.3,
        )
        content = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage", {})
        results.append({
            "source": a["source"],
            "title": a["title"],
            "link": a["link"],
            "model": model,
            "analysis": content,
            "tokens": usage.get("total_tokens", 0),
        })
        print(f"    → 完成, tokens={usage.get('total_tokens', 0)}")
    except Exception as e:
        print(f"    → 失败: {type(e).__name__}: {str(e)[:150]}")
        results.append({
            "source": a["source"], "title": a["title"], "link": a["link"],
            "model": model, "analysis": f"<研判失败: {str(e)[:200]}>", "tokens": 0,
        })
    time.sleep(1)  # 避免瞬时并发过大

# ---------------- 4. 输出全部情报 ----------------
print("\n" + "=" * 70)
print("情报研判结果汇总")
print("=" * 70)
total_tokens = 0
for r in results:
    total_tokens += r["tokens"]
    print(f"\n【{r['source']}】{r['title']}")
    if r["link"]:
        print(f"链接: {r['link']}")
    print(f"模型: {r['model']} | tokens: {r['tokens']}")
    print(r["analysis"])
    print("-" * 70)

print(f"\n总计研判 {len(results)} 条，消耗 tokens 约 {total_tokens}")
print("完成。")