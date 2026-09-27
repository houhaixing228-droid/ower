"""分词。

starter 原来只有一行 `normalise(text).split()`：按空白切词。中文正文里没有空格，
于是“外卖订单多久内可以申请退款”整句变成一个词项，文档侧也是整段一个词项，
BM25 永远匹配不上——任何中文查询的得分为 0，top-k 全靠补位凑出来。
这也是“运营反馈答非所问”的根因。

现在的做法（无外部依赖，纯标准库）：

- 中日韩统一表意文字：按字生成二元组（bigram）。“退款”→「退款」，
  “营业时间”→「营业」「业时」「时间」，不需要词典也能让同义句共享词项。
- 拉丁字母、数字：整词保留并转小写，方便 `KB-013`、`Salmon`、`2026` 这类命中。
- 其余符号当分隔符。
"""

from __future__ import annotations

import unicodedata

#: 分词规则变了，索引缓存必须失效。
TOKENIZER_VERSION = "tokenizer-3"

#: 中文里几乎不携带信息的字。只用在“查询覆盖率”上，索引照常保留全部词。
STOP_CHARS = frozenset("的了吗呢是在有和与及或就都也还把被给对从向于个些这那哪什么怎样如何多少几请帮我你他它可以能要想会一下少吧啊呀们么样过得着为所")
STOP_WORDS = frozenset("the a an of to in is are and or for on at it this that how what".split())

#: 中日韩统一表意文字（含扩展 A 与兼容区）。表意文字之间没有空格，靠 bigram 切。
_CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
)


def normalise(text: str) -> str:
    """全角转半角、统一大小写，比较与分词都走这一层。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _is_word_char(char: str) -> bool:
    return char.isalnum() and not _is_cjk(char)


def tokenize(text: str) -> list[str]:
    """把一段文本变成词项序列。

    CJK 连续片段产出相邻二字组；只有一个字时保留它本身。
    拉丁/数字串作为一个整词。例：

    >>> tokenize("退款政策 v2")
    ['退款', '款政', '政策', 'v2']
    """
    source = normalise(text)
    terms: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if _is_cjk(char):
            end = index + 1
            while end < length and _is_cjk(source[end]):
                end += 1
            run = source[index:end]
            if len(run) == 1:
                terms.append(run)
            else:
                terms.extend(run[position : position + 2] for position in range(len(run) - 1))
            index = end
            continue
        if _is_word_char(char):
            end = index + 1
            while end < length and _is_word_char(source[end]):
                end += 1
            terms.append(source[index:end])
            index = end
            continue
        index += 1
    return terms


def content_tokens(text: str) -> list[str]:
    """去掉虚词之后的查询词，用来算“这个问题被文档覆盖了多少”。"""
    kept = []
    for token in tokenize(text):
        if token in STOP_WORDS:
            continue
        if all(char in STOP_CHARS for char in token):
            continue
        kept.append(token)
    return kept
