# -*- coding: utf-8 -*-
"""LLM 客户端。

默认是 mock：用规则模拟"后编辑"的判定逻辑，毫秒级返回，方便任何人
clone 下来直接跑通整条流水线。切到 openai 就变成真调用，
Prompt 组装方式完全一致，便于对比。
"""

import json
import os
import random
import re
import time

from .segment import check_tags


class MockLLM(object):
    """规则式假模型。

    模拟真实后编辑行为，并故意保留几种"不完美"，好让 demo 能演示
    失败重试与兜底路径：
      - 约 6% 概率直接抛超时（触发 consumer 重试 / DLQ）
      - 检出标签缺损时判 N（触发重译与人工复核标记）
    """

    def __init__(self, latency_ms=15, timeout_rate=0.06, seed=20260518):
        self.latency_ms = latency_ms
        self.timeout_rate = timeout_rate
        self._rnd = random.Random(seed)

    def post_edit(self, segment, context, guidelines):
        started = time.time()
        time.sleep(self.latency_ms / 1000.0)

        if self._rnd.random() < self.timeout_rate:
            raise TimeoutError("mock LLM call exceeded %d ms" % (self.latency_ms * 10))

        source = segment.get("source", "")
        mt = segment.get("mt", "") or ""

        # 判定 1：标签对不上 -> 必须重译，不能回写
        tag_ok, tag_detail = check_tags(source, mt)

        # 判定 2：术语表命中（演示 DNT / Term 两类约束的注入效果）
        dnt = _parse_guideline_lines(guidelines.get("dnt"))
        term = _parse_guideline_lines(guidelines.get("term"))
        # 注意：_parse_guideline_lines 返回的是 (source, target, note) 三元组，
        # 这里必须解包，否则会把元组当成词去匹配字符串。
        dnt_violation = [src for src, _tgt, _n in dnt if src and src in source and src not in mt]
        term_applied = [src for src, _tgt, _n in term if src and src in source]

        if not mt.strip():
            final_text = "【重译】" + _fake_translate(source)
            evaluation = "N"
            comment = "MT empty; produced a fresh translation."
        elif not tag_ok:
            final_text = _fake_translate(source, keep_tags_from=mt)
            evaluation = "N"
            comment = "Inline tag mismatch (%s); retranslated." % _first_bad(tag_detail)
        elif dnt_violation:
            final_text = mt
            evaluation = "N"
            comment = "DNT term changed: %s" % ", ".join(dnt_violation)
        else:
            final_text = mt.strip()
            evaluation = "Y"
            comment = ("Term applied: %s." % ", ".join(term_applied)) if term_applied else ""

        return {
            "final_translation": final_text,
            "evaluation_result": evaluation,
            "comment": comment,
            "tag_ok": tag_ok,
            "tag_detail": tag_detail,
            "llm_latency_ms": int((time.time() - started) * 1000),
        }


def _parse_guideline_lines(text):
    """把术语表 / 禁改词片段解析成条目。

    两种写法都支持：
        word
        source => target
    """
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            src, tgt = line.split("=>", 1)
            out.append((src.strip(), tgt.strip(), None))
        else:
            out.append((line, None, None))
    return out


def _first_bad(detail):
    for k in ("curly_set_ok", "printf_multiset_ok", "xml_balanced_ok", "escape_form_ok"):
        if not detail.get(k, True):
            return k
    return "unknown"


def _fake_translate(source, keep_tags_from=None):
    """生成一个伪译文：保留原文的标签骨架，把词替换成占位形态。

    目的不是"翻得像"，而是让标签校验这一关有真实的数据可跑。
    """
    tags = re.findall(r"(\{[0-9]+\}|<[^>]+>|%(?:\d+\$)?[sdf])", keep_tags_from or source)
    body = re.sub(r"\{[0-9]+\}|<[^>]+>|%(?:\d+\$)?[sdf]", " ", source).strip()
    head = " ".join(tags)
    core = "「" + " ".join(reversed(body.split())) + "」"
    return (head + " " + core).strip() if tags else core


class OpenAICompatLLM(object):
    """真实调用：任何 OpenAI 兼容端点都能接（含 vLLM / SGLang / 网关）。"""

    def __init__(self, model=None, base_url=None, api_key=None, temperature=0.05):
        from openai import OpenAI  # 延迟导入

        self.model = model or os.environ.get("AIPE_LLM_MODEL", "gpt-4o-mini")
        # 温度取得很低：后编辑是"判断题"而不是"创作题"，
        # 同一个 segment 两次调用必须给出一致结果，否则评测没法做。
        self.temperature = temperature
        self._client = OpenAI(
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            api_key=api_key or os.environ.get("OPENAI_API_KEY") or "EMPTY",
        )

    def post_edit(self, segment, context, guidelines):
        from .segment import build_prompt_messages

        started = time.time()
        messages = build_prompt_messages(segment, context, guidelines)
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        final_text = data.get("final_translation", "")
        tag_ok, tag_detail = check_tags(segment.get("source", ""), final_text)
        return {
            "final_translation": final_text,
            "evaluation_result": data.get("evaluation_result", "N"),
            "comment": data.get("comment", ""),
            "tag_ok": tag_ok,
            "tag_detail": tag_detail,
            "llm_latency_ms": int((time.time() - started) * 1000),
        }


def build_llm(backend=None):
    from . import config
    backend = backend or config.LLM_BACKEND
    if backend == "openai":
        return OpenAICompatLLM()
    return MockLLM()
