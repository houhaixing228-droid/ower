# -*- coding: utf-8 -*-
"""完整流水线演示：Producer -> MQ1 -> Consumer -> MQ2 -> Writer -> MQ3 -> Callback

零依赖运行（内存队列 + SQLite + mock LLM）：

    python run_pipeline.py
    python run_pipeline.py --segments 300 --consumers 4
    python run_pipeline.py --broker rabbitmq          # 需要本机有 RabbitMQ

跑完会打印每个任务的状态、耗时分布，以及标签校验的通过情况。
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config, sample_data                       # noqa: E402
from app.broker import build_broker                       # noqa: E402
from app.llm import build_llm                             # noqa: E402
from app.pipeline import Producer, Consumer, Writer, Callback, DeadLetterWatcher  # noqa: E402
from app.store import Store                               # noqa: E402


# --------------------------------------------------------------------------
# 上游回调接收端（模拟业务方的 webhook）
# --------------------------------------------------------------------------
class MockUpstream(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):                                    # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            MockUpstream.received.append(json.loads(raw.decode("utf-8")))
        except ValueError:
            MockUpstream.received.append({"raw": raw.decode("utf-8", "replace")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):                         # 静音
        return


def start_mock_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/callback" % srv.server_address[1]


# --------------------------------------------------------------------------
def run(segment_count, consumers, broker_backend, llm_backend, db_path):
    broker = build_broker(broker_backend)
    first = None
    srv, callback_url = start_mock_upstream()

    store = Store(":memory:" if db_path == ":memory:" else db_path)
    llm = build_llm(llm_backend)

    producer = Producer(broker, store)
    writer = Writer(broker, store)
    callback = Callback(broker, store)
    dlq = DeadLetterWatcher(broker, store)
    consumer_objs = [Consumer(broker, store, llm, "consumer-%d" % (i + 1))
                     for i in range(consumers)]

    segments = sample_data.make_segments(segment_count, seed=11, inject_bad=True)
    task_id = "demo-batch-001"

    print("=" * 74)
    print("AIPE pipeline demo")
    print("  broker    : %s" % broker_backend)
    print("  llm       : %s" % llm_backend)
    print("  segments  : %d" % len(segments))
    print("  consumers : %d" % consumers)
    print("  priority  : %d (batch <=5 -> 10, <=20 -> 8, <=100 -> 6, <=500 -> 4, >500 -> 2)"
          % config.priority_for(len(segments)))
    print("=" * 74)

    t0 = time.time()
    accepted = producer.submit_batch(task_id, "demo-program", segments,
                                     callback_url=callback_url)
    print("submit_batch -> %s" % accepted)
    print()

    stop = threading.Event()
    counters = {"ok": 0, "failed": 0, "callback": 0}
    lock = threading.Lock()

    def consume_loop(cons):
        """模拟一个 consumer Pod：一直拉 MQ1 直到队列空且任务结束。"""
        idle = 0
        while not stop.is_set():
            try:
                res = cons.process_one(timeout=0.02)
            except Exception:                              # noqa: BLE001
                res = None
            if res is None:
                idle += 1
                if idle > 60:
                    return
                continue
            idle = 0
            with lock:
                counters[res["outcome"]] += 1

    threads = [threading.Thread(target=consume_loop, args=(c,), daemon=True)
               for c in consumer_objs]
    for t in threads:
        t.start()

    last_report = 0.0
    while True:
        for _ in range(50):
            if writer.process_one(timeout=0.001) is None:
                break
        if callback.process_one(timeout=0.001):
            with lock:
                counters["callback"] += 1
        dlq.sweep()

        run_row = store.get_run(task_id)
        done = run_row and run_row["status"] == "done"
        alive = any(t.is_alive() for t in threads)
        now = time.time()
        if now - last_report > 0.5 and not done:
            pct = 100.0 * (run_row["progress"] + run_row["failed"]) / max(1, run_row["total"])
            print("  progress %3d/%d  (%5.1f%%)  elapsed %.1fs"
                  % (run_row["progress"] + run_row["failed"], run_row["total"], pct, now - t0))
            last_report = now
        if done and not alive:
            break
        if now - t0 > 180:
            print("  !! timeout waiting for completion")
            break

    stop.set()
    for t in threads:
        t.join(timeout=2.0)

    # 把可能残留在 DLQ 里的消息认领掉
    dlq.sweep()

    elapsed = time.time() - t0
    run_row = store.get_run(task_id)
    segs = store.get_segments(task_id)
    eval_yes = sum(1 for s in segs if s.get("evaluation") == "Y")
    eval_no = sum(1 for s in segs if s.get("evaluation") == "N")
    tag_fail = sum(1 for s in segs if s.get("tag_ok") == 0)

    print()
    print("=" * 74)
    print("RESULT")
    print("-" * 74)
    print("  status        : %s" % run_row["status"])
    print("  progress      : %d / %d" % (run_row["progress"], run_row["total"]))
    print("  failed        : %d" % run_row["failed"])
    print("  elapsed       : %.2f s" % elapsed)
    print("  throughput    : %.1f segment/s" % (len(segments) / max(elapsed, 1e-6)))
    print("  evaluation Y/N: %d / %d" % (eval_yes, eval_no))
    print("  tag mismatch  : %d" % tag_fail)
    print("  dlq reclaimed : %d" % dlq.reclaimed)
    print("  callbacks     : %d" % len(MockUpstream.received))
    if MockUpstream.received:
        print("  callback body : %s" % json.dumps(MockUpstream.received[-1],
                                                  ensure_ascii=False))
    print("=" * 74)

    srv.shutdown()
    store.close()
    return run_row


def main():
    ap = argparse.ArgumentParser(description="AIPE pipeline end-to-end demo")
    ap.add_argument("--segments", type=int, default=200)
    ap.add_argument("--consumers", type=int, default=4)
    ap.add_argument("--broker", default="memory", choices=["memory", "rabbitmq"])
    ap.add_argument("--llm", default="mock", choices=["mock", "openai"])
    ap.add_argument("--db", default=":memory:")
    args = ap.parse_args()
    run(args.segments, args.consumers, args.broker, args.llm, args.db)


if __name__ == "__main__":
    main()
