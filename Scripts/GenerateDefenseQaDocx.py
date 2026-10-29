# -*- coding: utf-8 -*-
"""把 docs/defense_qa.md 排版为正式答辩备答 Word 文档。

版式按党政机关公文常用要求处理：A4 纸，上 37 毫米、下 35 毫米、左 28 毫米、
右 26 毫米；正文三号仿宋、固定 28 磅行距；标题二号小标宋（系统无该字体时由
Word 自动回退）；一级标题三号黑体、二级标题三号楷体；页脚插入页码域。

运行：
    py Scripts/GenerateDefenseQaDocx.py
"""

import os
import re

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_PATH = os.path.join(PROJECT_ROOT, "docs", "defense_qa.md")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "docs", "美国涉华公开信息监测系统答辩备答材料.docx")

TITLE_FONT = "方正小标宋简体"
SONG = "宋体"
HEI = "黑体"
KAI = "楷体"
FANG = "仿宋"
ASCII_FONT = "Times New Roman"
CODE_FONT = "Consolas"

CENTER = WD_ALIGN_PARAGRAPH.CENTER
LEFT = WD_ALIGN_PARAGRAPH.LEFT
RIGHT = WD_ALIGN_PARAGRAPH.RIGHT


def set_run_font(run, east, size, bold=False, color=None, ascii_font=ASCII_FONT):
    """统一设置中文字体、西文字体、字号和加粗状态。"""
    run.font.name = ascii_font
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.get_or_add_rFonts()
    r_fonts.set(qn("w:ascii"), ascii_font)
    r_fonts.set(qn("w:hAnsi"), ascii_font)
    r_fonts.set(qn("w:eastAsia"), east)


def set_paragraph_indent(paragraph, first_line=True, left_mm=None, hanging_pt=None):
    """设置公文正文的首行缩进或列表缩进。"""
    fmt = paragraph.paragraph_format
    if left_mm is not None:
        fmt.left_indent = Mm(left_mm)
    if hanging_pt is not None:
        fmt.first_line_indent = Pt(-hanging_pt)
    elif first_line:
        fmt.first_line_indent = Pt(32)
    else:
        fmt.first_line_indent = Pt(0)


def set_paragraph_format(
    paragraph,
    *,
    size=16,
    exact=28,
    align=LEFT,
    first_line=True,
    left_mm=None,
    hanging_pt=None,
    before=0,
    after=0,
):
    fmt = paragraph.paragraph_format
    fmt.line_spacing = Pt(exact) if exact is not None else None
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    paragraph.alignment = align
    set_paragraph_indent(
        paragraph,
        first_line=first_line,
        left_mm=left_mm,
        hanging_pt=hanging_pt,
    )
    return paragraph


def add_hyperlink(paragraph, text, url, *, size=16, bold=False, east=FANG):
    """向段落插入可点击的 Word 超链接。"""
    part = paragraph.part
    relation_id = part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relation_id)

    run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    r_fonts = OxmlElement("w:rFonts")
    r_fonts.set(qn("w:ascii"), ASCII_FONT)
    r_fonts.set(qn("w:hAnsi"), ASCII_FONT)
    r_fonts.set(qn("w:eastAsia"), east)
    r_pr.append(r_fonts)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size * 2)))
    r_pr.append(sz)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    r_pr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    r_pr.append(underline)
    if bold:
        r_pr.append(OxmlElement("w:b"))
    run.append(r_pr)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


INLINE_TOKEN = re.compile(r"(\*\*.*?\*\*|`[^`]+`|\[[^\]]+\]\([^\)]+\))")
LINK_TOKEN = re.compile(r"^\[([^\]]+)\]\(([^\)]+)\)$")


def add_inline(paragraph, text, *, size=16, east=FANG, bold=False):
    """解析少量 Markdown 行内标记，保留粗体、代码和超链接。"""
    pos = 0
    for match in INLINE_TOKEN.finditer(text):
        if match.start() > pos:
            run = paragraph.add_run(text[pos : match.start()])
            set_run_font(run, east, size, bold=bold)
        token = match.group(0)
        link_match = LINK_TOKEN.match(token)
        if link_match:
            add_hyperlink(
                paragraph,
                link_match.group(1),
                link_match.group(2),
                size=size,
                bold=bold,
                east=east,
            )
        elif token.startswith("**") and token.endswith("**"):
            add_inline(paragraph, token[2:-2], size=size, east=east, bold=True)
        elif token.startswith("`") and token.endswith("`"):
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, SONG, max(10, size - 1), bold=bold, ascii_font=CODE_FONT)
        else:
            run = paragraph.add_run(token)
            set_run_font(run, east, size, bold=bold)
        pos = match.end()
    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        set_run_font(run, east, size, bold=bold)


