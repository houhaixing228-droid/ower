# -*- coding: utf-8 -*-
"""队列抽象层。

抽象层存在的理由：
    项目早期是"单进程 + 线程池"直接调 LLM，跑得通但没法弹性伸缩。
    要上 K8s 按队列积压量扩缩 consumer，就必须把队列从进程内挪到真正的 broker。
    有了抽象层，同一份业务代码可以在两种形态间切换：
        MemoryBroker   零依赖，本地演示 / 单元测试
        RabbitMQBroker 生产形态，优先级队列 + 可见性超时 + 死信队列

两个实现都必须满足的语义：
    1. 优先级投递（数字越大越先出队）
    2. ack 之前消息不算消费完成；未 ack 且超过可见性超时 → 自动重投
    3. 重投次数超过上限 → 进死信队列，而不是无限循环
"""

import heapq
import itertools
import threading
import time
import uuid

from . import config


class Message(object):
    """队列消息信封。body 是业务负载，其余是投递元数据。"""

    __slots__ = ("body", "msg_id", "attempts", "receipt", "enqueued_at", "priority")

    def __init__(self, body, msg_id=None, attempts=0, priority=0, receipt=None):
        self.body = body or {}
        self.msg_id = msg_id or uuid.uuid4().hex
        self.attempts = attempts
        self.priority = priority
        self.receipt = receipt          # broker 侧的投递句柄
        self.enqueued_at = time.time()

    @property
    def task_id(self):
        return self.body.get("task_id")

    @property
    def segment_index(self):
        return self.body.get("segment_index")

    def __repr__(self):
        return "<Message %s task=%s seg=%s prio=%d>" % (
            self.msg_id[:8], self.task_id, self.segment_index, self.priority
        )


class DeadLetter(Exception):
    """重投次数超限，消息已转入死信队列。"""


class Broker(object):
    """队列接口。"""

    def declare_queue(self, name, max_priority=10):
        raise NotImplementedError

    def publish(self, queue, body, priority=0):
        raise NotImplementedError

    def consume(self, queue, timeout=0.1, auto_ack=False):
        """取一条消息。取不到返回 None。"""
        raise NotImplementedError

    def ack(self, message):
        raise NotImplementedError

    def nack(self, message, requeue=True):
        raise NotImplementedError

    def qsize(self, queue):
        raise NotImplementedError

    def purge(self, queue):
        raise NotImplementedError


