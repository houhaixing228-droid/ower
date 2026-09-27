"""数字核对的允许清单要带上"上一轮自己说过的数"，删句也不能把回答删空。

第七轮全量评测暴露的两个系统性问题（T01 第 2 轮、H03）：

1. T01 第 2 轮"那 7 月呢？"，trace 里 number_check_failed 的 unmatched 是
   [6.0, 156757.0, 5657.0, 3.6] —— 156757 是**上一轮它自己答过的 6 月净营业额**
   （那一轮已经过了核对），模型拿它做对比，却被当成幻觉。
2. 重写没救回来时 `_drop_sentences` 把含这些数的句子整句删掉，
   结果是整段回答被删空，只剩一句占位符——本来 162414 是对的，一起没了。

两处都要修：上一轮的数字本来就核对过，属于可信上下文；删到什么都不剩时
宁可保留原答案，也不能把回答清空（清空之后所有数字检查都必然失败）。
"""

from __future__ import annotations

from kbqa.planner import Plan


def test_previous_turn_numbers_are_allowed(docs_engine):
    """上一轮答过的数，这一轮拿来对比不该被判成幻觉。"""
    plan = Plan(question="那 7 月呢？", standalone="7 月的净营业额", search_query="那 7 月呢？")

    allowed = docs_engine._allowed_numbers(plan, [], [], context=[156757.0])

    assert any(abs(value - 156757.0) <= 0.011 for value in allowed)


def test_drop_sentences_never_empties_the_answer(docs_engine):
    """每一句都含不可核对数字时，保留原答案，而不是删成占位符。"""
    text = "7 月净营业额 162414.00 元，比 6 月的 156757.00 元高。"

    kept = docs_engine._drop_sentences(text, [156757.0])

    assert "162414" in kept
    assert "已略去" not in kept


def test_drop_sentences_still_drops_when_something_remains(docs_engine):
    """还有干净的句子时，照旧删掉带幻觉数的那句。"""
    text = "7 月净营业额 162414.00 元。上一轮说的 999999 元是错的。"

    kept = docs_engine._drop_sentences(text, [999999.0])

    assert "162414" in kept
    assert "999999" not in kept
