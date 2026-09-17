# -*- coding: utf-8 -*-
"""AIPE 架构最小复现 —— 演示包。

模块划分对应生产里的部署单元：
    config   策略与参数
    broker   队列抽象（内存 / RabbitMQ）
    segment  上下文窗口 + 标签保真
    store    持久化与事务边界
    llm      LLM 客户端（mock / OpenAI 兼容）
    pipeline Producer / Consumer / Writer / Callback 四角色
"""

__version__ = "0.1.0"
