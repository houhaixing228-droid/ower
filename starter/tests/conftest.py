"""测试夹具。

原来的写法在 session 级把 `Retriever.search` 换成假实现，并且从不还原：
这个替身会污染同一次 pytest 会话里后续的所有测试，
于是任何关于真实检索的断言都不可能失败——这也是“自带测试全绿”的一部分原因。

这里改成用 `monkeypatch` 逐测试打补丁，pytest 会在每个用例结束后自动还原。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE_TEXT = "退款政策 v2 > 三、时限：外卖订单在订单送达后 24 小时内可以申请退款。"


@pytest.fixture()
def client(monkeypatch, tmp_path_factory):
    os.environ["VAR_DIR"] = str(tmp_path_factory.mktemp("var"))
    # 显式置空而不是删除：`load_dot_env` 只补环境里没有的项，
    # 本机 .env 里有 Key 时，删掉变量反而会让 .env 生效，测试就跑成 live 了。
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        os.environ[key] = ""

    from fastapi.testclient import TestClient

    from kbqa import retriever as retriever_module
    from kbqa import server

    def fake_search(self, query, top_k=5, **kwargs):
        hit = retriever_module.Hit(
            doc_id="KB-013",
            chunk_id="KB-013#1",
            score=42.0,
            text=FAKE_TEXT,
            source_text=FAKE_TEXT,
            meta={"title": "退款政策 v2", "status": "现行"},
        )
        return retriever_module.SearchResult(
            hits=[hit][:top_k],
            query=query,
            terms=[],
            expansions=[],
            filtered=[],
            coverage=1.0,
        )

    monkeypatch.setattr(retriever_module.Retriever, "search", fake_search)
    return TestClient(server.app)
