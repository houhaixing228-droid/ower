# -*- coding: utf-8 -*-
"""存储层。

这里有一个刻意的对照：persist_result() 与 persist_result_buggy()。
后者是生产上真实出过的事故 —— 写 segment 和推进度不在同一个事务里，
导致 progress 永远停在 total-1，任务被 reaper 判定为卡死并杀掉。
详见 docs/DESIGN_DECISIONS.md 决策 4。
"""

import json
import os
import sqlite3
import threading
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    task_id      TEXT PRIMARY KEY,
    program_id   TEXT,
    total        INTEGER NOT NULL DEFAULT 0,
    progress     INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'queued',
    batch_size   INTEGER NOT NULL DEFAULT 0,
    priority     INTEGER NOT NULL DEFAULT 0,
    callback_url TEXT,
    created_at   REAL,
    updated_at   REAL,
    finished_at  REAL
);

CREATE TABLE IF NOT EXISTS segments (
    task_id        TEXT NOT NULL,
    segment_index  INTEGER NOT NULL,
    string_id      TEXT,
    source         TEXT,
    mt             TEXT,
    final_text     TEXT,
    evaluation     TEXT,
    consumer_id    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    tag_ok         INTEGER,
    started_at     REAL,
    finished_at    REAL,
    PRIMARY KEY (task_id, segment_index)
);

CREATE TABLE IF NOT EXISTS callbacks (
    task_id     TEXT PRIMARY KEY,
    url         TEXT,
    status      TEXT,
    attempts    INTEGER DEFAULT 0,
    last_error  TEXT,
    fired_at    REAL
);
"""


class Store(object):
    """SQLite 存储。

    check_same_thread=False + 自建锁，是为了让多个消费者线程共用一条连接，
    对应生产里 MySQL 的 per-thread 连接池做法（这里简化了）。
    """

    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        self._lock = threading.RLock()
        if self.path != ":memory:" and os.path.dirname(self.path):
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
            except OSError:
                pass
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- 任务 -----------------------------------------------------------
    def create_run(self, task_id, program_id, total, batch_size, priority,
                   callback_url=None):
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (task_id, program_id, total, progress, "
                "status, batch_size, priority, callback_url, created_at, updated_at) "
                "VALUES (?,?,?,0,'queued',?,?,?,?,?)",
                (task_id, program_id, total, batch_size, priority, callback_url, now, now),
            )
            self._conn.commit()

    def create_segments(self, task_id, segments):
        rows = [(task_id, i, s.get("string_id"), s.get("source"), s.get("mt"))
                for i, s in enumerate(segments)]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO segments (task_id, segment_index, string_id, "
                "source, mt, status) VALUES (?,?,?,?,?,'pending')", rows)
            self._conn.commit()

    def mark_processing(self, task_id, index, consumer_id):
        with self._lock:
            self._conn.execute(
                "UPDATE segments SET status='processing', consumer_id=?, started_at=? "
                "WHERE task_id=? AND segment_index=?",
                (consumer_id, time.time(), task_id, index))
            self._conn.commit()

    # -- 正确版本：单事务 ------------------------------------------------
    def persist_result(self, result):
        """写 segment 结果 + 推进 task 进度，**在同一个事务里**。

        这是修复后的写法。两条 UPDATE 要么一起成功，要么一起回滚，
        所以 progress 不可能落在 total-1 不动。

        注意这里**不**把 run 置为 done。收尾（置 done + 触发回调）统一由
        Writer 负责。早期版本两处都改 status，结果 persist_result 抢先置了
        done，Writer 一看"已经结束了"就跳过回调 —— 任务完成了，上游却永远
        收不到通知。

        返回 bool：计数是否已达标。

        幂等保护：UPDATE 带 `AND status != 'done'` 条件，并检查 rowcount。
        队列重投是常态（可见性超时、broker 抖动），如果不做这层保护，
        progress 会被同一条 segment 重复 +1，最后跑到 total 之上。
        """
        task_id = result["task_id"]
        now = time.time()
        with self._lock:
            with self._conn:                      # with 块即事务，异常自动回滚
                cur = self._conn.execute(
                    "UPDATE segments SET status='done', final_text=?, evaluation=?, "
                    "tag_ok=?, consumer_id=?, finished_at=? "
                    "WHERE task_id=? AND segment_index=? AND status != 'done'",
                    (result.get("final_translation"), result.get("evaluation_result"),
                     1 if result.get("tag_ok") else 0, result.get("consumer_id"),
                     now, task_id, result.get("segment_index")))
                if cur.rowcount:                  # 只有真的从非终态转过来才推进度
                    self._conn.execute(
                        "UPDATE runs SET progress = progress + 1, updated_at=? "
                        "WHERE task_id=?", (now, task_id))
            row = self._conn.execute(
                "SELECT total, progress, failed FROM runs WHERE task_id=?",
                (task_id,)).fetchone()
        return bool(row and row["progress"] + row["failed"] >= row["total"])

    def finish_run(self, task_id):
        """收尾：置 done。由 Writer 在确认没有非终态 segment 后调用。"""
        now = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE runs SET status='done', finished_at=?, updated_at=? "
                    "WHERE task_id=? AND status != 'done'", (now, now, task_id))

    # -- 事故版本：两个事务 ----------------------------------------------
    def persist_result_buggy(self, result, fail_after_first=False):
        """演示用：写 segment 和推进度分成两个事务。

        如果两者之间出现异常（进程被杀、连接断开、死锁重试），
        segment 已是 done，progress 却没动 —— 于是 progress 永远差 1，
        reaper 到点就把整个 task 杀掉。
        """
        task_id = result["task_id"]
        now = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE segments SET status='done', final_text=?, evaluation=?, "
                    "finished_at=? WHERE task_id=? AND segment_index=?",
                    (result.get("final_translation"), result.get("evaluation_result"),
                     now, task_id, result.get("segment_index")))
            if fail_after_first:
                raise RuntimeError("模拟：进程在两次写入之间挂掉")
            with self._conn:
                self._conn.execute(
                    "UPDATE runs SET progress = progress + 1, updated_at=? WHERE task_id=?",
                    (now, task_id))
        return True

    def mark_failed(self, task_id, index, error):
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE segments SET status='failed', final_text=?, attempts=attempts+1, "
                    "finished_at=? WHERE task_id=? AND segment_index=?",
                    (str(error)[:500], time.time(), task_id, index))
                self._conn.execute(
                    "UPDATE runs SET failed = failed + 1, updated_at=? WHERE task_id=?",
                    (time.time(), task_id))

    def mark_timeout(self, task_id, index):
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE segments SET status='timeout', finished_at=? "
                    "WHERE task_id=? AND segment_index=?", (time.time(), task_id, index))

    # -- 查询 -----------------------------------------------------------
    def get_run(self, task_id):
        row = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self):
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at")]

    def get_segments(self, task_id):
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM segments WHERE task_id=? ORDER BY segment_index", (task_id,))]

    def pending_count(self, task_id):
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM segments WHERE task_id=? "
            "AND status NOT IN ('done','failed','timeout')", (task_id,)).fetchone()
        return row["c"] if row else 0

    def record_callback(self, task_id, url, status, error=None):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO callbacks (task_id, url, status, last_error, fired_at) "
                "VALUES (?,?,?,?,?)", (task_id, url, status, error, time.time()))
            self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
