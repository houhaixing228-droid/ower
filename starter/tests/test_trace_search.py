"""调试面板（第四关）的数据来源：**live 路径也必须留下检索证据**。

现象：前端的"检索到的片段（含分数）"和"被过滤掉的文档"两块在真实使用里一直是空的。

根因不在前端。`Retriever.search()` 本来就返回一个 `SearchResult`，里面有
每个片段的分数、被版本元数据过滤掉的文档与原因、覆盖率；`answerer.py`（mock 路径）
也老老实实把它记成了 trace 里的一步。但 live 路径是另一条：

- `service.retrieve()` 只把 `results` 交出去，`SearchResult` 的分数/过滤信息当场丢掉；
- `live.py` 只记 `trace.step("tool", {"tool": name, "params": params})`——没有命中、没有分数；
- 前端去 `step.step == "search"` 里找命中，而 live 的 trace 里根本没有这一步，
  于是两块永远渲染成"这一轮没有检索"。

所以修法是把检索证据一路带到 trace，但有两个边界不能破：

1. `/api/retrieve` 的响应必须还是契约 §4 的形状（只有 `results`），
   内部调试字段不许漏出去；
2. 新加的 `trace` 键**不能**跟着工具结果进模型上下文——那是给调试面板看的，
   塞进去只会白占额度。
   （BM25 的 `score` 本来就在工具结果里，属既有行为，这次不动它：改它会动到
   模型看到的上下文、把评测分数一起搅进来，代价和收益不成比例。）
"""

from __future__ import annotations

import json

from kbqa.live import LiveEngine
from kbqa.planner import Plan
from kbqa.trace import Trace

QUESTION = "外卖订单多久内可以申请退款？"
ANSWER = "外卖订单在送达后 24 小时内可以申请退款，以现行退款政策为准。"
#: mock 模式下会真的引到 KB-013 的问句（上面那句会被判成 data 型，不带引用）。
QUESTION_CITED = "退款政策是怎么规定的？"


