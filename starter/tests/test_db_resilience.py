"""数据库连接的韧性。

多轮对话里出现过的现象：第一轮答得好好的，第二第三轮连着报 OperationalError。
原因是连接按线程复用，坏一次之后这个线程处理的每个请求都接着坏。
"""

from __future__ import annotations

import sqlite3

from kbqa.tools import DataTools


def _make_db(path) -> None:
    conn = sqlite3.connect(path.as_posix())
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()


def test_query_recovers_when_connection_dies(tmp_path):
    db = tmp_path / "clean.sqlite"
    _make_db(db)

    tools = DataTools(db)
    assert tools.query("SELECT n FROM t").fetchone()[0] == 7

    # 模拟这条线程局部连接坏掉：重建库、磁盘抖动、文件被换掉都是这个表现。
    tools._local.conn.close()

    assert tools.query("SELECT n FROM t").fetchone()[0] == 7
    # 重开之后连接是新的一条，后续查询继续走它，不会每次都重连。
    assert tools._local.conn is not None


def test_query_raises_when_file_is_gone(tmp_path):
    db = tmp_path / "clean.sqlite"
    _make_db(db)
    tools = DataTools(db)
    tools.query("SELECT n FROM t").fetchone()
    tools._local.conn.close()
    db.unlink()
    try:
        tools.query("SELECT n FROM t")
    except sqlite3.Error:
        pass
    else:  # pragma: no cover - 理论上到不了这里
        raise AssertionError("库文件没了还查出了结果")
