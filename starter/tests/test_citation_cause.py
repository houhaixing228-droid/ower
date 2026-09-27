"""问“为什么”时，引用要一直引到写原因的那一句。

第六轮 C07（S04 为什么不卖吞拿鱼三明治了）丢的 2 分不是代码改坏的：
第五轮模型自己在答案里写了“毛利率低于 35%”，第六轮改写成“毛利率连续两个月偏低”，
那个 35% 就只剩引用 quote 一处可以落脚——而 quote 引的是“会议决定下架”那句决议，
没把写原因的句子带上。评测核对事实时答案和 quote 两头都看，两头都没有，就判缺失。

mock 路径早就处理过这件事（answerer.py:97 用 `extend_to_cause`），live 路径漏了。
"""

from __future__ import annotations

from kbqa.planner import Plan


def test_why_question_quote_reaches_the_cause_sentence(docs_engine):
    """引用只挑到“会议决定……下架”不够，要把写着 35% 的原因句一起引上。"""
    question = "S04 为什么不卖吞拿鱼三明治了？"
    plan = Plan(question=question, standalone=question, search_query="S04 吞拿鱼三明治 下架")

    cites = docs_engine._citations(plan, ["KB-029"])

    assert [c["doc_id"] for c in cites] == ["KB-029"]
    assert "35" in cites[0]["quote"]


def test_why_question_quote_stays_within_the_contract_bound(docs_engine):
    """扩引不能把整篇文档倒出来：契约 §5 限 400 字，扩出来还得逐字可核对。"""
    question = "S04 为什么不卖吞拿鱼三明治了？"
    plan = Plan(question=question, standalone=question, search_query="S04 吞拿鱼三明治 下架")

    cites = docs_engine._citations(plan, ["KB-029"])

    quote = cites[0]["quote"]
    assert len(quote) <= 400
    assert docs_engine.answerer.facts.verbatim("KB-029", quote)


def test_non_why_question_does_not_get_extended(docs_engine):
    """不问为什么就不扩引：C01 问“多久”，引的就该是写着 24 小时的那一句。

    这条是"别改坏"的护栏——扩引只在问原因时做，别的问句引什么由相关度决定。
    """
    question = "外卖订单多久内可以申请退款？"
    plan = Plan(question=question, standalone=question, search_query="外卖订单 退款 时限")

    cites = docs_engine._citations(plan, ["KB-013"])

    assert [c["doc_id"] for c in cites] == ["KB-013"]
    assert "24" in cites[0]["quote"]
