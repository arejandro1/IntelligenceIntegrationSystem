# -*- coding: utf-8 -*-
"""生成《关于IntelligenceIntegrationSystem项目情况的简要说明》（党政机关公文格式）。

说明：
1. 本脚本为独立的文档生成工具，不属于业务代码，不依赖项目内其他模块；
2. 版式参照 GB/T 9704-2012《党政机关公文格式》常用要求：A4纸，上37mm、下35mm、
   左28mm、右26mm页边距；正文三号仿宋、固定行距28磅；标题二号宋体居中；
   一级标题三号黑体，二级标题三号楷体；页码四号宋体“— 1 —”（单页右、双页左）；
3. 字体使用 Windows 中文环境通用的宋体、黑体、楷体、仿宋；
4. 运行方式：python Scripts/GenerateProjectIntroDocx.py，输出 doc/项目简要说明.docx。
"""

import os

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "doc", "项目简要说明.docx")

ORG_NAME = "IntelligenceIntegrationSystem项目组"
DOC_NUMBER = "IIS组〔2026〕1号"
DOC_TITLE = "关于IntelligenceIntegrationSystem项目情况的简要说明"
DOC_DATE_CN = "2026年9月17日"

SONG, HEI, KAI, FANG = "宋体", "黑体", "楷体", "仿宋"
ASCII_FONT = "Times New Roman"
RED = RGBColor(0xFF, 0x00, 0x00)

CENTER = WD_ALIGN_PARAGRAPH.CENTER
LEFT = WD_ALIGN_PARAGRAPH.LEFT
RIGHT = WD_ALIGN_PARAGRAPH.RIGHT


def set_run_font(run, east, size, bold=False, color=None):
    """统一设置字符格式：西文Times New Roman，中文按east指定，字号为磅值。"""
    run.font.name = ASCII_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rPr = run._element.get_or_add_rPr()
    rPr.get_or_add_rFonts().set(qn("w:eastAsia"), east)


def add_paragraph(doc, runs, align=None, indent=True, size=16, exact=28,
                  before=0, after=0, right_indent_pt=None, color=None):
    """新增段落。runs为(文本, 中文字体, 加粗)元组列表；exact为固定行距磅值。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    if align is not None:
        p.alignment = align
    if exact is not None:
        pf.line_spacing = Pt(exact)
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    ind = p._p.get_or_add_pPr().get_or_add_ind()
    if indent:
        ind.set(qn("w:firstLineChars"), "200")
        ind.set(qn("w:firstLine"), "640")
    if right_indent_pt is not None:
        ind.set(qn("w:right"), str(int(right_indent_pt * 20)))
    for text, east, bold in runs:
        set_run_font(p.add_run(text), east, size, bold=bold, color=color)
    return p


def body(doc, text, **kwargs):
    """正文段落：三号仿宋。"""
    return add_paragraph(doc, [(text, FANG, False)], **kwargs)


def h1(doc, text):
    """一级标题：一、……（三号黑体）。"""
    return add_paragraph(doc, [(text, HEI, False)])


def h2(doc, lead, rest=""):
    """二级标题：（一）……（三号楷体引导），可接排正文（三号仿宋）。"""
    runs = [(lead, KAI, False)]
    if rest:
        runs.append((rest, FANG, False))
    return add_paragraph(doc, runs)


def h3(doc, lead, rest):
    """三级条目：1.……（三号仿宋加粗引导）接排正文。"""
    return add_paragraph(doc, [(lead, FANG, True), (rest, FANG, False)])


def add_rule(doc, color="FF0000", sz=12, side="bottom", before=0, after=0):
    """添加水平分隔线段落（版头红线或版记细线）。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = Pt(6)
    pPr = p._p.get_or_add_pPr()
    pBdr = pPr.find(qn("w:pBdr"))
    if pBdr is None:
        pBdr = OxmlElement("w:pBdr")
        anchor = None
        for tag in ("w:shd", "w:tabs", "w:spacing", "w:ind", "w:jc", "w:rPr"):
            anchor = pPr.find(qn(tag))
            if anchor is not None:
                break
        if anchor is not None:
            anchor.addprevious(pBdr)
        else:
            pPr.append(pBdr)
    el = OxmlElement("w:" + side)
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(sz))
    el.set(qn("w:space"), "1")
    el.set(qn("w:color"), color)
    pBdr.append(el)
    return p


