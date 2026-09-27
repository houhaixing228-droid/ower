"""live 模式里几个纯函数的回归测试。

_wants_value 之前只有调用没有定义，跑的时候才炸，测试全绿照样上线——
这类"定义在别处"的坑补个用例最省事。

_raw tool call 那两条来自第六轮 H06：最后那一轮为了逼模型收尾不再给它工具，
它就把工具调用当正文写了出来，一段标记原样当成答案返回，这一题直接 0 分。
"""

from __future__ import annotations

from kbqa.live import (
    _RAW_TOOL_CALL,
    _compact_result,
    _strip_raw_tool_calls,
    _wants_value,
)

# 模型把工具调用当正文写出来时的样子，避开引号和尖括号以免和源码混在一起。
RAW = "".join(
    [
        "<|DSML|> invoke name=",
        "search_kb",
        " parameter name=query: S02 停业通知",
    ]
)


def test_raw_tool_call_is_detected():
    assert _RAW_TOOL_CALL.search(RAW)
    assert _RAW_TOOL_CALL.search('invoke name="daily_metrics"')
    # 正常回答不能被误判
    assert not _RAW_TOOL_CALL.search("8 月 3 日 S05 的现金支付占比是 100%。")


def test_raw_tool_calls_are_stripped_from_answer():
    text = "S02 那三天没有营业额。" + RAW + "补充说明。"
    cleaned = _strip_raw_tool_calls(text)
    assert "invoke" not in cleaned
    assert "S02 那三天没有营业额。" in cleaned
    assert "补充说明。" in cleaned


def test_strip_leaves_normal_text_alone():
    text = "外卖订单在送达后 24 小时内可以申请退款。"
    assert _strip_raw_tool_calls(text) == text


def test_wants_value_true():
    for question in (
        "门店月度营业额的目标是多少",
        "这次食品安全事故赔了多少",
        "外卖平台的抽成比例是多少",
        "毛利率多少",
    ):
        assert _wants_value(question), question


def test_wants_value_false():
    for question in (
        "现在的退款政策是什么",
        "最新的排班制度怎么规定的",
        "顾客投诉主要集中在哪些方面",
    ):
        assert not _wants_value(question), question


def test_compact_result_keeps_short():
    result = {"days": [{"date": "2025-07-01", "net_revenue": 1.0, "orders": 2}]}
    assert _compact_result(result) is result


def test_compact_result_prefers_asked_window():
    days = [
        {"date": "2025-07-%02d" % day, "net_revenue": float(day), "orders": day}
        for day in range(1, 31)
    ]

    class _Plan:
        window = ("2025-07-10", "2025-07-12")

    compacted = _compact_result({"days": days}, _Plan())
    assert len(compacted["days"]) == 14
    assert compacted["days_total"] == 30
    kept = {day["date"] for day in compacted["days"]}
    assert {"2025-07-10", "2025-07-11", "2025-07-12"} <= kept
    assert "省略" in compacted["note"]
