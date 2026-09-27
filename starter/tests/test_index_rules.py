"""索引缓存、过滤顺序、top-k 契约的回归测试。

前同事把 `.cache/index.json` 提交进了仓库，而缓存键只哈希版本号、不哈希知识库内容：
换掉 `knowledge_base/` 之后 `rebuild` 会直接复用旧索引，词条还是上一份文档的。
"""

from __future__ import annotations

import pytest

from kbqa.index import build_index, content_key, load_index
from kbqa.retriever import Retriever

KB_DIR = pytest.importorskip("pathlib").Path(__file__).resolve().parents[2] / "knowledge_base"


@pytest.fixture(scope="module")
def kb_dir():
    if not KB_DIR.exists():
        pytest.skip("找不到 knowledge_base/")
    return KB_DIR


def test_content_key_reacts_to_kb_content(tmp_path, kb_dir):
    """缓存键必须把知识库内容算进去，否则换了文档还读旧索引。"""
    mirror = tmp_path / "kb"
    mirror.mkdir()
    (mirror / "KB-900_test.md").write_text("初版内容：退款时限是 24 小时。", encoding="utf-8")
    first = content_key(mirror)
    (mirror / "KB-900_test.md").write_text("改动后的内容：退款时限改成 12 小时。", encoding="utf-8")
    second = content_key(mirror)
    assert first != second


def test_content_key_ignores_nothing_that_matters(tmp_path, kb_dir):
    """新增、删除、改名都要让键变化，评审时会直接替换整个目录。"""
    mirror = tmp_path / "kb2"
    mirror.mkdir()
    (mirror / "KB-901_a.md").write_text("alpha", encoding="utf-8")
    before = content_key(mirror)
    (mirror / "KB-902_b.md").write_text("beta", encoding="utf-8")
    assert content_key(mirror) != before


def test_stale_cache_is_not_reused(tmp_path, kb_dir):
    """缓存存在但内容对不上时必须重建，不能图快直接返回旧索引。"""
    cache = tmp_path / "cache" / "index.json"
    index = load_index(kb_dir, cache)
    assert len(index.docs_meta) == 35
    # 手动把缓存写成一份“别人的”内容，再换个目录加载
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "KB-800_new.md").write_text("这是一份全新的文档，讲会员储值。", encoding="utf-8")
    index2 = load_index(mirror, cache)
    assert sorted(index2.docs_meta) == ["KB-800"]


def test_retrieve_returns_exactly_top_k(kb_dir):
    index = build_index(kb_dir)
    retriever = Retriever(index, __import__("datetime").date(2026, 9, 1))
    for query in ("外卖订单多久内可以申请退款", "发票怎么开", "员工餐", "0000zzzz"):
        result = retriever.search(query, top_k=5)
        assert len(result.hits) == 5, "%s 只返回了 %d 条" % (query, len(result.hits))


def test_hit_doc_id_matches_its_chunk(kb_dir):
    """要把片段归属于它真正来自的那篇文档，否则引用会对不上人。"""
    index = build_index(kb_dir)
    retriever = Retriever(index, __import__("datetime").date(2026, 9, 1))
    result = retriever.search("退款", top_k=5)
    for hit in result.hits:
        assert hit.chunk_id.startswith(hit.doc_id), hit
        owner = index.chunks[[c.chunk_id for c in index.chunks].index(hit.chunk_id)].doc_id
        assert hit.doc_id == owner, "%s 被错标成了 %s" % (owner, hit.doc_id)


def test_superseded_version_yields_the_slot(kb_dir):
    """元数据过滤要在取 top-k 之前生效：占了格子再删，等于把位置浪费给废止版本。"""
    index = build_index(kb_dir)
    retriever = Retriever(index, __import__("datetime").date(2026, 9, 1))
    result = retriever.search("外卖订单多久内可以申请退款", top_k=5)
    ids = [hit.doc_id for hit in result.hits]
    assert "KB-012" not in ids, "已废止的 v1 退款政策不该占位：%s" % ids
    assert "KB-013" in ids, ids


def test_scores_are_descending(kb_dir):
    index = build_index(kb_dir)
    retriever = Retriever(index, __import__("datetime").date(2026, 9, 1))
    result = retriever.search("会员储值现在充值 500 送多少", top_k=5)
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True), scores
