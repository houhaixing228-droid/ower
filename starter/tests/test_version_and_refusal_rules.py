"""版本择优与"没有找到"判定的回归测试。

这两条都是拿实测翻车换来的：
- 检索排得对（现行版排第一），但模型还是会把归档的旧版一起引上；
- 模型答"没有找到"时还挂着两条引用，答案类型却是 doc。
"""

from __future__ import annotations

from kbqa.guard import unknown_entity_intent
from kbqa.live import LiveEngine, _NO_RESULT_MARK, _title_key, _wants_value

STORES = ["S01", "S02", "S03", "S04", "S05"]
PRODUCTS = ["P01", "P05", "P06", "P21"]


def test_title_key_ignores_year_and_version():
    assert _title_key("2025 年 618 活动方案") == _title_key("2026 年 618 活动方案")
    assert _title_key("会员储值政策 v1") == _title_key("会员储值政策 v2")
    assert _title_key("退款政策 v2") != _title_key("会员储值政策 v2")


def _engine_with(docs_meta):
    """只为了测 _canonical_doc，所以只喂一个带 docs_meta 的替身。"""

    class _Index:
        pass

    class _Retriever:
        pass

    class _Answerer:
        pass

    index = _Index()
    index.docs_meta = docs_meta
    retriever = _Retriever()
    retriever.index = index
    answerer = _Answerer()
    answerer.retriever = retriever

    engine = LiveEngine.__new__(LiveEngine)
    engine.answerer = answerer
    engine.today = "2026-09-01"
    return engine


def test_canonical_doc_swaps_archived_for_current():
    engine = _engine_with(
        {
            "KB-023": {"title": "2026 年 618 活动方案", "status": "现行"},
            "KB-024": {"title": "2025 年 618 活动方案", "status": "归档"},
            "KB-010": {"title": "会员储值政策 v1", "status": "已废止", "superseded_by": "KB-011"},
            "KB-011": {"title": "会员储值政策 v2", "status": "现行"},
        }
    )
    # 问"今年"时，归档的 2025 方案要换成 2026 那份
    assert engine._canonical_doc("KB-024", "今年 618 做活动的是哪个商品，活动价多少？") == "KB-023"
    assert engine._canonical_doc("KB-010", "会员现在单笔充值满 500 送多少？") == "KB-011"
    # 已经是现行版的不动
    assert engine._canonical_doc("KB-023", "今年 618 活动价多少？") == "KB-023"
    # 回看过去的问句必须照原样引旧版
    assert engine._canonical_doc("KB-010", "那 6 月的时候呢？") == "KB-010"
    assert engine._canonical_doc("KB-024", "2025 年那次活动是什么商品？") == "KB-024"


def test_superseded_doc_finds_predecessor():
    """追问"那 6 月的时候呢"要引旧版——旧版就是 superseded_by 指向当前版的那一份。"""
    engine = _engine_with(
        {
            "KB-011": {"title": "会员储值政策 v2", "status": "现行"},
            "KB-010": {
                "title": "会员储值政策 v1",
                "status": "已废止",
                "superseded_by": "KB-011",
            },
            "KB-023": {"title": "2026 年 618 活动方案", "status": "现行"},
        }
    )
    assert engine._superseded_doc("KB-011") == "KB-010"
    # 没有前身就还是它自己
    assert engine._superseded_doc("KB-023") == "KB-023"


def test_wants_value_covers_target_questions():
    """问"达标了吗"就是在要数字，挑引用时要优先挑带数字的那句。"""
    assert _wants_value("冷萃乌龙茶上市第一个月的销量达标了吗？")
    assert _wants_value("618 当天卖了多少份？达到目标了吗？")
    assert not _wants_value("最新的排班制度怎么规定的")


def test_unknown_store_code_is_refused():
    refusal = unknown_entity_intent("S06 这家门店的店长是谁？", STORES, PRODUCTS)
    assert refusal is not None
    assert "S06" in refusal
    assert "门店" in refusal


def test_known_codes_pass():
    assert unknown_entity_intent("618 当天 S02 的牛肉poke 卖了多少份？", STORES, PRODUCTS) is None
    assert unknown_entity_intent("P06 六月卖了多少钱？", STORES, PRODUCTS) is None
    assert unknown_entity_intent("会员现在单笔充值满 500 送多少？", STORES, PRODUCTS) is None


def test_unknown_product_code_is_refused():
    assert unknown_entity_intent("P99 卖了多少？", STORES, PRODUCTS) is not None


def test_no_catalog_means_no_guess():
    """拿不到目录就不要瞎拦，宁可放行。"""
    assert unknown_entity_intent("S06 是谁？") is None
    assert unknown_entity_intent("S06 是谁？", [], []) is None


def test_no_result_marks():
    for text in (
        "两边都没有可支撑这一问题的数据，所以不能回答。",
        "知识库里没有找到相关内容。",
        "查不到这家门店的信息。",
    ):
        assert _NO_RESULT_MARK.search(text), text
    # 正常的结论句不能被误判
    for text in ("现行售价是 45 元。", "6 月的净营业额是 13524.00 元。"):
        assert not _NO_RESULT_MARK.search(text), text
