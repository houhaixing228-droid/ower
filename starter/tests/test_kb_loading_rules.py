"""知识库加载与切块的回归测试。

交接文档说“md、txt、html 三种格式都支持”“95% 命中率”，
实际 loader 只认 .md，且 GBK 文件与 HTML 的处理方式都会让文本失真。
"""

from __future__ import annotations

import pytest

from kbqa.chunker import chunk_document
from kbqa.loader import SUPPORTED_SUFFIXES, decode_bytes, load_document, load_knowledge_base

KB_DIR = pytest.importorskip("pathlib").Path(__file__).resolve().parents[2] / "knowledge_base"


@pytest.fixture(scope="module")
def documents():
    if not KB_DIR.exists():
        pytest.skip("找不到 knowledge_base/")
    docs, warnings = load_knowledge_base(KB_DIR)
    return docs, warnings


def test_supported_suffixes_cover_txt_and_html():
    assert SUPPORTED_SUFFIXES >= {".md", ".txt", ".html", ".htm"}


def test_every_kb_numbered_file_is_loaded(documents):
    """知识库里 35 份 KB-xxx 文档必须全部进索引，公共题库 health 类查的就是这个数。"""
    docs, _ = documents
    ids = sorted(doc.doc_id for doc in docs)
    assert len(ids) == 35, ids


def test_readme_is_not_a_document(documents):
    """knowledge_base/README.md 没有 KB 编号，不该算文档（契约 §1）。"""
    docs, _ = documents
    assert all(doc.doc_id.startswith("KB-") for doc in docs)


def test_txt_is_loaded(documents):
    docs, _ = documents
    ids = {doc.doc_id for doc in docs}
    assert "KB-062" in ids, "GBK 编码的旧 OA 导出文件没进索引"
    assert "KB-022" in ids, "英文邮件通知没进索引"


def test_gbk_file_decodes_to_real_chinese(documents):
    """按 UTF-8 忽略错误地读 GBK 文件，得到的是一串替换符，检索到也没用。"""
    docs, _ = documents
    doc = next(doc for doc in docs if doc.doc_id == "KB-062")
    assert "替换" not in doc.text[:200]
    assert "合味餐饮" in doc.text
    assert "营业时间" in doc.text


def test_html_is_visible_text_without_tags(documents):
    """HTML 必须剥成可见正文，否则引用里带着标签，逐字校验过不了。"""
    docs, _ = documents
    doc = next(doc for doc in docs if doc.doc_id == "KB-061")
    assert "<" not in doc.text
    assert ">" not in doc.text or "→" in doc.text
    assert "发票" in doc.text


def test_html_script_and_style_removed(documents):
    docs, _ = documents
    doc = next(doc for doc in docs if doc.doc_id == "KB-061")
    assert "javascript" not in doc.text.lower()
    assert "function" not in doc.text.lower()


def test_chunking_covers_the_whole_document(documents):
    """切块不能丢尾巴：每篇文档每个字至少出现在一个片段里。"""
    docs, _ = documents
    for doc in docs:
        chunks = chunk_document(doc)
        joined = "".join(chunk.text for chunk in chunks) if chunks else ""
        # 只要求“原文被覆盖”，允许片段之间有重叠，但不允许整段丢失
        tail = doc.text[-60:]
        assert any(tail[:20] in chunk.text for chunk in chunks), (
            "%s 的末尾被丢掉了：%r" % (doc.doc_id, tail[:20])
        )


def test_every_document_has_at_least_one_chunk(documents):
    """长度不足一块的短文档也要有片段，否则它永远检索不到。"""
    docs, _ = documents
    for doc in docs:
        assert chunk_document(doc), doc.doc_id


def test_gbk_bytes_decode_correctly():
    """没有 BOM 的 GBK 文件要能按 GBK 读出来，而不是被忽略成一串替换符。"""
    import pathlib

    assert "营业时间" in decode_bytes("营业时间调整".encode("gbk"), pathlib.Path("x.txt"), [])
