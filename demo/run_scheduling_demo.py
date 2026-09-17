# -*- coding: utf-8 -*-
"""调度对照实验：FIFO 队列 vs 优先级队列。

这个脚本复现的是一次真实线上事故：

    上游同时推来一个大任务（几百段）和若干小任务（各几段）。
    队列是严格 FIFO 的，大任务的消息先入队，把整个队列占住。
    小任务排在后面，全部饿死 —— 上游看到的现象是
    "我推了一小批，五分钟了还没回来"。

    现场统计：一个时段内 79 个小任务因此失败。

修法不是加机器，而是给消息按 batch 大小分优先级：小任务先跑。
本脚本用同一份代码跑两种模式，把差别量化出来。

    python run_scheduling_demo.py
    python run_scheduling_demo.py --big 790 --small-count 20 --small-size 5 --latency-ms 3

输出的是真实跑出来的数字，不是估算。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config, sample_data                       # noqa: E402
from app.broker import MemoryBroker                       # noqa: E402
from app.llm import MockLLM                               # noqa: E402
from app.pipeline import Producer, Consumer, Writer       # noqa: E402
from app.store import Store                               # noqa: E402


def run_mode(use_priority, big_size, small_count, small_size, latency_ms, verbose=True):
    """跑一次实验，返回统计结果。

    用单 consumer 串行处理，是为了把"调度顺序"这一个变量单独隔离出来 ——
    多 consumer 会并发，顺序差异被并行度掩盖。
    """
    broker = MemoryBroker()
    store = Store(":memory:")
    # 关掉随机超时，保证实验可复现；这里观察的是调度顺序，不是失败路径
    llm = MockLLM(latency_ms=latency_ms, timeout_rate=0.0)
    producer = Producer(broker, store)
    consumer = Consumer(broker, store, llm, "consumer-1")
    writer = Writer(broker, store)

    big_segments = sample_data.make_segments(big_size, seed=7, inject_bad=False)
    small_batches = [sample_data.make_segments(small_size, seed=100 + i, inject_bad=False)
                     for i in range(small_count)]

    submitted = {}
    t0 = time.time()

    # 顺序很关键：大任务先提交，先把队列占住 —— 这就是事故发生的前提
    override = 0 if not use_priority else None
    producer.submit_batch("big-001", "prog", big_segments, priority_override=override)
    submitted["big-001"] = time.time() - t0
    for i in range(small_count):
        tid = "small-%02d" % i
        producer.submit_batch(tid, "prog", small_batches[i], priority_override=override)
        submitted[tid] = time.time() - t0

    total_msgs = big_size + small_count * small_size
    if verbose:
        print("  队列中消息数 %d（大任务 %d + 小任务 %d x %d），单 consumer 串行处理"
              % (total_msgs, big_size, small_count, small_size))

    pending = set(submitted)
    done_at = {}
    processed = 0

    while pending:
        res = consumer.process_one(timeout=0)
        if res is not None:
            processed += 1
        while writer.process_one(timeout=0) is not None:
            pass
        for tid in list(pending):
            row = store.get_run(tid)
            if row and row["status"] == "done":
                done_at[tid] = time.time() - t0
                pending.discard(tid)
        if processed > total_msgs + 50:            # 保险丝
            break

    big_wait = done_at.get("big-001", float("inf"))
    small_waits = sorted(done_at[t] for t in done_at if t != "big-001")
    store.close()

    def pct(vals, p):
        if not vals:
            return 0.0
        k = min(len(vals) - 1, int(round((len(vals) - 1) * p)))
        return vals[k]

    stats = {
        "big_wait": big_wait,
        "small_avg": sum(small_waits) / len(small_waits) if small_waits else 0.0,
        "small_min": small_waits[0] if small_waits else 0.0,
        "small_max": small_waits[-1] if small_waits else 0.0,
        "small_p95": pct(small_waits, 0.95),
        "small_count": len(small_waits),
        "total_msgs": total_msgs,
    }
    return stats


def main():
    ap = argparse.ArgumentParser(description="FIFO vs priority queue scheduling")
    ap.add_argument("--big", type=int, default=790, help="大任务的 segment 数")
    ap.add_argument("--small-count", type=int, default=20)
    ap.add_argument("--small-size", type=int, default=5)
    ap.add_argument("--latency-ms", type=float, default=3.0, help="每条 segment 的处理耗时")
    args = ap.parse_args()

    print("=" * 74)
    print("调度对照实验：FIFO vs 优先级队列")
    print("  大任务 %d 段 / 小任务 %d 个 x %d 段 / 单条处理 %.1f ms"
          % (args.big, args.small_count, args.small_size, args.latency_ms))
    print("  两种模式跑的是同一份代码，唯一差别是消息优先级")
    print("=" * 74)

    print()
    print("[模式 A] 严格 FIFO —— 所有消息同优先级")
    t = time.time()
    a = run_mode(False, args.big, args.small_count, args.small_size, args.latency_ms)
    print("  实际耗时 %.1fs" % (time.time() - t))
    print("  大任务 (%d 段)   完成于 %.2fs" % (args.big, a["big_wait"]))
    print("  小任务 (%d 个)   平均等待 %.2fs  (最快 %.2fs / 最慢 %.2fs / p95 %.2fs)"
          % (a["small_count"], a["small_avg"], a["small_min"], a["small_max"], a["small_p95"]))

    print()
    print("[模式 B] 优先级队列 —— 按 batch 大小分级 (%s)"
          % ", ".join("<=%s->%d" % (l if l else "inf", p) for l, p in config.PRIORITY_BY_SIZE))
    t = time.time()
    b = run_mode(True, args.big, args.small_count, args.small_size, args.latency_ms)
    print("  实际耗时 %.1fs" % (time.time() - t))
    print("  大任务 (%d 段)   完成于 %.2fs" % (args.big, b["big_wait"]))
    print("  小任务 (%d 个)   平均等待 %.2fs  (最快 %.2fs / 最慢 %.2fs / p95 %.2fs)"
          % (b["small_count"], b["small_avg"], b["small_min"], b["small_max"], b["small_p95"]))

    print()
    print("=" * 74)
    print("对照结论")
    print("-" * 74)
    if a["small_avg"] > 0:
        drop = 100.0 * (a["small_avg"] - b["small_avg"]) / a["small_avg"]
        print("  小任务平均等待 : %.2fs  ->  %.2fs   （下降 %.1f%%）"
              % (a["small_avg"], b["small_avg"], drop))
        print("  小任务 p95 等待: %.2fs  ->  %.2fs   （下降 %.1f%%）"
              % (a["small_p95"], b["small_p95"],
                 100.0 * (a["small_p95"] - b["small_p95"]) / max(a["small_p95"], 1e-9)))
    print("  大任务总耗时   : %.2fs  ->  %.2fs   （变化 %+.1f%%）"
          % (a["big_wait"], b["big_wait"],
             100.0 * (b["big_wait"] - a["big_wait"]) / max(a["big_wait"], 1e-9)))
    print()
    print("  代价说明：优先级调度不是免费的 —— 高优先级消息插队会让大任务")
    print("  的完成时间略微后移。这里换来的是小任务的等待时间从秒级降到毫秒级，")
    print("  而大任务本身是批处理，晚几十毫秒没有业务影响。")
    print("=" * 74)


if __name__ == "__main__":
    main()
