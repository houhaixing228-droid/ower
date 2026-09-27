"""把文档切成检索用的小块。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .loader import Document

#: 切块参数变了，索引缓存必须失效，所以写进缓存键里。
CHUNKER_VERSION = "chunker-3"

CHUNK_SIZE = 300
#: 相邻片段之间留一点重叠，句子跨块时才不至于两边都不完整。
CHUNK_OVERLAP = 60

#: 优先级大于它的边界都算“可以断的地方”：句号、换行、列表符号。
_BREAKERS = re.compile(r"(?<=[。！？；\n])|(?<=[^0-9A-Za-z])(?=[-*•])")


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str
    source_text: str
    heading: str = ""
    kind: str = "text"
    table_header: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "source_text": self.source_text,
            "heading": self.heading,
            "kind": self.kind,
            "table_header": self.table_header,
        }


def _cut_boundary(text: str, start: int, limit: int) -> int:
    """在 `[start+limit//2, start+limit]` 里找最后一个可断处，找不到就硬切。"""
    window = text[start + limit // 2 : start + limit]
    cuts = list(_BREAKERS.finditer(window))
    return start + limit if not cuts else start + limit // 2 + cuts[-1].end()


def chunk_document(document: Document) -> list[Chunk]:
    """一篇文档切成 300 字左右的块，允许少量重叠。

    原来的写法用了 `range(0, len(text) - CHUNK_SIZE, CHUNK_SIZE)`：
    整数除法会让最后不足一块的那段直接不被切出来，每篇文档尾巴上几十到
    近三百字（KB-052 丢了 298/598，接近一半）从来没进过索引。
    """
    text = document.text.strip() or document.title
    chunks: list[Chunk] = []
    start = 0
    number = 0
    while start < len(text):
        number += 1
        end = min(len(text), start + CHUNK_SIZE)
        if end < len(text):
            end = max(start + CHUNK_SIZE // 2, _cut_boundary(text, start, CHUNK_SIZE))
        piece = text[start:end].strip()
        if piece:
            chunks.append(
                Chunk(
                    doc_id=document.doc_id,
                    chunk_id="%s#%d" % (document.doc_id, number),
                    text=piece,
                    source_text=piece,
                    heading=document.title,
                )
            )
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    if not chunks:
        piece = text or document.title
        chunks.append(
            Chunk(
                doc_id=document.doc_id,
                chunk_id="%s#1" % document.doc_id,
                text=piece,
                source_text=piece,
                heading=document.title,
            )
        )
    return chunks


def chunk_documents(documents: list[Document]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    return chunks
