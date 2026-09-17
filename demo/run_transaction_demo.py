# -*- coding: utf-8 -*-
"""事务边界演示：为什么"写结果"和"推进度"必须在同一个事务里。

这是第二个真实线上事故：

    writer 处理完一条 segment 后做两件事：
        1. 把 segment 标成 done，写入译文
        2. 把 task 的 progress 加一
    这两步各自独立提交。平时看不出问题，直到某次进程在两步之间挂掉
    （连接断开、Pod 被驱逐、死锁重试耗尽），
    segment 已经是 done，progress 却没动。

    于是 progress 永远停在 total-1。reaper 巡检发现这个任务
    长时间没有任何进展，判定它卡死，把整个任务杀掉并标记失败。
    之前已经跑完的几百条 segment 白跑。

    现场统计：一天内命中 9 个任务。

这个脚本用同一份存储代码跑两条路径，把差异摆出来：

    python run_transaction_demo.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config                                   # noqa: E402
from app.store import Store                              # noqa: E402


TOTAL = 12


def _result(task_id, idx):
    return {
        "task_id": task_id, "segment_index": idx,
        "final_translation": "translated-%d" % idx, "evaluation_result": "Y",
        "tag_ok": True, "consumer_id": "consumer-1",
    }


def _seed(store, task_id):
    """建 run + 逐条 segment。

    注意 persist_result 带 `AND status != 'done'` 的幂等条件，
    segment 行必须先存在，否则 UPDATE 影响 0 行、进度不会推进。
    """
    store.create_run(task_id, "prog", TOTAL, TOTAL, 6)
    store.create_segments(task_id, [
        {"string_id": "s-%02d" % i, "source": "source line %d" % i,
         "mt": "machine translation %d" % i}
        for i in range(TOTAL)
    ])


def reaper_check(store, task_id, simulated_now=None):
    """模拟 reaper 的判定逻辑。

    生产里的阈值是 1800 秒：一个任务在这个时间内 updated_at 没有推进，
    就认为它卡死了。这里用 simulated_now 做时间旅行，免得真等半小时。
    """
    run = store.get_run(task_id)
    now = simulated_now if simulated_now is not None else time.time()
    stall = now - (run.get("updated_at") or now)
    if run["status"] == "done":
        return "DONE", stall
    if stall > config.REAPER_STALL_SEC:
        return "REAP", stall
    return "OK", stall


def scenario_correct():
    print("[路径 A] 单事务：segment 写入 + 进度推进 一起提交")
    print("-" * 70)
    store = Store(":memory:")
    task_id = "task-correct"
    _seed(store, task_id)

    for i in range(TOTAL):
        store.persist_result(_result(task_id, i))
    store.finish_run(task_id)

    run = store.get_run(task_id)
    segs = store.get_segments(task_id)
    done = sum(1 for s in segs if s["status"] == "done")
    print("  segments done : %d / %d" % (done, TOTAL))
    print("  progress      : %d / %d" % (run["progress"], run["total"]))
    print("  status        : %s" % run["status"])
    verdict, stall = reaper_check(store, task_id)
    print("  reaper        : %s" % verdict)
    print()
    store.close()
    return run


def scenario_buggy():
    print("[路径 B] 两次提交：segment 写入与进度推进各自成事务")
    print("        在最后一条的两次写入之间模拟进程崩溃")
    print("-" * 70)
    store = Store(":memory:")
    task_id = "task-buggy"
    _seed(store, task_id)

    # 前 TOTAL-1 条正常走完
    for i in range(TOTAL - 1):
        store.persist_result_buggy(_result(task_id, i))
    run = store.get_run(task_id)
    print("  处理完前 %d 条 -> progress %d / %d"
          % (TOTAL - 1, run["progress"], run["total"]))

    # 最后一条：segment 已写成 done，但进度没推上去
    try:
        store.persist_result_buggy(_result(task_id, TOTAL - 1), fail_after_first=True)
    except RuntimeError as exc:
        print("  第 %d 条出错：%s" % (TOTAL, exc))

    run = store.get_run(task_id)
    segs = store.get_segments(task_id)
    done = sum(1 for s in segs if s["status"] == "done")
    print()
    print("  segments done : %d / %d   ← 数据其实全跑完了" % (done, TOTAL))
    print("  progress      : %d / %d   ← 计数永远差 1" % (run["progress"], run["total"]))
    print("  status        : %s        ← 任务永不结束" % run["status"])

    # 把时间快进到 reaper 阈值之后
    verdict, stall = reaper_check(store, task_id, simulated_now=time.time()
                                 + config.REAPER_STALL_SEC + 5)
    print("  reaper        : %s（停滞 %.0f 秒 > 阈值 %.0f 秒）"
          % (verdict, stall, config.REAPER_STALL_SEC))
    print()
    print("  后果：任务被判卡死并杀掉，前面 %d 条已经算好的译文一起作废，" % (TOTAL - 1))
    print("        上游拿到的是失败，而不是 %d/%d 的部分结果。" % (TOTAL - 1, TOTAL))
    print()
    store.close()
    return run


def main():
    print("=" * 74)
    print("事务边界演示：为什么两条 UPDATE 必须在同一个事务里")
    print("  reaper 阈值 REAPER_STALL_SEC = %d 秒" % config.REAPER_STALL_SEC)
    print("=" * 74)
    print()
    a = scenario_correct()
    b = scenario_buggy()

    print("=" * 74)
    print("结论")
    print("-" * 74)
    print("  路径 A progress %d/%d  status=%s" % (a["progress"], a["total"], a["status"]))
    print("  路径 B progress %d/%d  status=%s" % (b["progress"], b["total"], b["status"]))
    print()
    print("  差别只有一行：路径 A 把两条 UPDATE 放进同一个事务，")
    print("  路径 B 让它们各自提交。在出故障之前，两者的表现完全一样 ——")
    print("  这就是这类 bug 难发现的地方：它只在异常路径上出现。")
    print()
    print("  顺带一个容易忽略的点：MQ 的 publish 要放在事务之外。")
    print("  如果先 publish 再提交事务，事务回滚后消息已经发出去了，")
    print("  消费者取到一条指向不存在数据的消息。")
    print("=" * 74)


if __name__ == "__main__":
    main()
