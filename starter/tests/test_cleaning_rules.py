"""KB-001（现行口径手册）的清洗规则回归测试。

交接文档说“测试全是绿的”，但那组测试只验了接口能返回 200。
这里把 KB-001 §2/§3 逐条写成断言：清洗必须真的剔除行、真的规范化。
"""

from __future__ import annotations

import sqlite3

from kbqa.cleaning import build_clean_db, clean_rows, parse_amount, parse_date, parse_qty

STORES = {"S01", "S02"}
PRODUCTS = {"P01", "P02"}


def rows_from(specs: list[dict]) -> list[sqlite3.Row]:
    """把 dict 变成 sqlite3.Row，字段和 pos.db 的 sales 表一致。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sales (order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,"
        " qty TEXT, amount TEXT, payment TEXT)"
    )
    for spec in specs:
        conn.execute(
            "INSERT INTO sales VALUES (:order_id,:date,:store_id,:product_id,:qty,:amount,:payment)",
            spec,
        )
    return conn.execute("SELECT * FROM sales").fetchall()


def clean(specs: list[dict]):
    return clean_rows(rows_from(specs), STORES, PRODUCTS)


def base(**over) -> dict:
    spec = {
        "order_id": "ORD1",
        "date": "2026-06-01",
        "store_id": "S01",
        "product_id": "P01",
        "qty": "2",
        "amount": "20.00",
        "payment": "微信",
    }
    spec.update(over)
    return spec


# -- §2 规范化 ------------------------------------------------------------------


def test_parse_date_iso():
    assert parse_date("2026-06-01") == "2026-06-01"


def test_parse_date_slash():
    assert parse_date("2026/6/1") == "2026-06-01"
    assert parse_date("2026/06/01") == "2026-06-01"


def test_parse_date_day_first_when_day_over_12():
    """KB-001 §2.2：DD-MM-YYYY 日在前。25-07-2026 是 7 月 25 日。"""
    assert parse_date("25-07-2026") == "2026-07-25"


def test_parse_date_day_first_disambiguates():
    """07-06-2026 按日在前解析成 6 月 7 日，不是 7 月 6 日。"""
    assert parse_date("07-06-2026") == "2026-06-07"


def test_parse_date_rejects_garbage():
    assert parse_date("N/A") is None
    assert parse_date("") is None
    assert parse_date("2026-13-45") is None


def test_parse_amount_strips_currency():
    cents, status = parse_amount("¥38.00")
    assert (cents, status) == (3800, "ok")


def test_parse_amount_empty():
    cents, status = parse_amount("")
    assert cents is None and status == "empty"


def test_parse_qty_ok():
    assert parse_qty("3") == 3


# -- §3 剔除顺序 ----------------------------------------------------------------


def test_unparseable_date_dropped():
    kept, report = clean([base(date="N/A"), base(order_id="ORD2")])
    assert len(kept) == 1
    assert report.removed["1_unparseable_date"] == 1


def test_empty_amount_dropped_not_filled():
    """KB-001 §3.2：空金额直接剔除，且不得用 qty × 建档价回填。"""
    kept, report = clean([base(order_id="ORD1", amount=""), base(order_id="ORD2")])
    assert len(kept) == 1
    assert report.removed["2_empty_amount"] == 1


def test_currency_amount_kept():
    """带 ¥ 的行是可恢复脏值，必须留下并按数值参与统计。"""
    kept, _ = clean([base(amount="¥38.00")])
    assert len(kept) == 1
    assert kept[0][5] == 3800  # amount_cents


def test_qty_le_zero_dropped():
    kept, report = clean([base(order_id="ORD1", qty="0"), base(order_id="ORD2", qty="-1"), base(order_id="ORD3")])
    assert len(kept) == 1
    assert report.removed["3_qty_le_zero"] == 2


def test_dirty_store_fk_dropped_after_normalising():
    """S99 不在维表里要剔除，但 s01 / ' S01 ' 规范化之后是合法的，不能误删。"""
    kept, report = clean(
        [base(order_id="ORD1", store_id="S99"), base(order_id="ORD2", store_id="s01"), base(order_id="ORD3", store_id=" S01 ")]
    )
    assert report.removed["4_store_not_in_stores"] == 1
    assert len(kept) == 2
    assert all(row[2] == "S01" for row in kept)


def test_dirty_product_fk_dropped():
    kept, report = clean([base(order_id="ORD1", product_id="p01"), base(order_id="ORD2", product_id="P99")])
    assert report.removed["5_product_not_in_products"] == 1
    assert kept[0][3] == "P01"


def test_exact_duplicate_dropped():
    kept, report = clean([base(), base()])
    assert len(kept) == 1
    assert report.removed["6_duplicate_row"] == 1


def test_multi_item_order_kept():
    """共用订单号、商品不同的多行是合法多行订单，必须全留。"""
    kept, report = clean(
        [base(product_id="P01", amount="20.00"), base(product_id="P02", amount="30.00")]
    )
    assert len(kept) == 2
    assert report.removed["6_duplicate_row"] == 0


def test_duplicate_check_compares_all_seven_fields():
    """只有七个字段全一致才算重复：日期不同就不算。"""
    kept, report = clean([base(), base(date="2026-06-02")])
    assert len(kept) == 2
    assert report.removed["6_duplicate_row"] == 0


def test_refund_rows_kept_after_cleaning():
    """KB-001 §4：退款行是通过清洗之后 amount<0 的行，不得在清洗阶段剔掉。"""
    kept, report = clean([base(order_id="ORD1"), base(order_id="ORD2", amount="-25.00")])
    assert len(kept) == 2
    assert report.kept_refund_rows == 1
    assert report.kept_sales_rows == 1


# -- 与真实数据的一致性 ---------------------------------------------------------


def test_real_source_has_dirty_rows_that_must_be_removed(tmp_path):
    """真实 pos.db 里一定有脏行：全留下来就是没清洗。"""
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]  # starter/.. = 作业包根
    source = root / "data" / "pos.db"
    if not source.exists():
        return
    report = build_clean_db(source, tmp_path / "clean.db")
    assert report.raw_rows > report.kept_rows, "真实数据里应该有被剔除的脏行"
    assert report.removed["1_unparseable_date"] > 0
    assert report.removed["2_empty_amount"] > 0
    assert report.removed["6_duplicate_row"] > 0
    assert report.kept_rows == report.kept_sales_rows + report.kept_refund_rows
    # 清洗库里的字段必须是规范之后的写法，查询才能直接比字符串
    conn = sqlite3.connect(tmp_path / "clean.db")
    try:
        dates = {row[0] for row in conn.execute("SELECT DISTINCT date FROM sales_clean")}
        assert all(len(d) == 10 and d[4] == "-" for d in dates), dates
        stores = {row[0] for row in conn.execute("SELECT DISTINCT store_id FROM sales_clean")}
        assert stores <= STORES | {"S03", "S04", "S05"}, stores
    finally:
        conn.close()