def add_page_number(p, size=14):
    """在页脚段落中插入“— 1 —”式页码域（四号宋体）。"""
    set_run_font(p.add_run("— "), SONG, size)
    r = p.add_run()
    set_run_font(r, SONG, size)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    r._r.append(begin)
    r._r.append(instr)
    r._r.append(end)
    set_run_font(p.add_run(" —"), SONG, size)


def _enable_even_odd_headers(doc):
    """在settings中启用奇偶页页脚，以便页码单页居右、双页居左。"""
    settings = doc.settings.element
    if settings.find(qn("w:evenAndOddHeaders")) is not None:
        return
    el = OxmlElement("w:evenAndOddHeaders")
    anchor = None
    for tag in ("w:characterSpacingControl", "w:compat", "w:rsids",
                "m:mathPr", "w:themeFontLang"):
        anchor = settings.find(qn(tag))
        if anchor is not None:
            break
    if anchor is not None:
        anchor.addprevious(el)
    else:
        settings.append(el)


def setup_document(doc):
    """页面设置（A4、公文页边距）、正文默认样式与页码。"""
    st = doc.styles["Normal"]
    st.font.name = ASCII_FONT
    st.font.size = Pt(16)
    st.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), FANG)
    sec = doc.sections[0]
    sec.page_width = Mm(210)
    sec.page_height = Mm(297)
    sec.top_margin = Mm(37)
    sec.bottom_margin = Mm(35)
    sec.left_margin = Mm(28)
    sec.right_margin = Mm(26)
    _enable_even_odd_headers(doc)
    odd = sec.footer
    odd.is_linked_to_previous = False
    odd.paragraphs[0].alignment = RIGHT
    add_page_number(odd.paragraphs[0])
    even = sec.even_page_footer
    even.is_linked_to_previous = False
    even.paragraphs[0].alignment = LEFT
    add_page_number(even.paragraphs[0])


def set_cell(cell, text, east, center=False, size=12):
    """填充表格单元格：默认小四号字，垂直居中。"""
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    p = cell.paragraphs[0]
    p.alignment = CENTER if center else LEFT
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    set_run_font(p.add_run(text), east, size)


def add_table(doc, headers, rows, widths_mm, grid=True):
    """插入表格；grid=True时使用全框线（公文表格常用式样）。"""
    t = doc.add_table(rows=1, cols=len(headers))
    if grid:
        t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, text in enumerate(headers):
        set_cell(t.rows[0].cells[i], text, HEI, center=True)
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            set_cell(cells[i], str(val), FANG, center=(i == 0))
    for i, width in enumerate(widths_mm):
        for r in t.rows:
            r.cells[i].width = Mm(width)
    return t


def table_caption(doc, text):
    """表题：小四号黑体居中。"""
    add_paragraph(doc, [(text, HEI, False)], align=CENTER, indent=False,
                  size=12, exact=20, before=6, after=4)

TECH_TABLE = [
    ("语言环境", "Python 3.10及以上"),
    ("Web框架", "Flask（主服务）、FastAPI（子服务）"),
    ("WSGI服务", "Waitress（Windows默认）、Gunicorn（Linux可选）"),
    ("数据库", "MongoDB（情报文档）、SQLite（用户认证与状态日志）"),
    ("向量数据库", "ChromaDB，向量模型BAAI/bge-m3"),
    ("数据采集", "Playwright、requests、BeautifulSoup4、crawl4ai、feedparser"),
    ("AI客户端", "OpenAI兼容API，支持多厂商、多账号轮询与故障切换"),
    ("数据校验", "Pydantic v2"),
    ("任务调度", "APScheduler与自定义AdvancedScheduler"),
    ("前端呈现", "Jinja2服务端渲染模板"),
]

