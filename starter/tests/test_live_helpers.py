"""live 模式里几个纯函数的回归测试。

_wants_value 之前只有调用没有定义，跑的时候才炸，测试全绿照样上线——
这类"定义在别处"的坑补个用例最省事。
"""

from __future__ import annotations

from kbqa.live import _compact_result, _wants_value


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
    days = [{"date": "2025-07-%02d" % day, "net_revenue": float(day), "orders": day} for day in range(1, 31)]

    class _Plan:
        window = ("2025-07-10", "2025-07-12")

    compacted = _compact_result({"days": days}, _Plan())
    assert len(compacted["days"]) == 14
    assert compacted["days_total"] == 30
    kept = {day["date"] for day in compacted["days"]}
    assert {"2025-07-10", "2025-07-11", "2025-07-12"} <= kept
    assert "省略" in compacted["note"]