def add_body_paragraph(doc, text, *, size=16, first_line=True, before=0, after=0):
    paragraph = doc.add_paragraph()
    set_paragraph_format(
        paragraph,
        size=size,
        exact=28 if size >= 16 else 20,
        first_line=first_line,
        before=before,
        after=after,
    )
    add_inline(paragraph, text, size=size, east=FANG)
    return paragraph


def add_heading(doc, text, level):
    paragraph = doc.add_paragraph()
    if level == 1:
        font = HEI
        size = 16
        before, after = 8, 0
    elif level == 2:
        font = KAI
        size = 16
        before, after = 4, 0
    else:
        font = FANG
        size = 16
        before, after = 2, 0
    set_paragraph_format(
        paragraph,
        size=size,
        exact=28,
        first_line=False,
        before=before,
        after=after,
    )
    add_inline(paragraph, text, size=size, east=font, bold=(level >= 3))
    paragraph.paragraph_format.keep_with_next = True
    return paragraph


def add_title(doc, text):
    paragraph = doc.add_paragraph()
    set_paragraph_format(
        paragraph,
        size=22,
        exact=33,
        align=CENTER,
        first_line=False,
        before=0,
        after=18,
    )
    add_inline(paragraph, text, size=22, east=TITLE_FONT, bold=True)
    paragraph.paragraph_format.keep_with_next = True
    return paragraph


def add_quote(doc, text):
    paragraph = doc.add_paragraph()
    set_paragraph_format(
        paragraph,
        size=12,
        exact=20,
        first_line=False,
        left_mm=8,
        before=0,
        after=0,
    )
    add_inline(paragraph, text, size=12, east=FANG)
    return paragraph


def add_bullet(doc, text):
    paragraph = doc.add_paragraph()
    set_paragraph_format(
        paragraph,
        size=16,
        exact=28,
        first_line=False,
        left_mm=8,
        before=0,
        after=0,
    )
    add_inline(paragraph, "• " + text, size=16, east=FANG)
    return paragraph


def add_question(doc, text):
    paragraph = doc.add_paragraph()
    set_paragraph_format(
        paragraph,
        size=16,
        exact=28,
        first_line=False,
        before=4,
        after=0,
    )
    add_inline(paragraph, text, size=16, east=FANG, bold=True)
    paragraph.paragraph_format.keep_with_next = True
    return paragraph


def add_code_block(doc, lines):
    for line in lines:
        paragraph = doc.add_paragraph()
        set_paragraph_format(
            paragraph,
            size=11,
            exact=18,
            first_line=False,
            left_mm=8,
            before=0,
            after=0,
        )
        p_pr = paragraph._p.get_or_add_pPr()
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), "F2F2F2")
        p_pr.append(shading)
        run = paragraph.add_run(line)
        set_run_font(run, SONG, 11, ascii_font=CODE_FONT)


def split_table_row(line):
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [item.strip() for item in value.split("|")]


def is_table_separator(line):
    cells = split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def set_cell_text(cell, text, *, header=False):
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    paragraph = cell.paragraphs[0]
    paragraph.alignment = CENTER if header else LEFT
    paragraph.paragraph_format.line_spacing = Pt(18)
    paragraph.paragraph_format.space_before = Pt(1)
    paragraph.paragraph_format.space_after = Pt(1)
    paragraph.paragraph_format.first_line_indent = Pt(0)
    add_inline(paragraph, text, size=11 if not header else 11, east=HEI if header else FANG, bold=header)


def shade_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)
    for cell in row.cells:
        tc_pr = cell._tc.get_or_add_tcPr()
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), "EDEDED")
        tc_pr.append(shading)


def prevent_row_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:cantSplit"))


def table_widths(column_count):
    """返回 156 毫米正文宽度内的表格列宽。"""
    presets = {
        2: (42, 114),
        3: (32, 56, 68),
        4: (18, 54, 48, 36),
        5: (25, 31, 31, 31, 38),
        6: (25, 26, 26, 26, 26, 27),
    }
    if column_count in presets:
        return presets[column_count]
    width = 156 / column_count
    return tuple(width for _ in range(column_count))


