"""模型把工具调用写成正文时，"补一轮提示"必须真的把工具放回去。

round8 的 H03（冷萃乌龙茶上市首月达标了吗）失分，根因是两个：

1. 检测漏了 SQL。模型在收尾轮想要查 8 月实际销量，于是把
   `SELECT SUM(qty) ... FROM sales WHERE ...` 当正文写了出来。
   `_RAW_TOOL_CALL` 只认 DSML 那一串和 `invoke name=`，不认裸 SQL，
   所以这一轮**根本没触发补正**，那句 SQL 连同半段检索原文直接成了最终答案。

2. 就算触发了补正，工具也没被放回去。`allow_tools` 的判据是
   `round_index < MAX_TOOL_ROUNDS + nudges`，而"写成正文"恰恰最容易发生在
   收尾轮（第 MAX_TOOL_ROUNDS 轮，此时本来就不给工具）。nudges 从 0 加到 1、
   round_index 也已经走到 7，"7 < 6 + 1" 是假的，于是补正那一轮照样没有工具。
   想调工具却够不着工具，模型只能再把标记写一遍：H03 实测连着写了两次正文标记，
   第三次干脆把检索到的原文片段当答案交出来。

第二条测试盯的就是"补正之后那一轮，allow_tools 到底是不是空的"。
"""

from __future__ import annotations

import pytest

from kbqa.live import MAX_TOOL_ROUNDS, LiveEngine, _RAW_TOOL_CALL
from kbqa.planner import Plan

#: 收尾轮模型把想执行的查询写成了正文。
RAW_SQL = (
    "SELECT SUM(qty) AS total_qty FROM sales "
    "WHERE product_id='P21' AND date BETWEEN '2026-08-01' AND '2026-08-31'"
)

#: 另一种写法：DSML 标记（第六轮 H06 就是这种）。用 chr(60) 拼尖括号，
#: 免得源码里出现一长串看着像 HTML 的字符。
RAW_DSML = (
    chr(60) + "||DSML|| calls" + chr(62) + " "
    + chr(60) + "||DSML|| invoke name=" + chr(34) + "search_kb" + chr(34) + chr(62)
)

GOOD_ANSWER = "冷萃乌龙茶上市首月全门店实际销量 689 杯，未达标。"


class _Reply:
    def __init__(self, content="", tool_calls=None, call_id=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.message = {"role": "assistant", "content": content}
        if call_id:
            self.message["tool_calls"] = self.tool_calls


class _ScriptedClient:
    """按脚本依次返回；同时把每一轮拿到的 allow_tools 记下来。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.allow_tools = []
        self.seen = 0

    def chat_with_retry(self, messages, allow_tools=None, budget=None, on_call=None):
        self.allow_tools.append(allow_tools)
        index = min(self.seen, len(self._replies) - 1)
        self.seen += 1
        return self._replies[index]


class _Trace:
    def step(self, *args, **kwargs):
        pass

    def llm(self, *args, **kwargs):
        pass


def _build_engine(docs_engine):
    """把 docs_engine 补成一个能跑 `answer()` 的最小引擎。"""
    engine = LiveEngine.__new__(LiveEngine)
    engine.answerer = docs_engine.answerer
    engine.today = docs_engine.today
    engine.data_period = {"start": "2026-05-01", "end": "2026-08-31"}
    engine.budget = 120.0
    engine.run_tool = lambda name, params: {"rows": [{"total_qty": 689}]}
    return engine


def _plan():
    question = "冷萃乌龙茶上市第一个月的销量达标了吗？"
    return Plan(question=question, standalone=question, search_query=question)


def _tool_call_reply(call_id):
    calls = [
        {
            "id": call_id,
            "function": {"name": "query_metrics", "arguments": "{}"},
        }
    ]
    return _Reply("", tool_calls=calls, call_id=call_id)


def test_bare_sql_counts_as_a_tool_call_written_as_text():
    """裸 SQL 是"想调工具"的另一种写法，必须被认出来。"""
    assert _RAW_TOOL_CALL.search(RAW_SQL), "裸 SQL 没有被识别成工具调用"
    assert _RAW_TOOL_CALL.search(RAW_DSML), "DSML 那一串必须继续认得住"


@pytest.mark.parametrize("leak", [RAW_DSML, RAW_SQL])
def test_tools_are_reopened_after_a_tool_call_written_as_text(docs_engine, leak):
    """收尾轮写成正文并触发补正后，下一轮必须重新拿到工具。

    脚本：前 MAX_TOOL_ROUNDS 轮正常调工具（把轮次耗到收尾轮），
    第 MAX_TOOL_ROUNDS 轮写成正文 → 触发补正，下一轮就该带工具。
    DSML 与裸 SQL 两种写法都要走通，因为模型两种都用过。
    """
    script = [_tool_call_reply("c%d" % i) for i in range(MAX_TOOL_ROUNDS)]
    script.append(_Reply(leak))             # 收尾轮：把查询写成正文
    script.append(_Reply(GOOD_ANSWER))      # 补正那一轮

    client = _ScriptedClient(script)
    engine = _build_engine(docs_engine)
    engine.client = client

    answer = engine.answer(_plan(), _Trace(), [])

    # 收尾轮（下标 MAX_TOOL_ROUNDS）本来就不给工具，这一点不能变。
    assert client.allow_tools[MAX_TOOL_ROUNDS] is None, "收尾轮不该给工具"
    # 但补正那一轮必须给：它就是想调工具才把调用写成了正文。
    assert client.allow_tools[MAX_TOOL_ROUNDS + 1], (
        "补正之后那一轮没有把工具放回去，模型想调也调不了"
    )
    assert answer.answer == GOOD_ANSWER
