# -*- coding: utf-8 -*-
"""四个流水线角色。

拆成独立类而不是一个进程内的函数链，是因为生产上是四个独立的
K8s Deployment：Producer 按 CPU 扩缩，Consumer 按队列积压量扩缩，
Writer 固定副本，Callback 只关心回调重试。这里用同一份代码、
不同 entry point 来对应这种部署形态。

    Producer : 收 batch -> 建 run/segments -> 推 MQ1
    Consumer : 拉 MQ1 -> 拼 Prompt -> 调 LLM -> 推 MQ2
    Writer   : 拉 MQ2 -> 写库（单事务）-> 全部完成时推 MQ3
    Callback : 拉 MQ3 -> HTTP POST 上游 -> 失败指数退避重试
"""

import json
import os
import threading
import time

from . import config
from .segment import build_context, check_tags


# ==========================================================================
class Producer(object):
    """接收上游 batch。

    幂等：同一个 task_id 重复推送不会重复建 segment。
    上游网络抖动重推是常态，没有幂等就会把同一批数据翻两遍。
    """

    def __init__(self, broker, store, window=None):
        self.broker = broker
        self.store = store
        self.window = config.CONTEXT_WINDOW if window is None else window
        self.broker.declare_queue(config.MQ_TASK)

    def submit_batch(self, task_id, program_id, segments, callback_url=None,
                     priority_override=None):
        """提交一批 segment。

        priority_override 用于做对照实验：传 0 时所有消息优先级相同，
        队列退化为严格 FIFO —— 也就是做优先级调度之前的行为。
        生产代码里不需要这个参数。
        """
        segments = list(segments)
        if not segments:
            raise ValueError("empty batch")
        if len(segments) > config.MAX_BATCH_SEGMENTS:
            raise ValueError("batch exceeds %d segments" % config.MAX_BATCH_SEGMENTS)

        existing = self.store.get_run(task_id)
        if existing and existing.get("total"):
            return {"task_id": task_id, "accepted": False,
                    "reason": "duplicate task_id (idempotent skip)",
                    "total": existing["total"]}

        priority = (config.priority_for(len(segments))
                    if priority_override is None else priority_override)
        self.store.create_run(task_id, program_id, len(segments), len(segments),
                              priority, callback_url)
        self.store.create_segments(task_id, segments)
        self.store._conn.execute(
            "UPDATE runs SET status='queued', updated_at=? WHERE task_id=?",
            (time.time(), task_id))
        self.store._conn.commit()

        # 一条 segment 一条消息：这样 consumer 的扩缩容才能按 segment 数走，
        # 而不是按 task 数（一个 task 可能只有 3 段，也可能有 790 段）。
        for i, seg in enumerate(segments):
            self.broker.publish(config.MQ_TASK, {
                "schema_version": 1,
                "task_id": task_id,
                "program_id": program_id,
                "segment_index": i,
                "string_id": seg.get("string_id"),
                "source": seg.get("source", ""),
                "mt": seg.get("mt", ""),
                "context": build_context(segments, i, self.window),
                "priority": priority,
                "attempts": 0,
                "enqueued_at": time.time(),
            }, priority=priority)

        return {"task_id": task_id, "accepted": True, "total": len(segments),
                "priority": priority}


# ==========================================================================
class Consumer(object):
    """拉 MQ1 -> 调 LLM -> 推 MQ2。

    两种失败路径要分开处理：
      模型调用失败（可重试）-> 自己退避重试 N 次；仍失败则发 failed 结果并 ack，
                              不能让 broker 无限重投，否则会堵死队列
      进程被杀（不可知）    -> 不 ack，靠 broker 可见性超时重投给别的副本
    """

    def __init__(self, broker, store, llm, consumer_id="consumer-1"):
        self.broker = broker
        self.store = store
        self.llm = llm
        self.consumer_id = consumer_id
        self.guidelines = {
            "general": "Keep the meaning and tone of the source. Do not add explanations.",
            "spec": "Preserve product names verbatim. Use the glossary for domain terms.",
            "term": "",
            "dnt": "Do Not Translate:\nAPI\nSDK\ncloud",
            "tag": "Inline placeholders must keep the same count, ids and nesting as the source.",
        }
        self.broker.declare_queue(config.MQ_TASK)

    def process_one(self, timeout=0.05):
        msg = self.broker.consume(config.MQ_TASK, timeout=timeout)
        if msg is None:
            return None
        body = msg.body
        task_id, idx = body["task_id"], body["segment_index"]
        self.store.mark_processing(task_id, idx, self.consumer_id)

        last_error = None
        for attempt in range(config.LLM_MAX_RETRIES):
            try:
                res = self.llm.post_edit(body, body.get("context") or [], self.guidelines)
                break
            except Exception as exc:                      # noqa: BLE001
                last_error = exc
                time.sleep(0.01 * (2 ** attempt))         # 指数退避
        else:
            # 重试用尽：发失败结果并 ack。不要 nack，否则消息会一直转圈。
            self.broker.publish(config.MQ_RESULT, {
                "schema_version": 1, "task_id": task_id, "segment_index": idx,
                "string_id": body.get("string_id"), "outcome": "failed",
                "error": "%s: %s" % (type(last_error).__name__, last_error),
                "attempts": config.LLM_MAX_RETRIES,
                "consumer_id": self.consumer_id, "finished_at": time.time(),
            }, priority=body.get("priority", 0))
            self.broker.ack(msg)
            return {"task_id": task_id, "segment_index": idx, "outcome": "failed"}

        self.broker.publish(config.MQ_RESULT, {
            "schema_version": 1, "task_id": task_id, "segment_index": idx,
            "string_id": body.get("string_id"), "outcome": "ok",
            "final_translation": res["final_translation"],
            "evaluation_result": res["evaluation_result"],
            "comment": res.get("comment", ""),
            "tag_ok": res.get("tag_ok"),
            "metrics": {"tag_ok": res.get("tag_ok")},
            "consumer_id": self.consumer_id,
            "llm_latency_ms": res.get("llm_latency_ms"),
            "attempts": attempt + 1,
            "finished_at": time.time(),
        }, priority=body.get("priority", 0))
        self.broker.ack(msg)
        return {"task_id": task_id, "segment_index": idx, "outcome": "ok"}


