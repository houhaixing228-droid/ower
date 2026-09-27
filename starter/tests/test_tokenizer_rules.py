"""分词与检索的回归测试。

现象：几乎每道检索题的 top-5 都恰好是索引里排在最前面的那几篇
（KB-001/002/003/020/021），顺序还与文档顺序一致——这说明 BM25 一个词都没命中，
结果全靠“凑满 top_k”的补位逻辑填上来的。
"""

from __future__ import annotations

import pytest

from kbqa.tokenizer import TOKENIZER_VERSION, tokenize


def test_chinese_text_is_split_into_terms():
    """`tokenize` 只按空白切词，中文整句会变成一个 token。

    “外卖订单多久内可以申请退款”作为整体不可能出现在任何文档里，
    于是任何中文查询都命中不了任何片段。
    """
    terms = tokenize("外卖订单多久内可以申请退款")
    assert len(terms) > 3, "整句被当成了一个词：%r" % (terms,)


def test_shared_words_match_across_sentences():
    """同一件事换个说法，两边的词项要有交集。"""
    left = set(tokenize("外卖订单多久内可以申请退款"))
    right = set(tokenize("退款政策 v2 外卖订单在订单送达后 24 小时内可以申请退款"))
    assert left & right, "两句话的公共词为空"


def test_version_bumped_to_invalidate_cached_index():
    """分词规则变了，旧的缓存索引必须失效，否则线上还是老页数。"""
    assert TOKENIZER_VERSION != "tokenizer-2"


def test_latin_and_digits_are_kept():
    terms = tokenize("KB-013 退款政策 v2")
    joined = " ".join(terms)
    assert "013" in joined or "KB-013" in joined
    assert any("退款" in term for term in terms)


@pytest.mark.parametrize(
    "left,right",
    [
        ("营业时间", "营业时间调整通知"),
        ("退款", "退货退款"),
    ],
)
def test_substring_queries_still_match(left, right):
    assert set(tokenize(left)) & set(tokenize(right))
