"""问数值类问题、模型又没标出处时的补引用。

第六轮 T02 第 3 轮"供应商后来赔了多少"：模型读的其实是 KB-022（供应商邮件，
里面写着 CNY 8,600），但没写编号；引用为空 → KB-022 的正文不在 allowed numbers 里
→ 数字核对把写着 8,600 的那句整句删了，最后只剩半句话，answer_type 掉成 refusal。
分丢了两次：一次是没引上出处，一次是因此把自己的数字删了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kbqa.cleaning import build_clean_db
from kbqa.live import LiveEngine, _retrieved_doc_ids
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