# ==========================================================================
class Writer(object):
    """拉 MQ2 -> 写库 -> 任务完结则推 MQ3。

    这里有一条不那么显然的规则：结尾判定不能只看 progress == total。
    因为可能有 segment 进了 DLQ 永远不回来，那样任务永远到不了 total。
    正确的做法是再查一次"还有没有非终态 segment"，没有就强制收尾，
    并在回调里说明哪些失败了 —— 上游宁可拿到部分结果 + 失败明细，
    也不愿意一直等到超时。
    """

    def __init__(self, broker, store):
        self.broker = broker
        self.store = store
        self.broker.declare_queue(config.MQ_RESULT)
        self.broker.declare_queue(config.MQ_CALLBACK)

    def process_one(self, timeout=0.05):
        msg = self.broker.consume(config.MQ_RESULT, timeout=timeout)
        if msg is None:
            return None
        body = msg.body
        task_id, idx = body["task_id"], body["segment_index"]

        if body.get("outcome") == "failed":
            self.store.mark_failed(task_id, idx, body.get("error", "unknown"))
            counted_out = False
        else:
            counted_out = self.store.persist_result(body)

        self.broker.ack(msg)

        run = self.store.get_run(task_id)
        if run is None or run["status"] == "done":
            return {"task_id": task_id, "segment_index": idx,
                    "outcome": body.get("outcome", "ok"), "task_finished": False}

        # 收尾判定不能只看计数：可能有 segment 进了 DLQ 永远回不来，
        # 那样计数永远差几条。所以补一条"还有没有非终态 segment"的检查。
        remaining = self.store.pending_count(task_id)
        finished = counted_out or remaining == 0
        if not finished:
            return {"task_id": task_id, "segment_index": idx,
                    "outcome": body.get("outcome", "ok"), "task_finished": False}

        self.store.finish_run(task_id)
        run = self.store.get_run(task_id)
        if run.get("callback_url"):
            self.broker.publish(config.MQ_CALLBACK, {
                "schema_version": 1, "task_id": task_id, "url": run["callback_url"],
                "progress": run["progress"], "total": run["total"],
                "failed": run["failed"], "priority": run.get("priority", 0),
            }, priority=run.get("priority", 0))
        return {"task_id": task_id, "segment_index": idx,
                "outcome": body.get("outcome", "ok"), "task_finished": True}


# ==========================================================================
class Callback(object):
    """拉 MQ3 -> 通知上游。

    为什么单独一条队列：HTTP 回调带重试退避，慢的时候能拖几十秒。
    早期版本是 writer 直接同步发回调，结果 writer 主线程被拖住，
    上游看起来就是"任务卡住不动"。把慢路径隔离出来是对的。
    """

    def __init__(self, broker, store, post_fn=None, max_attempts=4):
        self.broker = broker
        self.store = store
        self.post_fn = post_fn or _http_post
        self.max_attempts = max_attempts
        self.delivered = []

    def process_one(self, timeout=0.05):
        msg = self.broker.consume(config.MQ_CALLBACK, timeout=timeout)
        if msg is None:
            return None
        body = msg.body
        error = None
        for attempt in range(self.max_attempts):
            try:
                self.post_fn(body["url"], {
                    "task_id": body["task_id"], "status": "done",
                    "progress": body.get("progress"), "total": body.get("total"),
                    "failed": body.get("failed", 0),
                })
                self.store.record_callback(body["task_id"], body["url"], "ok")
                self.broker.ack(msg)
                self.delivered.append(body["task_id"])
                return {"task_id": body["task_id"], "status": "ok", "attempts": attempt + 1}
            except Exception as exc:                       # noqa: BLE001
                error = exc
                time.sleep(0.02 * (2 ** attempt))
        self.store.record_callback(body["task_id"], body["url"], "failed", str(error))
        self.broker.ack(msg)
        return {"task_id": body["task_id"], "status": "failed", "error": str(error)}


def _http_post(url, payload):
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Signature": "demo-shared-secret"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status >= 300:
            raise RuntimeError("callback HTTP %d" % resp.status)
        return resp.read()


# ==========================================================================
class DeadLetterWatcher(object):
    """监听 DLQ，把对应的 segment 标记为 timeout。

    超时消息不会自己变回正常结果，必须有人认领它，
    否则任务计数永远差几条，上游一直等。
    """

    def __init__(self, broker, store):
        self.broker = broker
        self.store = store
        self.broker.declare_queue(config.MQ_TASK_DLQ)
        self.reclaimed = 0

    def sweep(self):
        while True:
            msg = self.broker.consume(config.MQ_TASK_DLQ, timeout=0.001)
            if msg is None:
                break
            body = msg.body
            self.store.mark_timeout(body["task_id"], body["segment_index"])
            self.broker.ack(msg)
            self.reclaimed += 1
        return self.reclaimed


def build_pipeline(broker, store, llm=None, consumer_id="consumer-1"):
    """一次性组装四个角色，便于本地串起来跑。"""
    from .llm import build_llm
    return {
        "producer": Producer(broker, store),
        "consumer": Consumer(broker, store, llm or build_llm(), consumer_id),
        "writer": Writer(broker, store),
        "callback": Callback(broker, store),
        "dlq": DeadLetterWatcher(broker, store),
    }
