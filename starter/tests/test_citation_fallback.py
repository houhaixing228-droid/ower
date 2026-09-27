"""问数值类问题、模型又没标出处时的补引用。

第六轮 T02 第 3 轮"供应商后来赔了多少"：模型读的其实是 KB-022（供应商邮件，
里面写着 CNY 8,600），但没写编号；引用为空 → KB-022 的正文不在 allowed numbers 里
→ 数字核对把写着 8,600 的那句整句删了，最后只剩半句话，answer_type 掉成 refusal。
分丢了两次：一次是没引上出处，一次是因此把自己的数字删了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbqa.cleaning import build_clean_db
from kbqa.live import LiveEngine, _retrieved_doc_ids
from kbqa.planner import Plan
from kbqa.tools import DataTools


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    from kbqa.config import load_settings
    from kbqa.docfacts import DocFacts
    from kbqa.entities import Catalog
    from kbqa.index import load_index
    from kbqa.retriever import Retriever

    settings = load_settings()
    source = Path(__file__).resolve().parents[2] / "data" / "pos.db"
    if not source.exists():
        pytest.skip("找不到 data/pos.db")
    db = tmp_path_factory.mktemp("var") / "clean.db"
    build_clean_db(source, db)
    tools = DataTools(db)
    index = load_index(settings.kb_dir, settings.index_path)
    retriever = Retriever(index, settings.today)
    catalog = Catalog(stores=tools.stores(), products=tools.products(), aliases=index.aliases)

    class _Answerer:
        pass

    answerer = _Answerer()
    answerer.retriever = retriever
    answerer.facts = DocFacts(index)
    answerer.catalog = catalog

    built = LiveEngine.__new__(LiveEngine)
    built.answerer = answerer
    built.today = settings.today.isoformat()
    return built


def test_retrieved_doc_ids_keeps_order_and_dedupes():
    retrieved = {
        "a": [{"doc_id": "KB-029"}, {"doc_id": "KB-022"}],
        "b": [{"doc_id": "KB-022"}, {"doc_id": "KB-021"}],
    }
    assert _retrieved_doc_ids(retrieved) == ["KB-029", "KB-022", "KB-021"]
    assert _retrieved_doc_ids(None) == []


def test_value_citation_prefers_doc_that_has_the_number(engine):
    """只在 KB-029 和 KB-022 里挑，问"赔了多少"要挑出真写着金额的那篇。"""
    doc = engine._value_citation_doc(["供应商后来赔了多少？"], ["KB-029", "KB-022"])
    assert doc == "KB-022"


def test_citations_swap_in_the_doc_that_holds_the_amount(engine):
    """追问里模型没标出处、继承来的又是停售通知时，要把真写着金额的那篇换进来。

    T02 第 3 轮完整链路：上一轮引的是 KB-021（停售通知），里面只有一句
    "详见供应商邮件 KB-022"，金额不在这里；模型这一轮又没标出处。
    换不过来，引用就落在 KB-021 上，`_allowed_numbers` 里没有 8,600，
    写着金额的那句会被当成幻觉删掉，整题掉成 refusal。
    """
    question = "供应商后来赔了多少？"
    plan = Plan(question=question, standalone=question, search_query=question)
    history = [
        {"question": "三文鱼poke 七月初为什么停售了？", "standalone": "三文鱼poke 七月初为什么停售了？",
         "citations": [{"doc_id": "KB-021", "quote": "x"}]},
        {"question": "那停售期间让顾客换成什么？", "standalone": "那停售期间让顾客换成什么？",
         "citations": [{"doc_id": "KB-021", "quote": "x"}]},
    ]
    retrieved = {"c1": [{"doc_id": "KB-021"}, {"doc_id": "KB-029"}, {"doc_id": "KB-022"}]}

    cites = engine._citations(plan, [], history=history, retrieved=retrieved)

    assert [c["doc_id"] for c in cites] == ["KB-022"]
    assert "8,600" in cites[0]["quote"]


def test_value_citation_locates_the_orphan_number(engine):
    """按“这个数在哪篇文档里”定位出处。

    上面那条测试喂的候选只有三篇，KB-022 排在前面，所以“第一个写着金额的”就够了。
    真实链路不是这样：模型这一轮检索了 4 次，KB-022 是它第 4 次自己写的
    "Tasman 冷链 冷藏机组故障 赔付 CNY" 里排第 5 的结果，在 _retrieved_doc_ids
    的顺序里落到第 11 位。而 KB-027（POS 故障报告，写着"常备零钱 500 元提高到
    1,500 元"）排在它前面——“第一个写着金额的”会挑成 KB-027，照样对不上 8600。

    所以要认的不是“哪篇有金额”，是“模型写出来的这个 8600 在哪篇里”。
    """
    doc = engine._value_citation_doc(
        ["供应商后来赔了多少？"],
        [
            "KB-021", "KB-029", "KB-041", "KB-040", "KB-003", "KB-031",
            "KB-015", "KB-001", "KB-033", "KB-027", "KB-022",
        ],
        wanted=[7.0, 4.0, 8600.0],
    )
    assert doc == "KB-022"


def test_citations_use_the_answer_number_to_place_the_quote(engine):
    """端到端按真实检索顺序走一遍：引用要落到 KB-022，quote 里要有 8,600。"""
    question = "供应商后来赔了多少？"
    plan = Plan(question=question, standalone=question, search_query=question)
    history = [
        {"question": "那停售期间让顾客换成什么？", "standalone": "那停售期间让顾客换成什么？",
         "citations": [{"doc_id": "KB-021", "quote": "x"}]},
    ]
    retrieved = {
        json.dumps({"query": "三文鱼 冷链 供应商 赔付 金额", "top_k": 5}): [
            {"doc_id": "KB-021"}, {"doc_id": "KB-029"}, {"doc_id": "KB-041"},
            {"doc_id": "KB-040"}, {"doc_id": "KB-003"},
        ],
        json.dumps({"query": "Tasman 冷链 冷藏机组故障 赔付 CNY", "top_k": 8}): [
            {"doc_id": "KB-041"}, {"doc_id": "KB-033"}, {"doc_id": "KB-015"},
            {"doc_id": "KB-027"}, {"doc_id": "KB-022"},
        ],
    }
    answer = "供应商已按书面方案赔付到位，7 月 4 日那批三文鱼已销毁，赔偿金额为 8600 元。"

    cites = engine._citations(
        plan, [], history=history, retrieved=retrieved, answer=answer
    )

    assert [c["doc_id"] for c in cites] == ["KB-022"]
    assert "8,600" in cites[0]["quote"]


def test_value_doc_is_marked_even_when_the_model_already_cited_it(engine):
    """模型自己引对了 KB-022，照样会丢掉——这一条就是 round8 T02 第 3 轮。

    前面几条测试都在修"没引上出处"，修完之后仍然失分，因为真实链路是另一种：
    模型这次**引对了**（正文里写了 [KB-022]），于是 `doc_ids` 里已经有 KB-022，
    `_value_citation_doc` 那一步就不再触发（它只在"现有文档里没有金额句"时才换）。

    问题是 KB-022 是一封英文邮件，而问题问的是中文"供应商后来赔了多少"，
    `facts.rank` 两种模式在这个组合下都挑不出任何句子：

        rank("供应商后来赔了多少？", "KB-022", require_value=True)  -> []
        rank("供应商后来赔了多少？", "KB-022", require_value=False) -> []

    而 `allow_value_fallback` 只在文档是"被换进来的"（value_doc）时才打开，
    于是 KB-022 被静默丢掉，引用只剩 KB-021 和 KB-029。8600 随之不在
    `_allowed_numbers` 里 → 判成幻觉 → 重写 → 重写又写出一段元评论 → 题就这么丢了。

    所以：**只要模型写出来的数确实出自这一篇，就该给它 value 兜底**，
    跟"是不是刚刚换进来的"无关。
    """
    question = "供应商后来赔了多少？"
    plan = Plan(question=question, standalone=question, search_query=question)
    retrieved = {"c": [{"doc_id": "KB-021"}, {"doc_id": "KB-029"}, {"doc_id": "KB-022"}]}
    answer = "供应商已开具 8600 元的贷项通知单，冲抵整批被拒收货值。"

    cites = engine._citations(
        plan,
        ["KB-021", "KB-029", "KB-022"],
        retrieved=retrieved,
        answer=answer,
    )

    by_doc = {c["doc_id"]: c["quote"] for c in cites}
    assert "KB-022" in by_doc, "引对了 KB-022，却因为挑不出句子被丢掉了"
    assert "8,600" in by_doc["KB-022"]
