# -*- coding: utf-8 -*-
"""集中配置与调度策略。

这里最值得注意的是 PRIORITY_BY_SIZE —— 它不是拍脑袋定的，
而是队列饥饿事故的修复产物，详见 docs/DESIGN_DECISIONS.md 决策 2。
"""

import os


# --------------------------------------------------------------------------
# 队列名
# --------------------------------------------------------------------------
# 三队列分离的理由：每一段的耗时特征不同，混在一起会让慢环节拖住快环节。
#   MQ1 task    : Producer -> Consumer，单条 segment 一条消息
#   MQ2 result  : Consumer -> Writer，结果落库
#   MQ3 callback: Writer   -> Callback，HTTP 回调（慢路径，必须隔离）
MQ_TASK = "aipe.task.segment"
MQ_TASK_DLQ = "aipe.task.segment.dlq"
MQ_RESULT = "aipe.result.segment"
MQ_RESULT_DLQ = "aipe.result.segment.dlq"
MQ_CALLBACK = "aipe.callback.segment"


# --------------------------------------------------------------------------
# 优先级策略
# --------------------------------------------------------------------------
# 背景：一个 790 段的大任务入队后，把整个 FIFO 队列占满 88 分钟，
#       期间 79 个小任务（各几段）全部饿死。
# 修法：按 batch 大小给消息打优先级，小任务拿高优先级先跑。
#       RabbitMQ 的 x-max-priority 取 10 档；优先级越高数字越大。
PRIORITY_BY_SIZE = (
    (5, 10),      # <= 5 段
    (20, 8),      # <= 20 段
    (100, 6),     # <= 100 段
    (500, 4),     # <= 500 段
    (None, 2),    # > 500 段
)


def priority_for(batch_size):
    """按 batch 的 segment 数量返回队列优先级。"""
    for limit, prio in PRIORITY_BY_SIZE:
        if limit is None or batch_size <= limit:
            return prio
    return 2


# --------------------------------------------------------------------------
# 容量与超时
# --------------------------------------------------------------------------
MAX_BATCH_SEGMENTS = int(os.environ.get("AIPE_MAX_BATCH_SEGMENTS", "2000"))
CONTEXT_WINDOW = int(os.environ.get("AIPE_CONTEXT_WINDOW", "5"))   # 前后各取几条
LLM_MAX_RETRIES = int(os.environ.get("AIPE_LLM_MAX_RETRIES", "3"))
VISIBILITY_TIMEOUT_SEC = float(os.environ.get("AIPE_VISIBILITY_TIMEOUT", "480"))
MAX_DELIVER_ATTEMPTS = int(os.environ.get("AIPE_MAX_DELIVER_ATTEMPTS", "3"))

# 断流判定：reaper 认为任务卡死的阈值（对应生产里的 1800 秒）
REAPER_STALL_SEC = float(os.environ.get("AIPE_REAPER_STALL_SEC", "1800"))


# --------------------------------------------------------------------------
# 运行模式
# --------------------------------------------------------------------------
# memory   : 零依赖，内存队列 + SQLite，任何人 clone 下来就能跑
# rabbitmq : 生产形态，真 broker + 优先级队列 + DLQ
BROKER_BACKEND = os.environ.get("AIPE_BROKER", "memory")

# mock : 规则式假 LLM，毫秒级，用于演示流水线与调度行为
# openai: 真实 LLM，需要 OPENAI_API_KEY / OPENAI_BASE_URL
LLM_BACKEND = os.environ.get("AIPE_LLM", "mock")

DB_PATH = os.environ.get("AIPE_DB_PATH", "aipe_demo.sqlite3")
