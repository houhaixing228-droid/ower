"""run_sql 的两条硬规矩。

第四轮 H04 拿 0 分，trace 里的真实原因是 "no such table: clean_orders"：
模型用 run_sql 自己编了个表名，一个工具错误直接把整条回答炸成 refusal。
工具失败应该是"告诉模型查错了、让它换一个查法"，不是终止回答。

顺带发现第二件事：run_sql 跑完要 commit，而它并不拦写入语句——
也就是说模型（或被注入的模型）生成的 DELETE 会被真的执行掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kbqa.cleaning import build_clean_db
from kbqa.tools import DataTools


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    source = root / "data" / "pos.db"
    if not source.exists():
        pytest.skip("找不到 data/pos.db")
    db = tmp_path_factory.mktemp("var") / "clean.db"
    build_clean_db(source, db)
    return DataTools(db)


def test_run_sql_rejects_write_statements(tools: DataTools):
    for sql in (
        "DELETE FROM sales_clean",
        "DROP TABLE sales_clean",
        "UPDATE sales_clean SET amount_cents = 0",
        "INSERT INTO sales_clean (date) VALUES ('2026-08-01')",
    ):
        result = tools.run_sql(sql)
        assert "error" in result, sql


def test_run_sql_rejects_write_hidden_after_semicolon(tools: DataTools):
    """SELECT 后面挂个分号再跟 DELETE，不能算 SELECT。"""
    result = tools.run_sql("SELECT 1; DELETE FROM sales_clean")
    assert "error" in result


def test_run_sql_accepts_select(tools: DataTools):
    result = tools.run_sql("SELECT COUNT(*) AS n FROM sales_clean")
    assert "error" not in result
    assert result["rows"][0]["n"] > 0


def test_run_sql_unknown_table_is_an_error_not_a_crash(tools: DataTools):
    """表名写错时要给出能用的提示，让模型换一个查法，而不是抛异常。"""
    result = tools.run_sql("SELECT * FROM clean_orders")
    assert "error" in result
    # 提示里要点名真实存在的表，否则模型只能继续瞎猜
    assert "sales_clean" in result["error"]


@pytest.mark.parametrize("prefix", ["WITH", "with"])
def test_run_sql_allows_cte(tools: DataTools, prefix):
    sql = "%s t AS (SELECT COUNT(*) AS n FROM sales_clean) SELECT n FROM t" % prefix
    result = tools.run_sql(sql)
    assert "error" not in result
