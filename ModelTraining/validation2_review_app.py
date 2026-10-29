import streamlit as st
import json
import os
import re
import pandas as pd

# ================= 配置 =================
DATA_FILE = "Data/result_ckpt100.jsonl"
REVIEWED_FILE = "eval_reviewed.jsonl"

st.set_page_config(layout="wide", page_title="Model Evaluation Tool")


# streamlit run validation2_review_app.py


# --- Helper Functions ---
def load_data():
    data = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                data.append(json.loads(line))
    return data


def save_progress(index, label, comment, current_data):
    # 更新内存中的数据
    current_data[index]['human_label'] = label
    current_data[index]['comments'] = comment

    # 追加/覆盖写入文件 (这里简单处理：每次全部重写，数据量不大时没问题)
    # 实际生产中建议 Append 模式或数据库
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        for entry in current_data:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def extract_primary_category(rate_data):
    """
    从 RATE 字典中提取除 [内容准确率, 规模及影响, 潜力及传承] 之外的最高分领域。
    返回: (CategoryName, Score)
    """
    if not isinstance(rate_data, dict):
        return "N/A", 0

    # 1. 定义黑名单 (不需要参与比较的 key)
    exclude_keys = {"内容准确率", "规模及影响", "潜力及传承"}

    # 2. 筛选：只保留不在黑名单里的项
    # candidates 格式: {'国家政策': 0, '社会事件': 3, ...}
    candidates = {k: v for k, v in rate_data.items() if k not in exclude_keys}

    if not candidates:
        return "无有效领域", 0

    # 3. 找出分数最高的 Key
    # max(candidates, key=candidates.get) 会返回 value 最大的那个 key
    best_category = max(candidates, key=candidates.get)
    best_score = candidates[best_category]

    return best_category, best_score


# --- 1. 增强版 JSON 解析器 ---
def safe_parse_json(text):
    """
    尝试解析 JSON，如果失败返回 None。
    支持处理字典对象、标准 JSON 字符串、Markdown 代码块包裹的 JSON。
    """
    if text is None:
        return None
    if isinstance(text, dict):
        return text

    # 如果是字符串，尝试解析
    text = str(text).strip()

    # 情况 A: 已经是干净的 JSON
    try:
        return json.loads(text)
    except Exception:
        # 多格式提取链第 1 步失败属预期，落入第 2 步继续尝试
        pass

    # 情况 B: 被 ```json ... ``` 包裹
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            # 多格式提取链第 2 步失败属预期，落入第 3 步继续尝试
            pass

    # 情况 C: 只有一部分是 JSON，尝试提取第一个 { 到最后一个 }
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            # 多格式提取链第 3 步失败：按解析失败返回 None（调用方有空值分支）
            pass

    # 解析失败
    return None


# --- 2. 改进版渲染函数：出错时展示原文 ---
def render_content_card(column, title, raw_data, style="default"):
    """
    raw_data: 可能是字符串(模型输出)，也可能是字典(Ground Truth)
    """
    # 尝试解析
    data_dict = safe_parse_json(raw_data)

    with column:
        st.markdown(f"### {title}")

        # =================================================
        # 【核心修改】 如果解析失败 (data_dict 为 None)
        # =================================================
        if data_dict is None:
            st.error("⚠️ JSON Parse Error (格式错误)")

            # 这种情况下，直接把 raw_data 作为普通文本展示出来
            # 使用 st.code 可以保留格式（换行符等），方便 debug
            st.caption("原始输出 (Raw Output):")
            st.code(str(raw_data), language="text", line_numbers=True)

            # 可以在这里结束，也可以继续显示下面的完整查看器
            # return

        # =================================================
        #  如果解析成功，显示漂亮的指标
        # =================================================
        else:
            # 1. 提取指标
            primary_cat, primary_score = "N/A", 0
            impact_score = 0
            accuracy_score = 0

            if "RATE" in data_dict:
                primary_cat, primary_score = extract_primary_category(data_dict["RATE"])
                impact_score = data_dict["RATE"].get("规模及影响", 0)
                accuracy_score = data_dict["RATE"].get("内容准确率", 0)

            # 2. 显示三列指标
            m1, m2, m3 = st.columns(3)
            m1.metric(label="主要领域", value=primary_cat, delta=f"{primary_score}分")
            m2.metric(label="规模影响", value=impact_score)
            m3.metric(label="内容准确", value=accuracy_score)
            st.divider()

            # 3. 显示摘要文本
            display_text = data_dict.get("EVENT_TEXT", str(raw_data))
            if style == "success":
                st.success(display_text)
            elif style == "warning":
                st.warning(display_text)
            else:
                st.info(display_text)

        # =================================================
        #  底部：无论成功失败，都提供折叠框查看原始数据
        # =================================================
        with st.expander("查看原始数据 (Source)"):
            if data_dict:
                st.json(data_dict)
            else:
                # 解析失败时，这里再次显示 raw_data，防止上面没看清
                st.text(str(raw_data))


