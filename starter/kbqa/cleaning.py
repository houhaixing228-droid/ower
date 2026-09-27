"""把原始 sales 导进 var/clean.db，指标都查这张表。

口径完全按 KB-001（指标口径手册 v3，2026-05-01 起现行）实现：

- §2 规范化：门店/商品编号去空白转大写；日期接受三种写法，其中
  `DD-MM-YYYY` 是旧 POS 的“日在前”格式；金额去掉 `¥` 前缀；数量取整。
- §3 剔除按手册给的顺序执行，先剔除的不再计入后面的原因。
- §4 退款行不参与有效订单计数，但金额计入净营业额（负号天然做减法）。

不把门店/商品/支付方式写死在代码里，全部从维表和 data 里读。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

#: 金额里的货币符号与空白 KB-001 §2.3。
_CURRENCY = str.maketrans("", "", "¥￥ \t　,，")

#: `YYYY-MM-DD`，允许不补零，但要求分隔符为 `-`。
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
#: `YYYY/M/D`，斜杠分隔时不可能是“日在前”的旧格式。
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
#: `DD-MM-YYYY`：KB-001 §2.2 明确“日在前、月在后”，所以第一段是日。
_DASH_DATE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")

REMOVAL_REASONS = (
    "1_unparseable_date",
    "2_empty_amount",
    "3_qty_le_zero",
    "4_store_not_in_stores",
    "5_product_not_in_products",
    "6_duplicate_row",
)


def normalise_id(value: Optional[str]) -> str:
    """KB-001 §2.1：去首尾空白并转大写。

    `s01`、`S01 `、` s03` 都是同一个编号，规范化之后是合法值，不能当脏数据扔掉。
    """
    return str(value or "").strip().upper()


def parse_date(value: Optional[str]) -> Optional[str]:
    """把三种日期写法统一成 `YYYY-MM-DD`，解析不出来返回 None。

    KB-001 §2.2：`25-07-2026` 是 2026-07-25，`07-06-2026` 是 2026-06-07。
    第三种的日在前，日大于 12 的样本正好用来验证方向没有搞反。
    """
    text = str(value or "").strip()
    if not text:
        return None
    parts: Optional[tuple] = None
    match = _ISO_DATE.match(text)
    if match:
        parts = (match.group(1), match.group(2), match.group(3))
    if parts is None:
        match = _SLASH_DATE.match(text)
        if match:
            parts = (match.group(1), match.group(2), match.group(3))
    if parts is None:
        match = _DASH_DATE.match(text)
        if match:
            # 日在前、月在后：第一位是日，第二位是月。
            parts = (match.group(3), match.group(2), match.group(1))
    if parts is None:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
    except ValueError:
        return None


def parse_amount(value: Optional[str]) -> tuple[Optional[int], str]:
    """返回 (分, 状态)。状态取值：`ok`、`empty`、`bad`。

    KB-001 §2.3 与 §3.2：`¥38.00` 与 `38.00` 是同一个金额；空金额直接剔除，**不回填**。
    """
    text = (value or "").translate(_CURRENCY)
    if not text:
        return None, "empty"
    try:
        cents = int((Decimal(text) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None, "bad"
    return cents, "ok"


def parse_qty(value: Optional[str]) -> Optional[int]:
    """KB-001 §2.4：按整数解析。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class CleaningReport:
    raw_rows: int = 0
    kept_rows: int = 0
    kept_sales_rows: int = 0
    kept_refund_rows: int = 0
    removed: dict[str, int] = field(default_factory=lambda: {k: 0 for k in REMOVAL_REASONS})
    note_unparseable_amount: int = 0

    def as_dict(self) -> dict:
        return {
            "raw_rows": self.raw_rows,
            "removed": dict(self.removed, note_unparseable_amount=self.note_unparseable_amount),
            "kept_rows": self.kept_rows,
            "kept_sales_rows": self.kept_sales_rows,
            "kept_refund_rows": self.kept_refund_rows,
        }