def add_table(doc, headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    widths = table_widths(len(headers))
    header_row = table.rows[0]
    for index, value in enumerate(headers):
        header_row.cells[index].width = Mm(widths[index])
        set_cell_text(header_row.cells[index], value, header=True)
    shade_table_header(header_row)
    prevent_row_split(header_row)
    for row_values in rows:
        row = table.add_row()
        prevent_row_split(row)
        for index, value in enumerate(row_values):
            row.cells[index].width = Mm(widths[index])
            set_cell_text(row.cells[index], value)
    # 表格后留出少量空白，避免表格和下一段粘连。
    spacer = doc.add_paragraph()
    set_paragraph_format(spacer, size=1, exact=4, first_line=False, after=2)
    return table


def add_page_number(paragraph, size=14):
    run = paragraph.add_run("— ")
    set_run_font(run, SONG, size)
    run = paragraph.add_run()
    set_run_font(run, SONG, size)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)
    run = paragraph.add_run(" —")
    set_run_font(run, SONG, size)


def enable_even_odd_footers(doc):
    settings = doc.settings.element
    if settings.find(qn("w:evenAndOddHeaders")) is None:
        settings.append(OxmlElement("w:evenAndOddHeaders"))


def setup_document(doc):
    normal = doc.styles["Normal"]
    normal.font.name = ASCII_FONT
    normal.font.size = Pt(16)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), FANG)

    section = doc.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(37)
    section.bottom_margin = Mm(35)
    section.left_margin = Mm(28)
    section.right_margin = Mm(26)

    enable_even_odd_footers(doc)
    odd_footer = section.footer
    odd_footer.is_linked_to_previous = False
    odd_footer.paragraphs[0].alignment = RIGHT
    add_page_number(odd_footer.paragraphs[0])
    even_footer = section.even_page_footer
    even_footer.is_linked_to_previous = False
    even_footer.paragraphs[0].alignment = LEFT
    add_page_number(even_footer.paragraphs[0])


def build_document():
    with open(INPUT_PATH, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    doc = Document()
    setup_document(doc)
    doc.core_properties.title = "美国涉华公开信息监测系统答辩备答材料"
    doc.core_properties.subject = "项目答辩与领导提问备答"
    doc.core_properties.author = "项目组"
    doc.core_properties.comments = "由 docs/defense_qa.md 自动排版生成"

    index = 0
    title_written = False
    while index < len(lines):
        line = lines[index]

        if not line.strip():
            index += 1
            continue

        if line.strip() == "---":
            index += 1
            continue

        if line.startswith("```"):
            index += 1
            block = []
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            add_code_block(doc, block)
            continue

        if line.startswith("# ") and not title_written:
            add_title(doc, line[2:].strip())
            title_written = True
            index += 1
            continue

        if line.startswith("## "):
            add_heading(doc, line[3:].strip(), level=1)
            index += 1
            continue

        if line.startswith("### "):
            add_heading(doc, line[4:].strip(), level=2)
            index += 1
            continue

        if line.startswith("#### "):
            add_heading(doc, line[5:].strip(), level=3)
            index += 1
            continue

        if line.startswith("> "):
            add_quote(doc, line[2:].strip())
            index += 1
            continue

        if line.startswith("|") and index + 1 < len(lines) and is_table_separator(lines[index + 1]):
            headers = split_table_row(line)
            index += 2
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                values = split_table_row(lines[index])
                if len(values) == len(headers):
                    rows.append(values)
                index += 1
            add_table(doc, headers, rows)
            continue

        if line.startswith("- "):
            add_bullet(doc, line[2:].strip())
            index += 1
            continue

        if re.match(r"^\*\*\d+\.[^*]+\*\*$", line.strip()):
            add_question(doc, line.strip())
            index += 1
            continue

        add_body_paragraph(doc, line.strip())
        index += 1

    return doc


def main():
    doc = build_document()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    doc.save(OUTPUT_PATH)

    # 自校验：确认生成文件可重新打开，且正文与表格均存在。
    check = Document(OUTPUT_PATH)
    non_empty = [paragraph.text for paragraph in check.paragraphs if paragraph.text.strip()]
    if not non_empty:
        raise RuntimeError("生成的 Word 文档没有正文")
    print("输出文件：%s" % OUTPUT_PATH)
    print("文件大小：%d 字节" % os.path.getsize(OUTPUT_PATH))
    print("非空段落数：%d，表格数：%d" % (len(non_empty), len(check.tables)))
    print("标题：%s" % non_empty[0])
    print("末段：%s" % non_empty[-1])


if __name__ == "__main__":
    main()