# --- Main App Logic ---
def main():
    st.title("🤖 LLM Fine-tuning Human Reviewer")

    # 1. 初始化 Session State
    if 'data' not in st.session_state:
        st.session_state.data = load_data()

    if 'current_index' not in st.session_state:
        # 找到第一个还没评审的数据 (human_label is None)
        unreviewed_indices = [i for i, d in enumerate(st.session_state.data) if d.get('human_label') is None]
        st.session_state.current_index = unreviewed_indices[0] if unreviewed_indices else 0

    data = st.session_state.data
    idx = st.session_state.current_index

    # 进度条
    reviewed_count = sum(1 for d in data if d.get('human_label') is not None)
    total_count = len(data)
    st.progress(reviewed_count / total_count if total_count > 0 else 0)
    st.caption(f"Progress: {reviewed_count}/{total_count}")

    if idx < total_count:
        item = data[idx]

        # --- 界面布局 ---
        st.subheader(f"Sample #{idx + 1}")

        # 对比区 (左右两栏)
        col1, col2 = st.columns(2)

        render_content_card(
            column=col1,
            title="✅ Ground Truth",
            raw_data=item.get('ground_truth', '{}'),
            style="success"
        )

        # 右边：Model Output
        render_content_card(
            column=col2,
            title="🤖 Model Output",
            raw_data=item.get('model_output', '{}'),
            style="warning"
        )

        # --- 操作区 ---
        st.divider()
        c1, c2, c3 = st.columns([1, 1, 4])

        with c1:
            if st.button("👍 Good / Pass", use_container_width=True, type="primary"):
                save_progress(idx, "pass", "", data)
                st.session_state.current_index += 1
                st.rerun()

        with c2:
            if st.button("👎 Bad / Fail", use_container_width=True):
                save_progress(idx, "fail", "", data)
                st.session_state.current_index += 1
                st.rerun()

        with c3:
            # 允许写备注
            comment = st.text_input("Optional Comments (e.g. 'Hallucination', 'Wrong Score')", key="comment_input")
            if st.button("Submit with Comment"):
                save_progress(idx, "commented", comment, data)
                st.session_state.current_index += 1
                st.rerun()

        # 导航按钮
        st.divider()
        prev, _, next_btn = st.columns([1, 8, 1])
        if prev.button("Previous"):
            st.session_state.current_index = max(0, idx - 1)
            st.rerun()
        if next_btn.button("Next"):
            st.session_state.current_index = min(len(data) - 1, idx + 1)
            st.rerun()

        # 输入展示区 (折叠以节省空间)
        with st.expander("Input Prompt / Instruction", expanded=True):
            st.info(f"**Instruction:** {item['instruction']}")
            st.text(f"**Input:** {item['input']}")

    else:
        st.balloons()
        st.success("🎉 All samples reviewed! You can calculate the accuracy now.")

        # --- 修复 KeyError 的部分 ---
        if data:
            df = pd.DataFrame(data)

            # 1. 安全检查：确保列存在
            if 'human_label' in df.columns:
                st.write("### Label Distribution")
                # 统计各标签数量
                counts = df['human_label'].value_counts()
                st.write(counts)

                # 可选：简单的可视化
                st.bar_chart(counts)
            else:
                st.info("No labels found yet (all items are unreviewed or missing 'human_label' field).")
        else:
            st.warning("No data loaded.")
        # ---------------------------

        # 下载最终结果
        st.download_button(
            label="Download Reviewed JSONL",
            data=json.dumps(data, indent=2, ensure_ascii=False),
            file_name="reviewed_final.json",
            mime="application/json"
        )


if __name__ == "__main__":
    main()