def open_readonly(path: Path) -> sqlite3.Connection:
    """打开数据库。"""
    conn = sqlite3.connect(path.as_posix(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def clean_rows(
    rows: Iterable[sqlite3.Row],
    known_stores: Optional[Iterable[str]] = None,
    known_products: Optional[Iterable[str]] = None,
) -> tuple[list[tuple], CleaningReport]:
    """按 KB-001 §3 的顺序清洗明细行。

    返回 `(保留行, 清洗台账)`。保留行的字段顺序与 `sales_clean` 一致；
    金额为 None（空）的行在此之前已被剔除，绝不会留下来当成 0 参与统计。
    """
    stores = {normalise_id(s) for s in (known_stores or ())}
    products = {normalise_id(p) for p in (known_products or ())}
    report = CleaningReport()
    kept: list[tuple] = []
    seen: set[tuple] = set()

    for row in rows:
        report.raw_rows += 1

        # §2.2 日期
        day = parse_date(row["date"])
        if day is None:
            report.removed["1_unparseable_date"] += 1
            continue

        # §2.3 金额：空的不回填，直接剔除
        cents, status = parse_amount(row["amount"])
        if status == "empty":
            report.removed["2_empty_amount"] += 1
            continue
        if status == "bad":
            # 非空但解析不出来：没有可用的金额，按无法恢复处理并单列一笔。
            report.removed["2_empty_amount"] += 1
            report.note_unparseable_amount += 1
            continue

        # §2.4 数量
        qty = parse_qty(row["qty"])
        if qty is None or qty <= 0:
            report.removed["3_qty_le_zero"] += 1
            continue

        # §2.1 编号规范化之后再判断脏外键，顺序反了会误删真实订单
        store_id = normalise_id(row["store_id"])
        product_id = normalise_id(row["product_id"])

        if stores and store_id not in stores:
            report.removed["4_store_not_in_stores"] += 1
            continue
        if products and product_id not in products:
            report.removed["5_product_not_in_products"] += 1
            continue

        record = (
            (row["order_id"] or "").strip(),
            day,
            store_id,
            product_id,
            qty,
            int(cents),
            (row["payment"] or "").strip(),
        )
        # §3.6 只有七个字段全一致才算重复行；共用订单号但商品不同的多行订单必须全留。
        if record in seen:
            report.removed["6_duplicate_row"] += 1
            continue
        seen.add(record)

        kept.append(record + (1 if cents < 0 else 0,))

    report.kept_rows = len(kept)
    report.kept_refund_rows = sum(1 for row in kept if row[-1])
    report.kept_sales_rows = report.kept_rows - report.kept_refund_rows
    return kept, report


_SCHEMA = """
CREATE TABLE stores (store_id TEXT PRIMARY KEY, store_name TEXT, category TEXT, district TEXT);
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_name TEXT,
                       product_category TEXT, unit_price REAL);
CREATE TABLE sales_clean (
    order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,
    qty INTEGER, amount_cents INTEGER, payment TEXT, is_refund INTEGER
);
CREATE INDEX idx_clean_date ON sales_clean(date);
CREATE INDEX idx_clean_store ON sales_clean(store_id);
CREATE INDEX idx_clean_product ON sales_clean(product_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def build_clean_db(source: Path, target: Path) -> CleaningReport:
    """从只读的源库重建清洗表。返回清洗台账，供 `/api/health` 与数据质量面板使用。"""
    if not source.exists():
        raise FileNotFoundError("找不到源数据库：%s" % source)
    src = open_readonly(source)
    try:
        store_rows = [tuple(r) for r in src.execute("SELECT store_id, store_name, category, district FROM stores")]
        product_rows = [
            tuple(r)
            for r in src.execute(
                "SELECT product_id, product_name, product_category, unit_price FROM products"
            )
        ]
        # 维表自己也要规范化，否则 ` S01` 会变成另一个门店。
        stores = [(normalise_id(r[0]), r[1], r[2], r[3]) for r in store_rows]
        products = [(normalise_id(r[0]), r[1], r[2], r[3]) for r in product_rows]
        rows, report = clean_rows(
            src.execute("SELECT order_id, date, store_id, product_id, qty, amount, payment FROM sales"),
            known_stores={row[0] for row in stores},
            known_products={row[0] for row in products},
        )
    finally:
        src.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    out = sqlite3.connect(target)
    try:
        out.executescript(_SCHEMA)
        out.executemany("INSERT INTO stores VALUES (?,?,?,?)", stores)
        out.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
        out.executemany("INSERT INTO sales_clean VALUES (?,?,?,?,?,?,?,?)", rows)
        out.execute(
            "INSERT INTO meta VALUES ('cleaning_report', ?)",
            (json.dumps(report.as_dict(), ensure_ascii=False),),
        )
        out.execute("INSERT INTO meta VALUES ('source_db', ?)", (source.name,))
        out.execute(
            "INSERT INTO meta VALUES ('built_at', ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        out.commit()
    finally:
        out.close()
    return report
