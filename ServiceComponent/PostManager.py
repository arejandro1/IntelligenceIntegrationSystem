import os
import re
import hashlib
import html
import markdown

# 允许的 HTML 标签白名单（markdown 渲染结果消毒用）
_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "del", "ins", "sub", "sup",
    "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre", "code",
    "a", "img", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "span", "div", "figure", "figcaption",
}
# 允许的标签 → 允许的属性（仅安全属性）
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class"},
    "th": {"align"},
    "td": {"align"},
}

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>")

def sanitize_html_content(raw_html: str) -> str:
    """
    对 markdown 生成的 HTML 做白名单消毒：
    - 移除 script/style/iframe/object/embed/form 等危险标签。
    - 标签属性仅保留白名单内安全属性，并转义属性值。
    - 转义残留的 < > 防止标签走私。
    """
    def _sanitize_attrs(tag: str, attrs_raw: str) -> str:
        tag = tag.lower()
        allowed = _ALLOWED_ATTRS.get(tag, set())
        kept = []
        # 匹配 attrs: name="value" 或 name='value'
        for m in re.finditer(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*')", attrs_raw):
            name, value = m.group(1).lower(), m.group(2)
            if name not in allowed:
                continue
            if name.startswith("on"):
                continue
            value = html.escape(value[1:-1], quote=True)
            kept.append(f'{name}="{value}"')
        return (" " + " ".join(kept)) if kept else ""

    def _replace(m: re.Match) -> str:
        closing, tag, attrs_raw = m.group(1), m.group(2).lower(), m.group(3)
        if tag in _ALLOWED_TAGS:
            if closing:
                return f"</{tag}>"
            return f"<{tag}{_sanitize_attrs(tag, attrs_raw)}>"
        # 未知/危险标签：保留其文本内容（去掉标签本身）
        return ""

    sanitized = _TAG_RE.sub(_replace, raw_html)
    return sanitized


def generate_html_from_markdown(md_file_path: str, allowed_base_dir: str = "posts") -> str:
    """
    Convert Markdown file to HTML with caching mechanism and enhanced styling.

    Args:
        md_file_path: Path to the Markdown file
        allowed_base_dir: 允许读取的目录（必须包含该文件），用于路径逃逸防护

    Returns:
        str: Path to generated HTML file if successful, empty string otherwise
    """
    # 路径逃逸防护：文件必须位于 allowed_base_dir 目录内
    try:
        base_real = os.path.realpath(allowed_base_dir)
        file_real = os.path.realpath(md_file_path)
        if not (file_real == base_real or file_real.startswith(base_real + os.sep)):
            print(f"Path escape blocked: {md_file_path}")
            return ""
    except Exception as e:
        print(f"Path validation error: {str(e)}")
        return ""

    # Validate input file
    if not os.path.isfile(md_file_path):
        return ""

    # Configure output directory
    output_dir = "templates/generated"
    os.makedirs(output_dir, exist_ok=True)

    # Generate file names
    base_name = os.path.splitext(os.path.basename(md_file_path))[0]
    html_file = os.path.join(output_dir, f"{base_name}.html")
    hash_file = os.path.join(output_dir, f"{base_name}.hash")

    try:
        # Calculate current file hash
        with open(md_file_path, "rb") as f:
            current_hash = hashlib.md5(f.read()).hexdigest()

        # Check cache validity
        if (os.path.exists(html_file) and
                os.path.exists(hash_file) and
                open(hash_file, "r").read() == current_hash):
            return html_file  # Valid cache exists

        # Read and convert Markdown
        with open(md_file_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        html_content = markdown.markdown(md_content, extensions=["fenced_code", "tables"])
        # 消毒：移除危险标签与事件属性，防止存储型 XSS / SSTI 载荷被注入
        html_content = sanitize_html_content(html_content)

        full_html = f"""<!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>{base_name}</title>
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/mvp.css@1.12/mvp.min.css" id="base-css">
            <style>
                /* 马卡龙渐变背景 */
                body {{
                    background: linear-gradient(135deg, 
                        #F8C3CD 0%,   /* 浅粉色 */
                        #A2E4B8 30%,  /* 薄荷绿 */
                        #87CEEB 70%,  /* 天蓝色 */
                        #FFF5C3 100%  /* 奶油黄 */
                    ) fixed;
                    background-size: 300% 300%;
                    animation: gradientBG 15s ease infinite;
                    padding: 20px 0;
                    min-height: 100vh;
                }}
                
                /* 动态背景动画 */
                @keyframes gradientBG {{
                    0% {{ background-position: 0% 50% }}
                    50% {{ background-position: 100% 50% }}
                    100% {{ background-position: 0% 50% }}
                }}
                
                /* 内容区域样式 */
                .markdown-body {{
                    max-width: 800px;
                    margin: 0 auto;
                    padding: 30px;
                    background: rgba(255, 255, 255, 0.85); /* 半透明白色 */
                    border-radius: 12px;
                    box-shadow: 0 8px 32px rgba(162, 228, 184, 0.2); /* 薄荷绿阴影 */
                    backdrop-filter: blur(4px); /* 毛玻璃效果 */
                    border: 1px solid rgba(255, 245, 195, 0.3); /* 奶油黄边框 */
                }}
                
                /* 主题选择器优化 */
                .theme-selector {{
                    background: rgba(255, 255, 255, 0.9) !important;
                    border: 1px solid #F8C3CD !important; /* 浅粉色边框 */
                }}
            </style>
        </head>
        <body>
            <div class="theme-selector">
                <select id="cssSelector" onchange="changeTheme()">
                    <option value="">-- 选择样式 --</option>
                    <option value="https://cdn.jsdelivr.net/npm/mvp.css@1.12/mvp.min.css" selected>MVP.css</option>
                    <option value="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.1.0/github-markdown-dark.min.css">GitHub风格（暗色）</option>
                    <option value="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.1.0/github-markdown.min.css">GitHub风格（亮色）</option>
                    <option value="https://cdn.jsdelivr.net/npm/water.css@2/out/dark.min.css">Water.css（暗色）</option>
                    <option value="https://cdn.jsdelivr.net/npm/@picocss/pico@1/css/pico.min.css">Pico.css（暗色）</option>
                    <option value="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">Water.css（轻量）</option>
                    <option value="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css">Bootstrap 5</option>
                    <option value="https://cdn.jsdelivr.net/npm/milligram@1.4.1/dist/milligram.min.css">Milligram（极简）</option>
                    <option value="https://cdn.jsdelivr.net/npm/@picocss/pico@1/css/pico.min.css">Pico.css</option>
                    <option value="https://cdn.jsdelivr.net/npm/bulma@0.9.3/css/bulma.min.css">Bulma</option>
                    <option value="https://cdn.jsdelivr.net/npm/sakura.css/css/sakura.css">Sakura（樱花）</option>
                    <option value="https://cdn.jsdelivr.net/npm/holiday.css@0.9.8">Holiday.css</option>
                    <option value="https://cdn.jsdelivr.net/npm/awsm.css@3.0.7/dist/awsm.min.css">awsm.css</option>
                    <option value="https://cdn.jsdelivr.net/npm/papercss@1.6.1/dist/paper.min.css">PaperCSS（手绘风）</option>
                    <option value="https://cdn.jsdelivr.net/npm/retro.css@1.2.0/dist/retro.css">Retro（复古）</option>
                </select>
            </div>

            <div class="markdown-body">
                {html_content}
            </div>

            <script>
                function changeTheme() {{
                    const selectedUrl = document.getElementById("cssSelector").value;
                    if (selectedUrl) {{
                        const baseCSS = document.getElementById("base-css"); // 通过 ID 获取元素
                        baseCSS.href = selectedUrl; // 直接修改 href
                    }}
                }}
            </script>
        </body>
        </html>"""

        # Save files
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(full_html)
        with open(hash_file, "w", encoding="utf-8") as f:
            f.write(current_hash)

        return html_file

    except Exception as e:
        print(f"Error processing {md_file_path}: {str(e)}")
        return ""