# ==========================================================================
# 内存实现
# ==========================================================================
class MemoryBroker(Broker):
    """基于 heapq 的进程内队列。

    只用于演示与测试：进程退出消息即丢失，不满足持久化要求。
    但优先级、可见性超时、死信这三个语义与 RabbitMQ 版保持一致，
    所以调度逻辑可以在这上面验证完再上生产。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._queues = {}        # name -> list of (sort_key, seq, msg)
        self._unacked = {}       # msg_id -> (queue, msg, deadline)
        self._counter = itertools.count()
        self._dlq_depth = {}

    def declare_queue(self, name, max_priority=10):
        with self._lock:
            self._queues.setdefault(name, [])

    def publish(self, queue, body, priority=0):
        with self._lock:
            self.declare_queue(queue)
            msg = Message(body, priority=priority)
            # 优先级高的先出队 => 用小根堆，key 取负数
            seq = next(self._counter)
            heapq.heappush(self._queues[queue], (-priority, seq, msg))
            return msg

    def consume(self, queue, timeout=0.1, auto_ack=False):
        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._reap_expired(queue)
                q = self._queues.get(queue) or []
                if q:
                    _, _, msg = heapq.heappop(q)
                    if not auto_ack:
                        self._unacked[msg.msg_id] = (
                            queue, msg, time.time() + config.VISIBILITY_TIMEOUT_SEC
                        )
                    return msg
            if time.time() >= deadline:
                return None
            time.sleep(0.002)

    def ack(self, message):
        with self._lock:
            self._unacked.pop(message.msg_id, None)

    def nack(self, message, requeue=True):
        with self._lock:
            self._unacked.pop(message.msg_id, None)
            if not requeue:
                return
            message.attempts += 1
            if message.attempts > config.MAX_DELIVER_ATTEMPTS:
                # 超过重投上限 -> 死信，交给 DLQ 消费者标记 timeout
                dlq = self._dlq_name(message.body.get("_queue", config.MQ_TASK))
                self._queues.setdefault(dlq, [])
                heapq.heappush(self._queues[dlq],
                               (-message.priority, next(self._counter), message))
                self._dlq_depth[dlq] = self._dlq_depth.get(dlq, 0) + 1
                raise DeadLetter("delivery attempts exhausted -> %s" % dlq)
            self._requeue(message.body.get("_queue", config.MQ_TASK), message)

    def _requeue(self, queue, message):
        heapq.heappush(self._queues.setdefault(queue, []),
                       (-message.priority, next(self._counter), message))

    def _reap_expired(self, queue):
        """把超时未 ack 的消息放回队列。模拟 broker 的可见性超时。"""
        now = time.time()
        for mid, (q, msg, deadline) in list(self._unacked.items()):
            if q == queue and now > deadline:
                del self._unacked[mid]
                msg.attempts += 1
                self._requeue(queue, msg)

    @staticmethod
    def _dlq_name(queue):
        return queue + ".dlq"

    def qsize(self, queue):
        with self._lock:
            return len(self._queues.get(queue) or [])

    def purge(self, queue):
        with self._lock:
            self._queues[queue] = []

    def stats(self):
        with self._lock:
            return {k: len(v) for k, v in self._queues.items() if v}


# ==========================================================================
# RabbitMQ 实现
# ==========================================================================
class RabbitMQBroker(Broker):
    """生产形态。对应 AIPE 线上用的是 RabbitMQ（见 docs/DESIGN_DECISIONS.md 决策 3）。

    一个部署坑：已经存在的队列不能再加 x-max-priority，
    必须停服、删掉原队列、带参数重建 —— 否则 broker 直接拒绝声明。
    """

    def __init__(self, url=None):
        import pika  # 延迟导入，MemoryBroker 使用者不必装 pika

        import os
        self.url = (url or os.environ.get("AIPE_RABBITMQ_URL")
                    or "amqp://guest:guest@127.0.0.1:5672/%2F")
        self._params = pika.URLParameters(self.url)
        self._params.heartbeat = 60
        self._params.blocked_connection_timeout = 30
        self._conn = None
        self._channels = {}
        self._declared = set()

    # -- 连接管理 -------------------------------------------------------
    def _channel(self, queue):
        import pika
        if self._conn is None or self._conn.is_closed:
            self._conn = pika.BlockingConnection(self._params)
            self._channels.clear()
            self._declared.clear()
        if queue not in self._channels or self._channels[queue].is_closed:
            ch = self._conn.channel()
            self._channels[queue] = ch
        if queue not in self._declared:
            # 队列名以 .dlq 结尾的不声明优先级
            args = {} if queue.endswith(".dlq") else {"x-max-priority": 10}
            self._channels[queue].queue_declare(
                queue=queue, durable=True, arguments=args or None
            )
            self._declared.add(queue)
        return self._channels[queue]

    def declare_queue(self, name, max_priority=10):
        self._channel(name)

    def publish(self, queue, body, priority=0):
        import json
        import pika
        ch = self._channel(queue)
        props = pika.BasicProperties(
            delivery_mode=2,                      # 持久化
            priority=max(0, min(9, int(priority))),
            message_id=uuid.uuid4().hex,
        )
        payload = dict(body)
        payload["_queue"] = queue
        ch.basic_publish(exchange="", routing_key=queue,
                         body=json.dumps(payload, ensure_ascii=False), properties=props)

    def consume(self, queue, timeout=0.5, auto_ack=False):
        import json
        ch = self._channel(queue)
        for method, props, body in ch.consume(queue, inactivity_timeout=timeout,
                                              auto_ack=auto_ack):
            if method is None:
                return None
            payload = json.loads(body.decode("utf-8"))
            msg = Message(payload, msg_id=props.message_id or uuid.uuid4().hex,
                          priority=props.priority or 0, receipt=method.delivery_tag)
            if auto_ack:
                return msg
            return msg
        return None

    def ack(self, message):
        ch = self._channels.get(message.body.get("_queue"))
        if ch and message.receipt is not None:
            try:
                ch.basic_ack(message.receipt)
            except Exception:
                # AMQP 的 delivery_tag 是 channel 作用域，重连后无法 ack。
                # 这是协议限制，只能吞掉异常让消息按可见性超时重投。
                pass

    def nack(self, message, requeue=True):
        ch = self._channels.get(message.body.get("_queue"))
        if ch and message.receipt is not None:
            try:
                ch.basic_nack(message.receipt, requeue=requeue)
            except Exception:
                pass

    def qsize(self, queue):
        try:
            ch = self._channel(queue)
            res = ch.queue_declare(queue=queue, durable=True, passive=True)
            return res.method.message_count
        except Exception:
            return -1

    def purge(self, queue):
        try:
            self._channel(queue).queue_purge(queue)
        except Exception:
            pass

    def stats(self):
        return {}


def build_broker(backend=None):
    """按配置构造 broker。"""
    backend = backend or config.BROKER_BACKEND
    if backend == "rabbitmq":
        return RabbitMQBroker()
    return MemoryBroker()
