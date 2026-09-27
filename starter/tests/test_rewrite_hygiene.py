"""数字核对失败后的重写，产物不能再把工具调用标记当正文交出去。

第七轮复测 T02 第 3 轮暴露的：引用修好之后（29678b7 / afae647），链路走到
"数字核对失败 → 请模型重写"，模型把重写又写成了 DSML 工具调用。
`_finalise` 拿到正文时会剥标记（_strip_raw_tool_calls），但 `_rewrite_with_allowed`
的产物直接替换了 text，这段产物没过剥离——一段尖括号原样成了最终答案。

`_strip_raw_tool_calls` 本身是好的（直接验证过，对这段文本能剥干净），
漏的只是重写这条路径没调用它。
"""

from __future__ import annotations

from kbqa.live import LiveEngine
from kbqa.planner import Plan

#: 第七轮实测里模型重写出来的那段（全角竖线是它本来的样子）。
RAW_DSML = (
    "<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name=\"search_kb\"> "
    "<｜｜DSML｜｜ parameter name=\"query\" string=\"true\">Tasman 三文鱼 赔付 金额"
    "</｜｜DSML｜｜ parameter> </｜｜DSML｜｜ invoke> </｜｜DSML｜｜ calls>"
)


class _Reply:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []


class _FakeClient:
    """重写那一轮模型返回什么，由用例指定。"""

    def __init__(self, content):
        self._content = content

    def chat_with_retry(self, messages, allow_tools=None, budget=None, on_call=None):
        return _Reply(self._content)


class _Trace:
    def step(self, *args, **kwargs):
        pass

    def llm(self, *args, **kwargs):
        pass


def test_rewrite_output_never_leaks_raw_tool_calls(docs_engine):
    question = "供应商后来赔了多少？"
    plan = Plan(question=question, standalone=question, search_query=question)
    docs_engine.client = _FakeClient(RAW_DSML)

    answer = docs_engine._finalise(
        plan,
        "赔偿金额为 8600 元。",
        [],
        {},
        _Trace(),
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": question},
        ],
        history=[],
    )

    assert "DSML" not in answer.answer
    assert "invoke" not in answer.answer
    assert "｜" not in answer.answer
