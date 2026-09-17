# -*- coding: utf-8 -*-
"""segment 级处理：上下文窗口构造 + 行内标签保真校验。

这两件事是 AI 后编辑（Post-Edit）区别于"直接调翻译 API"的地方：

1. 上下文窗口
   上游给的是逐句的机器译文，如果一句一句独立丢给模型，
   指代和语气会断裂（"它" 指的是谁？上一句的否定要不要延续？）。
   做法是给目标句配一个滑动窗口，前后各取 N 条一起进 Prompt，
   并在 Prompt 里明确标出哪一条是本次要处理的。

2. 行内标签保真
   译文最终要回写进下游翻译系统渲染，译文里的行内占位符
   必须与原文保持一一对应 —— 数量、编号、配对关系、转义形式都不能变，
   只允许按目标语言的语序调整位置。只要标签对不上，这条译文就是废的。
"""

import re

from . import config


# ==========================================================================
# 行内标签
# ==========================================================================
TAG_PATTERNS = {
    # {1} {2} —— 数字占位符，下游最常见的形式
    "curly": re.compile(r"\{(\d+)\}"),
    # %s %1$s %d —— printf 风格
    "printf": re.compile(r"%(?:\d+\$)?[sdf]"),
    # <b> </b> <g id="1"> —— XML 风格，需要区分开闭
    "xml": re.compile(r"<(/?)([a-zA-Z][\w\-]*)([^>]*?)(/?)>"),
}


def extract_tags(text):
    """抽取文本中的全部行内标签，按类型分组。

    返回 dict：
        curly  -> 编号列表（顺序保留）
        printf -> 原样字符串列表
        xml    -> [(是否闭合标签, 标签名, 是否自闭合)] 列表
        escaped-> 被转义的 &amp; / &lt; 等出现次数（用于判断转义形式是否被改动）
    """
    text = text or ""
    curly = [int(m.group(1)) for m in TAG_PATTERNS["curly"].finditer(text)]
    printf = [m.group(0) for m in TAG_PATTERNS["printf"].finditer(text)]
    xml = [(bool(m.group(1)), m.group(2), bool(m.group(4)))
           for m in TAG_PATTERNS["xml"].finditer(text)]
    escaped = len(re.findall(r"&(?:amp|lt|gt|quot|apos);|&#\d+;", text))
    return {"curly": curly, "printf": printf, "xml": xml, "escaped": escaped}


def _xml_balanced(tags):
    """用栈检查 XML 标签的配对关系。

    返回 (是否配对, 出问题的标签名)。自闭合标签不参与配对。
    """
    stack = []
    for is_close, name, self_closing in tags:
        if self_closing:
            continue
        if not is_close:
            stack.append(name)
        else:
            if not stack or stack.pop() != name:
                return False, name
    return (not stack), (stack[-1] if stack else None)


def check_tags(source, target):
    """校验译文的行内标签是否与原文一一对应。

    对应关系参考 MXLIFF 规范的四条约束：
        数量一致 / 编号集合一致 / 开闭配对一致 / 转义形式一致
    位置可以变（目标语言语序不同），所以这里只做集合与结构比较。

    返回 (是否通过, 明细 dict)
    """
    s = extract_tags(source)
    t = extract_tags(target)
    detail = {}

    detail["curly_count_ok"] = len(s["curly"]) == len(t["curly"])
    detail["curly_set_ok"] = sorted(s["curly"]) == sorted(t["curly"])

    detail["printf_count_ok"] = len(s["printf"]) == len(t["printf"])
    detail["printf_multiset_ok"] = sorted(s["printf"]) == sorted(t["printf"])

    detail["xml_count_ok"] = len(s["xml"]) == len(t["xml"])
    s_bal, s_bad = _xml_balanced(s["xml"])
    t_bal, t_bad = _xml_balanced(t["xml"])
    detail["xml_balanced_ok"] = s_bal and t_bal
    if not t_bal:
        detail["xml_unbalanced_at"] = t_bad

    detail["escape_form_ok"] = s["escaped"] == t["escaped"]

    detail["ok"] = all([
        detail["curly_count_ok"], detail["curly_set_ok"],
        detail["printf_count_ok"], detail["printf_multiset_ok"],
        detail["xml_count_ok"], detail["xml_balanced_ok"],
        detail["escape_form_ok"],
    ])
    return detail["ok"], detail


# ==========================================================================
# 上下文窗口
# ==========================================================================
def build_context(segments, index, window=None):
    """为第 index 条 segment 构造上下文窗口。

    参数
        segments: [{string_id, source}, ...] 按文档顺序排列
        index   : 目标句下标
        window  : 前后各取多少条，默认 config.CONTEXT_WINDOW

    返回 [{string_id, source, is_self}, ...]

    注意 is_self 标记：它让模型知道"这一条才是要你处理的，
    其余只是上下文，不要跟着一起改写"。缺了这个标记，
    模型会把上下文里的句子也翻一遍，造成重复输出。
    """
    window = config.CONTEXT_WINDOW if window is None else window
    lo = max(0, index - window)
    hi = min(len(segments), index + window + 1)
    out = []
    for i in range(lo, hi):
        seg = segments[i]
        out.append({
            "string_id": seg.get("string_id"),
            "source": seg.get("source", ""),
            "is_self": (i == index),
        })
    return out


def build_prompt_messages(segment, context, guidelines):
    """把一条 segment 组装成对话消息。

    guidelines 是 5 块可版本化的 Prompt 片段，对应平台上的
    General / Spec / Term（术语表）/ DNT（禁改词）/ Tag（标签规则）。
    拆成 5 块而不是一整块，是为了能单独回滚某一块的改动。
    """
    system_parts = ["You are a professional post-editor for machine translation."]
    for name in ("general", "spec", "term", "dnt", "tag"):
        text = guidelines.get(name)
        if text:
            system_parts.append("## %s\n%s" % (name.upper(), text))

    ctx_lines = []
    for item in context:
        flag = ">>> 待处理" if item.get("is_self") else "    上下文"
        ctx_lines.append("%s [%s] %s" % (flag, item.get("string_id"), item.get("source")))

    user_text = (
        "The lines below are a sliding window of the document. "
        "Only the line marked '>>> 待处理' is yours to process; "
        "the others are context for resolving references and tone. "
        "Do not translate the context lines.\n\n"
        + "\n".join(ctx_lines)
        + "\n\nMachine translation of the target line:\n%s\n\n"
        "Return JSON with keys: final_translation, evaluation_result (Y/N), comment."
        % segment.get("mt", "")
    )
    return [{"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": user_text}]
