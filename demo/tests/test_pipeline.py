# -*- coding: utf-8 -*-
"""单元测试。

只依赖标准库，直接跑：

    python -m unittest discover -s tests -v
    python tests/test_pipeline.py

覆盖的是那些"出问题不报错、但结果不对"的地方 —— 标签校验、
调度顺序、幂等、事务边界。这几类问题在演示里好看，在线上要命。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, sample_data, segment              # noqa: E402
from app.broker import MemoryBroker, DeadLetter           # noqa: E402
from app.llm import MockLLM                               # noqa: E402
from app.pipeline import Producer, Consumer, Writer       # noqa: E402
from app.store import Store                               # noqa: E402


class TestTagValidation(unittest.TestCase):
    """行内标签保真 —— 译文的标签必须与原文一一对应。"""

    def test_identical_tags_pass(self):
        ok, detail = segment.check_tags(
            "Welcome {1}, you have {2} messages.",
            "Bienvenido {1}, tiene {2} mensajes.")
        self.assertTrue(ok, detail)

    def test_reordered_tags_pass(self):
        """目标语言语序不同，位置可以变 —— 只比集合，不比顺序。"""
        ok, detail = segment.check_tags(
            "Download {1} from {2}.", "Descargue {2} desde {1}.")
        self.assertTrue(ok, detail)

    def test_missing_tag_fails(self):
        ok, detail = segment.check_tags(
            "Balance {1} points {2}.", "Saldo y puntos {2}.")
        self.assertFalse(ok)
        self.assertFalse(detail["curly_count_ok"])

    def test_wrong_id_fails(self):
        ok, detail = segment.check_tags(
            "Download {1} from {2}.", "Descargue {2} desde {2}.")
        self.assertFalse(ok)
        self.assertTrue(detail["curly_count_ok"])       # 数量对
        self.assertFalse(detail["curly_set_ok"])        # 编号不对

    def test_unbalanced_xml_fails(self):
        ok, detail = segment.check_tags(
            "Click <b>Save</b> to apply <i>changes</i>.",
            "Haga clic en <b>Guardar</b> para aplicar cambios</i>.")
        self.assertFalse(ok)
        self.assertFalse(detail["xml_balanced_ok"])

    def test_escape_form_change_fails(self):
        ok, detail = segment.check_tags(
            "Use &amp; to separate.", "Use & to separate.")
        self.assertFalse(ok)
        self.assertFalse(detail["escape_form_ok"])

    def test_printf_tags(self):
        ok, _ = segment.check_tags("Hello %s, %d items.", "Hola %s, %d elementos.")
        self.assertTrue(ok)
        ok2, _ = segment.check_tags("Hello %s, %d items.", "Hola %s elementos.")
        self.assertFalse(ok2)


class TestContextWindow(unittest.TestCase):
    def setUp(self):
        self.segs = [{"string_id": "s%d" % i, "source": "line %d" % i} for i in range(20)]

    def test_middle_window(self):
        ctx = segment.build_context(self.segs, 10, window=5)
        self.assertEqual(len(ctx), 11)
        self.assertEqual(sum(1 for c in ctx if c["is_self"]), 1)
        self.assertEqual([c["string_id"] for c in ctx][5], "s10")

    def test_head_clamped(self):
        ctx = segment.build_context(self.segs, 1, window=5)
        self.assertEqual(ctx[0]["string_id"], "s0")
        self.assertEqual(sum(1 for c in ctx if c["is_self"]), 1)

    def test_tail_clamped(self):
        ctx = segment.build_context(self.segs, 19, window=5)
        self.assertEqual(ctx[-1]["string_id"], "s19")
        self.assertEqual(sum(1 for c in ctx if c["is_self"]), 1)


class TestPriorityPolicy(unittest.TestCase):
    def test_boundaries(self):
        cases = [(1, 10), (5, 10), (6, 8), (20, 8), (21, 6), (100, 6),
                 (101, 4), (500, 4), (501, 2), (2000, 2)]
        for size, expected in cases:
            self.assertEqual(config.priority_for(size), expected,
                             "size=%d" % size)


class TestBrokerOrdering(unittest.TestCase):
    """优先级出队顺序 —— 这是队列饥饿的修复手段，必须可靠。"""

    def test_higher_priority_first(self):
        b = MemoryBroker()
        b.declare_queue("q")
        b.publish("q", {"tag": "big-1"}, priority=2)
        b.publish("q", {"tag": "big-2"}, priority=2)
        b.publish("q", {"tag": "small"}, priority=10)
        order = [b.consume("q").body["tag"] for _ in range(3)]
        self.assertEqual(order[0], "small")
        self.assertEqual(order[1:], ["big-1", "big-2"])

    def test_fifo_when_same_priority(self):
        """同优先级必须保持入队顺序，否则丢的是可预测性。"""
        b = MemoryBroker()
        b.declare_queue("q")
        for i in range(5):
            b.publish("q", {"i": i}, priority=0)
        order = [b.consume("q").body["i"] for _ in range(5)]
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_dlq_after_attempts_exhausted(self):
        b = MemoryBroker()
        b.declare_queue(config.MQ_TASK)
        b.publish(config.MQ_TASK, {"task_id": "t", "segment_index": 0}, priority=5)
        msg = b.consume(config.MQ_TASK)
        raised = False
        for _ in range(config.MAX_DELIVER_ATTEMPTS + 1):
            try:
                b.nack(msg, requeue=True)
                msg = b.consume(config.MQ_TASK)
            except DeadLetter:
                raised = True
                break
        self.assertTrue(raised, "超过重投上限后应转入死信队列")


class TestIdempotency(unittest.TestCase):
    def test_duplicate_batch_skipped(self):
        broker = MemoryBroker()
        store = Store(":memory:")
        producer = Producer(broker, store)
        segs = sample_data.make_segments(6, seed=1, inject_bad=False)

        first = producer.submit_batch("dup-task", "p", segs)
        self.assertTrue(first["accepted"])
        second = producer.submit_batch("dup-task", "p", segs)
        self.assertFalse(second["accepted"])
        self.assertIn("duplicate", second["reason"])
        # 队列里不该多出一份消息
        self.assertEqual(broker.qsize(config.MQ_TASK), 6)

    def test_persist_result_is_idempotent(self):
        """重复投递同一条 result 不能让 progress 多加。"""
        store = Store(":memory:")
        store.create_run("t1", "p", 2, 2, 6)
        store.create_segments("t1", [{"string_id": "a", "source": "x", "mt": "y"},
                                     {"string_id": "b", "source": "x", "mt": "y"}])
        res = {"task_id": "t1", "segment_index": 0, "final_translation": "A",
               "evaluation_result": "Y", "tag_ok": True}
        store.persist_result(res)
        store.persist_result(res)          # 重投
        self.assertEqual(store.get_run("t1")["progress"], 1)


class TestTransactionBoundary(unittest.TestCase):
    """两条 UPDATE 必须同事务，否则 progress 会永远差 1。"""

    def _seed(self, store, task_id, total):
        store.create_run(task_id, "p", total, total, 6)
        store.create_segments(task_id, [
            {"string_id": "s%d" % i, "source": "s%d" % i, "mt": "m%d" % i}
            for i in range(total)])

    def test_correct_path_reaches_total(self):
        store = Store(":memory:")
        self._seed(store, "ok", 10)
        for i in range(10):
            store.persist_result({"task_id": "ok", "segment_index": i,
                                  "final_translation": "x", "evaluation_result": "Y",
                                  "tag_ok": True})
        store.finish_run("ok")
        run = store.get_run("ok")
        self.assertEqual(run["progress"], 10)
        self.assertEqual(run["status"], "done")

    def test_buggy_path_stalls_before_total(self):
        store = Store(":memory:")
        self._seed(store, "bad", 10)
        for i in range(9):
            store.persist_result_buggy({"task_id": "bad", "segment_index": i,
                                        "final_translation": "x", "evaluation_result": "Y"})
        with self.assertRaises(RuntimeError):
            store.persist_result_buggy({"task_id": "bad", "segment_index": 9,
                                        "final_translation": "x", "evaluation_result": "Y"},
                                       fail_after_first=True)
        run = store.get_run("bad")
        self.assertEqual(run["progress"], 9)                    # 差 1
        self.assertNotEqual(run["status"], "done")              # 永不结束
        done = sum(1 for s in store.get_segments("bad") if s["status"] == "done")
        self.assertEqual(done, 10)                              # 数据其实都写进去了


class TestEndToEnd(unittest.TestCase):
    def test_small_batch_completes(self):
        broker = MemoryBroker()
        store = Store(":memory:")
        llm = MockLLM(latency_ms=0.2, timeout_rate=0.0)
        producer = Producer(broker, store)
        consumer = Consumer(broker, store, llm, "c1")
        writer = Writer(broker, store)

        segs = sample_data.make_segments(30, seed=5, inject_bad=True)
        seed = producer.submit_batch("e2e", "p", segs)
        self.assertEqual(seed["total"], 30)

        guard = 0
        while store.get_run("e2e")["status"] != "done" and guard < 500:
            before = store.get_run("e2e")["progress"] + store.get_run("e2e")["failed"]
            consumer.process_one(timeout=0)
            while writer.process_one(timeout=0) is not None:
                pass
            guard += 1
            after = store.get_run("e2e")["progress"] + store.get_run("e2e")["failed"]
            if before == after:
                time.sleep(0.001)

        run = store.get_run("e2e")
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["progress"] + run["failed"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