CONCURRENCY_TABLE = [
    ("普通爬虫", "2"),
    ("Playwright浏览器采集", "1"),
    ("PDF处理", "1"),
    ("AI分析Worker", "1"),
]

MILESTONE_TABLE = [
    ("M0", "开发护栏与架构文档（当前阶段）", "否"),
    ("M1", "轻量运行配置、依赖分层和功能开关", "否"),
    ("M2", "数据结构、SourceRegistry和规则预筛", "否"),
    ("M3", "DeepSeek分级路由、峰谷调度、持久化队列、费用台账和预算", "否（测试通过后人工启用）"),
    ("M4", "首批8个来源", "待核验"),
    ("M5", "PDF处理", "待核验"),
    ("M6", "推送服务", "待核验"),
    ("M7", "Docker Compose和群晖部署", "待核验"),
    ("M8", "连续运行、资源和安全测试", "待核验"),
    ("M9", "扩展全部来源", "待核验"),
]


def build_document():
    doc = Document()
    setup_document(doc)

    # ---------- 版头：发文机关标志、发文字号、红色分隔线 ----------
    add_paragraph(doc, [(ORG_NAME, SONG, True)], align=CENTER, indent=False,
                  size=28, exact=40, after=10, color=RED)
    add_paragraph(doc, [(DOC_NUMBER, FANG, False)], align=CENTER,
                  indent=False, after=2)
    add_rule(doc, color="FF0000", sz=12, after=20)

    # ---------- 标题与正文 ----------
    add_paragraph(doc, [(DOC_TITLE, SONG, True)], align=CENTER, indent=False,
                  size=22, exact=33, after=16)
    body(doc, "现将IntelligenceIntegrationSystem（以下简称IIS）项目基本情况"
              "及发展规划说明如下。")

    h1(doc, "一、项目概况")
    h2(doc, "（一）项目定位。", "IIS是一套开源情报（OSINT）整合系统，通过抓取"
           "全球主流新闻网站的公开信息，运用人工智能（AI）进行结构化分析、评分与"
           "去重，将高价值情报归档入库，并通过网页端提供检索、聚合与态势推演服务。"
           "项目源代码托管于GitHub平台。")
    h2(doc, "（二）建设背景。", "当前新闻信息数量庞大、质量参差，大量内容缺乏"
           "情报价值，人工甄别成本较高。本项目旨在运用自动化采集与AI分析手段，对"
           "公开信息进行系统性价值筛选和整合加工，提高情报获取效率。")
    h2(doc, "（三）版本沿革。", "自2026年2月15日起，项目主分支正式切换至v2"
           "版本，v1版本另行归档保留；v2版本兼容v1版本数据，无需进行数据库升级。")

    h1(doc, "二、现有系统主要功能与技术架构")
    h2(doc, "（一）核心工作流程。", "系统按照“抓取新闻情报、提交情报中心、AI"
           "分析评分清洗、归档、发布查询”的主线运行，形成从信息采集到情报服务的"
           "完整闭环。")
    h2(doc, "（二）系统组成。", "系统由三个进程构成：一是核心Web服务"
           "（IntelligenceHubLauncher.py），承担队列管理、AI分析、情报归档与网页"
           "管理职能，默认端口5000；二是爬虫服务（CrawlerServiceEngine.py），承担"
           "抓取任务插件化加载、热重载与爬虫治理职能，治理后台默认端口8002；三是"
           "向量数据库服务（VectorDB.VectorDBBService），提供语义检索与情报聚类"
           "能力，仅监听本机8001端口，属可选组件。")
    h2(doc, "（三）主要技术栈。", "现有系统主要技术选型见表1。")
    table_caption(doc, "表1  主要技术栈")
    add_table(doc, ("层级", "技术选型"), TECH_TABLE, (30, 126))
    h2(doc, "（四）v2版本主要特性。", "一是采用PMES-T领域分类体系，涵盖政治与"
           "安全、经济与金融、科技与网络、社会与环境四大支柱，每条情报指定唯一主"
           "分类并可附加不超过5个跨域辅助标签；二是实行三层评分模型，按置信度30%、"
           "战略价值50%、行动紧迫性20%的权重计算总分（满分10分），置信度低于4分的"
           "情报先行标注核验；三是优化向量数据库存储格式，支持相似情报跳转与关联"
           "情报推演；四是爬虫框架支持“所见即所得”式配置生成，并具备监控调度与"
           "治理后台能力。")
    h2(doc, "（五）信息来源覆盖情况。", "现已接入阿纳多卢通讯社、澳大利亚广播"
           "公司、半岛电视台、英国广播公司、加拿大广播公司、中国新闻网、德国之声、"
           "西班牙《国家报》、英为财情、新华网、日本广播协会、土耳其NTV、法国国际"
           "广播电台、塔斯社、美国之音等15家媒体，覆盖亚洲、欧洲、美洲、大洋洲"
           "主要国家和地区。")
    h2(doc, "（六）部署要求。", "部署需Python 3.10及以上版本、MongoDB数据库及"
           "bge-m3向量模型；配置文件统一存放于_config目录，运行数据与日志分别存放"
           "于_data、_log目录，程序目录与数据目录分离，便于备份与容器化挂载。")


    h1(doc, "三、改造方向：建设美国涉华公开信息监测系统")
    h2(doc, "（一）改造目标。", "在现有IIS基础上，将系统改造为聚焦美国涉华"
           "公开信息的监测系统，强化来源注册管理、规则预筛、AI分级分析与费用控制"
           "能力。")
    h2(doc, "（二）生产环境约束。", "生产环境部署于Synology DS920+主机"
           "（Intel J4125四核处理器、20GB内存、无独立显卡），不运行本地大模型，"
           "AI推理统一通过云端DeepSeek API完成。")
    h2(doc, "（三）目标数据流。", "改造后系统按照“采集、缓存、预筛、排队、调度、"
           "分析、记账、归档推送”的流程运行：爬虫以并发2完成采集并写入MongoDB原始"
           "缓存；情报中心侧统一执行规则预筛，预筛结果分为拒绝审计、仅元数据归档、"
           "需AI分析三类；需分析数据进入MongoDB持久化延期队列，实行幂等入队与租约"
           "管理；非高峰时段由闲时调度器批量取出，普通内容调用DeepSeek Flash模型"
           "分析，重点内容经DeepSeek Pro模型复核；全部调用计入费用台账并受预算"
           "约束，分析结果归档后按需推送。")
    h2(doc, "（四）关键运行策略。")
    h3(doc, "1.峰谷调度策略。", "以北京时间计费，高峰时段（9:00至12:00、14:00至"
           "18:00，2倍价格）不发起任何付费调用；仅在非高峰时段调用，并在时段起止"
           "设置保护性缓冲；峰谷时段、价格与政策生效日期均实行配置化管理。")
    h3(doc, "2.分级路由与预算策略。", "普通内容与重点内容分别路由至不同档位模型，"
           "重点来源实行标记管理，建立费用台账与预算限额机制。")
    h3(doc, "3.并发与资源控制。", "各组件并发上限见表2。")
    table_caption(doc, "表2  组件并发上限")
    add_table(doc, ("组件", "并发上限"), CONCURRENCY_TABLE, (78, 78))
    h3(doc, "4.部署架构。", "生产部署采用四容器结构，即china-watch-mongo"
           "（数据库）、china-watch-hub（情报中心）、china-watch-crawler（采集"
           "服务）、china-watch-notifier（推送服务）；容器以非root用户运行，"
           "MongoDB端口不对公网暴露，管理界面仅限内网或反向代理访问。")
    h2(doc, "（五）第一阶段边界。", "VectorDB、本地嵌入（BGE-M3）、ChromaDB、"
           "FAISS等组件关闭但不删除，唯一配置开关为intelligence_hub.vectordb."
           "enabled；旧AI Worker与新的闲时调度消费者实行互斥启用；改造初期API调用"
           "默认关闭并处于模拟运行（DRY_RUN）状态，经测试验证后由人工启用。")

    h1(doc, "四、实施计划")
    h2(doc, "（一）总体安排。", "改造工作按M0至M9十个里程碑循序推进，一次只实施"
           "一个经批准的里程碑，各阶段任务边界及真实API调用许可见表3。")
    table_caption(doc, "表3  里程碑计划")
    add_table(doc, ("里程碑", "主要任务", "是否允许真实API调用"),
              MILESTONE_TABLE, (18, 96, 42))
    h2(doc, "（二）当前进展。", "改造工作已按里程碑推进至M9阶段，分级路由、峰谷调度、"
           "持久化队列、费用台账、推送服务、容器编排与安全测试等任务的代码"
           "和配套单元测试均已提交并通过离线验证（模拟环境，未调用真实付费"
           "接口）。群晖实机部署、真实API调用启用、真实推送渠道连通和长期"
           "连续运行验证尚待在目标环境完成验收。")

    h1(doc, "五、有关说明")
    h2(doc, "（一）安全管理。", "密钥、令牌等敏感信息不进入源码与日志，统一通过"
           "环境变量或本地配置注入；对外网络请求执行SSRF防护并设置超时与有限重试；"
           "日志落实脱敏与轮转要求。")
    h2(doc, "（二）文档索引。", "项目详细资料可通过README.md、doc/"
           "IntelligenceDesign.md、doc/IntelligenceDesign_v2.md、doc/"
           "iis_v2_concept.md等文件查阅。")
    h2(doc, "（三）适用范围。", "本说明材料仅供项目内部参阅，具体实施以各里程碑"
           "批准文件为准。")

    # ---------- 落款与版记 ----------
    add_paragraph(doc, [], indent=False)
    add_paragraph(doc, [(ORG_NAME, FANG, False)], align=RIGHT, indent=False,
                  right_indent_pt=108)
    add_paragraph(doc, [(DOC_DATE_CN, FANG, False)], align=RIGHT, indent=False,
                  right_indent_pt=32)
    add_rule(doc, color="000000", sz=6, before=14)
    stamp = doc.add_table(rows=1, cols=2)
    stamp.alignment = WD_TABLE_ALIGNMENT.CENTER
    stamp.autofit = False
    set_cell(stamp.rows[0].cells[0], ORG_NAME, FANG, size=14)
    set_cell(stamp.rows[0].cells[1], DOC_DATE_CN + "印发", FANG, size=14)
    stamp.rows[0].cells[1].paragraphs[0].alignment = RIGHT
    stamp.rows[0].cells[0].width = Mm(100)
    stamp.rows[0].cells[1].width = Mm(56)
    add_rule(doc, color="000000", sz=6)
    return doc


def main():
    doc = build_document()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    doc.save(OUTPUT_PATH)
    # 自校验：重新打开文档并统计结构
    check = Document(OUTPUT_PATH)
    texts = [p.text for p in check.paragraphs if p.text.strip()]
    print("输出文件：%s" % OUTPUT_PATH)
    print("文件大小：%d 字节" % os.path.getsize(OUTPUT_PATH))
    print("非空段落数：%d，表格数：%d" % (len(texts), len(check.tables)))
    for idx, t in enumerate(check.tables, 1):
        print("  表%d：%d行 × %d列" % (idx, len(t.rows), len(t.columns)))
    print("文档标题：%s" % texts[2])
    print("正文首段：%s" % texts[3])


if __name__ == "__main__":
    main()
