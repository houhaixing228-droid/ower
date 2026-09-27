"""KB-001 §4 的指标口径回归测试。

期望值取公开题库 `metrics` 类的同源口径（M01–M04），不是从当前实现抄来的。
当前实现把退款行整排剔除、行数当订单数、区间右端点开区间，全部会对不上。
"""

from __future__ import annotations

import pytest

from kbqa.cleaning import build_clean_db
from kbqa.tools import DataTools


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = root / "data" / "pos.db"
    if not source.exists():
        pytest.skip("找不到 data/pos.db")
    db = tmp_path_factory.mktemp("var") / "clean.db"
    build_clean_db(source, db)
    return DataTools(db)


def test_june_net_revenue_includes_refunds(tools):
    """KB-001 §4：净营业额 = 销售行 + 退款行（退款是负数，实际效果是减）。"""
    got = tools.query_metrics("2026-06-01", "2026-06-30")
    assert got["net_revenue"] == pytest.approx(156757.00, abs=0.01)


def test_june_refund_amount_is_positive_abs(tools):
    got = tools.query_metrics("2026-06-01", "2026-06-30")
    assert got["refund_amount"] == pytest.approx(953.00, abs=0.01)


def test_orders_counts_distinct_order_ids(tools):
    """KB-001 §4：有效订单数是销售行里不同 order_id 的个数，多行订单算 1 单。"""
    got = tools.query_metrics("2026-06-01", "2026-06-30")
    assert got["orders"] == 4311


def test_aov_divides_by_orders_not_rows(tools):
    got = tools.query_metrics("2026-06-01", "2026-06-30")
    assert got["aov"] == pytest.approx(36.36, abs=0.01)


def test_qty_subtracts_refunded_units(tools):
    """KB-001 §4：销量 = 销售行数量 - 退款行数量。"""
    got = tools.query_metrics("2026-06-01", "2026-06-30")
    assert got["qty"] == 6496


def test_store_filter(tools):
    got = tools.query_metrics("2026-07-01", "2026-07-31", store_id="S02")
    assert got["net_revenue"] == pytest.approx(41740.00, abs=0.01)
    assert got["refund_amount"] == pytest.approx(107.00, abs=0.01)
    assert got["orders"] == 875
    assert got["aov"] == pytest.approx(47.70, abs=0.01)
    assert got["qty"] == 1395


def test_product_filter(tools):
    got = tools.query_metrics("2026-08-01", "2026-08-31", product_id="P21")
    assert got["net_revenue"] == pytest.approx(11024.00, abs=0.01)
    assert got["orders"] == 461
    assert got["aov"] == pytest.approx(23.91, abs=0.01)
    assert got["qty"] == 689


def test_single_day_is_inclusive_of_end(tools):
    """契约 §2：start/end 是闭区间。右端点开区间会把这一天算没了。"""
    got = tools.query_metrics("2026-06-18", "2026-06-18", store_id="S02", product_id="P06")
    assert got["orders"] == 53
    assert got["net_revenue"] == pytest.approx(3625.00, abs=0.01)
    assert got["aov"] == pytest.approx(68.40, abs=0.01)
    assert got["qty"] == 125


def test_store_id_case_and_space_are_tolerated(tools):
    """门店编号大小写、空格是 KB-001 §2.1 说的可恢复写法。"""
    got = tools.query_metrics("2026-07-01", "2026-07-31", store_id=" s02 ")
    assert got["orders"] == 875


def test_empty_range_returns_zero_without_error(tools):
    got = tools.query_metrics("2025-01-01", "2025-01-31")
    assert got["net_revenue"] == 0
    assert got["orders"] == 0
    assert got["aov"] is None


def test_daily_has_every_day_and_values_match_summary(tools):
    got = tools.daily_metrics("2026-08-01", "2026-08-05")
    assert [day["date"] for day in got["days"]] == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
    ]
    summary = tools.query_metrics("2026-08-01", "2026-08-05")
    assert sum(day["orders"] for day in got["days"]) == summary["orders"]
    assert sum(day["net_revenue"] for day in got["days"]) == pytest.approx(
        summary["net_revenue"], abs=0.01
    )


def test_daily_end_day_is_included(tools):
    """M06：闭区间的最后一天必须有数。"""
    days = tools.daily_metrics("2026-08-29", "2026-08-31")["days"]
    last = [day for day in days if day["date"] == "2026-08-31"][0]
    one = tools.query_metrics("2026-08-31", "2026-08-31")
    assert last["orders"] == one["orders"]
    assert last["orders"] > 0