class _Reply:
    def __init__(self, content="", tool_calls=None, call_id=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.message = {"role": "assistant", "content": content}
        if call_id:
            self.message["tool_calls"] = self.tool_calls


class _ScriptedClient:
    """按脚本返回；每轮把 messages 的快照留下来，用来检查发给模型的内容。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.messages = []

    def chat_with_retry(self, messages, allow_tools=None, budget=None, on_call=None):
        self.messages.append(json.loads(json.dumps(messages, ensure_ascii=False, default=str)))
        index = min(len(self.messages) - 1, len(self._replies) - 1)
        return self._replies[index]


def _search_then_answer():
    call_id = "call_search_1"
    calls = [
        {
            "id": call_id,
            "function": {
                "name": "search_kb",
                "arguments": json.dumps({"query": QUESTION, "top_k": 5}, ensure_ascii=False),
            },
        }
    ]
    return [_Reply("", tool_calls=calls, call_id=call_id), _Reply(ANSWER)]


def _build_engine(docs_engine, run_tool):
    engine = LiveEngine.__new__(LiveEngine)
    engine.answerer = docs_engine.answerer
    engine.today = docs_engine.today
    engine.data_period = {"start": "2026-05-01", "end": "2026-08-31"}
    engine.budget = 120.0
    engine.run_tool = run_tool
    return engine


def _plan():
    return Plan(question=QUESTION, standalone=QUESTION, search_query=QUESTION)


def _tool_messages(client):
    """把发给模型的 tool 消息内容找出来。"""
    found = []
    for snapshot in client.messages:
        for message in snapshot:
            if message.get("role") == "tool":
                found.append(message.get("content") or "")
    return found


# -- 1. 检索证据本身要能拿到 -------------------------------------------------


def test_retrieve_hands_out_hit_scores_and_filtered_docs(client):
    """`Service.retrieve` 必须把分数与过滤原因一起交出来，否则面板无从渲染。"""
    from kbqa.server import service

    payload = service().retrieve(QUESTION, 5)
    assert payload["results"], "检索结果本身是对的，这一条是背景"

    detail = payload.get("trace")
    assert detail, "检索的分数与过滤原因没有被交出来，调试面板只能显示“这一轮没有检索”"
    assert detail["hits"], "命中列表是空的"
    assert detail["hits"][0]["score"] == 42.0, "分数没带出来"
    assert "filtered" in detail, "被过滤掉的文档没有带出来"
    assert "coverage" in detail


def test_api_retrieve_keeps_the_contract_shape(client):
    """契约 §4：/api/retrieve 只返回 results，内部调试字段不许漏出去。"""
    body = client.post("/api/retrieve", json={"query": QUESTION, "top_k": 5}).json()
    assert set(body) == {"results"}, "响应里多了别的键：%s" % sorted(body)


# -- 2. live 路径要把这一步记进 trace ----------------------------------------


def test_live_records_a_search_step_with_scores(client, docs_engine):
    """live 回答的 trace 里必须有 search 步骤，且带分数——前端就是从这儿取数的。"""
    from kbqa.server import service

    engine = _build_engine(docs_engine, run_tool=service().run_tool)
    engine.client = _ScriptedClient(_search_then_answer())
    trace = Trace(trace_id="t-test-0001", question=QUESTION)

    engine.answer(_plan(), trace, [])

    searches = [step for step in trace.steps if step["step"] == "search"]
    assert searches, "live 路径没有记 search 步骤，前端的检索面板永远是空的"
    detail = searches[0]["detail"]
    assert detail["hits"][0]["doc_id"] == "KB-013"
    assert detail["hits"][0]["score"] == 42.0
    assert "filtered" in detail


def test_internal_trace_field_never_goes_into_the_request_sent_to_the_model(client, docs_engine):
    """`trace` 是给调试面板的内部字段，发给模型的工具结果里不能有它。

    注意这里**不**断言 `score`：BM25 分数本来就在工具结果里（`as_result()` 带的），
    那是既有行为，改它会动到模型看到的上下文、把评测分数一起搅进来，
    不值得为了这一关去动。这里只管我新加的那个键。
    """
    from kbqa.server import service

    scripted = _ScriptedClient(_search_then_answer())
    engine = _build_engine(docs_engine, run_tool=service().run_tool)
    engine.client = scripted
    engine.answer(_plan(), Trace(trace_id="t-test-0002", question=QUESTION), [])

    contents = _tool_messages(scripted)
    assert contents, "脚本里有一次 search_kb 调用，应该至少回传一条 tool 消息"
    for content in contents:
        assert '"trace"' not in content, "内部 trace 字段被塞进模型上下文了：%s" % content[:200]
    # 反过来也要成立：这份工具结果确实是那条检索，别把断言写成永远为真。
    assert "KB-013" in contents[0]


# -- 3. 面板还要能看出"检索了 5 片、最后用了哪几片" --------------------------


def test_response_step_records_the_citations_that_were_actually_used(client):
    """答错时要一眼看出"检索到的片段里最终用了哪几篇"，所以 response 步骤要写明引用。

    只看"检索到了什么"不够：真正要排查的往往是"第 2 片该用却没用上"。
    """
    from kbqa.server import service

    api = service()
    payload = api.chat("s-trace-cite", QUESTION_CITED)
    assert payload["citations"], "这条问答本身就该有引用，不然下面的断言是空转"

    trace = api.get_trace(payload["trace_id"])
    response = [step for step in trace["steps"] if step["step"] == "response"][-1]
    detail = response["detail"]
    assert detail.get("citations") == [item["doc_id"] for item in payload["citations"]], (
        "response 步骤没有记下这次实际用了哪几篇文档，面板无法把「采用」和「只是检索到」分开"
    )


# -- 4. 面板和 trace 之间的字段约定，要有人守 --------------------------------


def test_trace_carries_every_field_the_debug_panel_reads(client):
    """面板读的字段就是前后端之间的契约——这次坏掉正是因为没人守它。

    前端的 `loadTrace()` 从 `search` 步骤里取 `query/hits/filtered/coverage`，
    从每个 hit 里取 `doc_id/chunk_id/score/padded/dropped_instructions`，
    从 `response` 步骤里取 `citations`。字段名一改，面板不会报错，
    只会安静地显示"这一轮没有检索"——所以把它钉成测试。
    """
    from kbqa.server import service

    api = service()
    payload = api.chat("s-panel-shape", QUESTION_CITED)
    trace = api.get_trace(payload["trace_id"])

    searches = [step for step in trace["steps"] if step["step"] == "search"]
    assert searches, "面板在 search 步骤里取检索证据，这一步不能没有"
    detail = searches[0]["detail"]
    for key in ("query", "hits", "filtered", "coverage"):
        assert key in detail, "面板要读 search 步骤的 detail.%s" % key

    assert detail["hits"], "这条问答检索到了片段，hits 不该是空的"
    for key in ("doc_id", "chunk_id", "score", "padded", "dropped_instructions"):
        assert key in detail["hits"][0], "面板要读 hit.%s" % key

    response = [step for step in trace["steps"] if step["step"] == "response"][-1]
    assert "citations" in response["detail"], "面板要读 response 步骤的 detail.citations"
