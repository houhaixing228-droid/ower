#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把两份 `report.json` 摆在一起看：分数涨了还是跌了，哪几道题变了颜色。

第四关的"评测即回归"就落在这个脚本上：

- `regression_check.py` 拿刚跑出来的报告和 `baseline.json` 比，掉了就退出码非 0，
  接进 CI 就能在每次改动后自动看到涨跌；
- 人工复盘时也可以拿两轮 live 报告互比（比如 round10 vs round11），
  一眼看出"修 A 坏 B"。

## 判红的规则

| 规则 | 为什么 |
|---|---|
| 基线里绿的题变红了 | 哪怕总分被别的题补平，也必须红。"修好一题、弄坏一题"是回归最常见的形态 |
| 基线里有、这次没有的题 | 把不会做的题从题库里删掉就能刷分，这种"通过"最危险 |
| 总分比基线低超过 `--tolerance` | 兜底，挡住"一片题各掉一点"的慢性退化 |

由红变绿、分数有变但两头都没全绿、出现新题：都**不**判红，但会写在结论里——
尤其是新题，它没进基线，不重新生成基线的话分数就不可比。

只依赖标准库。

用法：

    python3 eval/compare_reports.py --current report.json          # 比基线
    python3 eval/compare_reports.py --current a.json --baseline b.json
    python3 eval/compare_reports.py --current report.json --update-baseline

退出码：0 = 通过，1 = 回归（或文件读不出来）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

__all__ = [
    "TOLERANCE", "BaselineError", "load_report", "slim", "update_baseline",
    "compare", "render", "main",
]

#: 总分允许的抖动（单位：分）。
#: 取 0.5 是因为一道 1 分题只拿一半分就是 0.5——再小会把"半道题"的
#: 波动当成回归，再大就漏掉真正的退化。真正的单题回归由"由绿变红"那条兜住，
#: 不靠这个数。
TOLERANCE = 0.5

#: 写进基线文件的那一段元信息，纯给人看的
BASELINE_KEY = "_baseline"


class BaselineError(Exception):
    """报告/基线文件读不出来，或者形状不对。"""


# ======================================================================
# 读、写
# ======================================================================

def load_report(path: str) -> dict:
    if not os.path.isfile(path):
        raise BaselineError("找不到报告文件：%s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise BaselineError("%s 不是合法 JSON：%s" % (path, exc))
    except OSError as exc:
        raise BaselineError("%s 读不了：%s" % (path, exc))
    if not isinstance(data, dict):
        raise BaselineError("%s 的顶层不是一个对象" % path)
    if "total" not in data:
        raise BaselineError("%s 里没有 total——这不是 run_eval.py 出的报告" % path)
    if "questions" not in data:
        raise BaselineError("%s 里没有 questions，比不了逐题" % path)
    return data


def slim(report: dict) -> dict:
    """把完整报告削成"只够比较"的基线。

    逐题的 `answer` / `data_evidence` / 每次模型调用的原文都不进基线：
    它们会让文件涨到几百 KB，而且基线要的是"当时哪几道题是全绿的"这个事实，
    不是当时的回答长什么样（想看回答去 `_round*/report.md`）。
    """
    questions = []
    for q in report.get("questions") or []:
        questions.append({
            "id": q.get("id"),
            "category": q.get("category"),
            "points": q.get("points"),
            "earned": q.get("earned"),
            "passed": bool(q.get("passed")),
        })
    out = {
        "generated_at": report.get("generated_at"),
        "base_url": report.get("base_url"),
        "questions_file": report.get("questions_file"),
        "kb_docs_loaded": report.get("kb_docs_loaded"),
        "timeout": report.get("timeout"),
        "only": report.get("only"),
        "total": report.get("total"),
        "per_category": report.get("per_category"),
        "latency_seconds": report.get("latency_seconds"),
        "health": report.get("health"),
        "questions": questions,
    }
    return out


def update_baseline(report: dict, path: str, note: str | None = None,
                    **extra) -> dict:
    """把 `report` 削成基线写到 `path`，返回写下去的那个对象。"""
    data = slim(report)
    meta = {
        "note": note,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_generated_at": report.get("generated_at"),
        "source_base_url": report.get("base_url"),
        "llm_mode": ((report.get("health") or {}).get("llm_mode")
                     if isinstance(report.get("health"), dict) else None),
    }
    meta.update(extra)
    data[BASELINE_KEY] = meta
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return data


# ======================================================================
# 比较
# ======================================================================

def _num(value, default=0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _total(report: dict, key: str) -> float:
    return _num((report.get("total") or {}).get(key))


def _questions(report: dict) -> dict:
    out: dict[str, dict] = {}
    for q in report.get("questions") or []:
        qid = q.get("id")
        if qid is not None:
            out[qid] = q
    return out


def _category_order(baseline: dict, current: dict) -> list:
    """按基线里的顺序排；基线没有的类别接在后面（保持两次跑的顺序可比）。"""
    order = []
    for report in (baseline, current):
        for cat in (report.get("per_category") or {}):
            if cat not in order:
                order.append(cat)
    return order


def _fmt(value: float) -> str:
    return "%.2f" % value


def _names(entries: list, limit: int = 8) -> str:
    shown = [e["id"] for e in entries[:limit]]
    text = "、".join(shown)
    if len(entries) > limit:
        text += " 等 %d 道" % len(entries)
    return text


def compare(baseline: dict, current: dict, tolerance: float = TOLERANCE) -> dict:
    """比两份报告，返回一个结论字典（`ok` 是能不能过门）。"""
    base_q = _questions(baseline)
    cur_q = _questions(current)

    regressed, fixed, changed = [], [], []
    for qid in base_q:
        if qid not in cur_q:
            continue
        before, after = base_q[qid], cur_q[qid]
        b_earned, c_earned = _num(before.get("earned")), _num(after.get("earned"))
        entry = {
            "id": qid,
            "category": after.get("category") or before.get("category"),
            "baseline": b_earned,
            "current": c_earned,
            "delta": round(c_earned - b_earned, 4),
            "points": _num(after.get("points"), _num(before.get("points"))),
        }
        if bool(before.get("passed")) and not bool(after.get("passed")):
            regressed.append(entry)
        elif not bool(before.get("passed")) and bool(after.get("passed")):
            fixed.append(entry)
        elif entry["delta"]:
            changed.append(entry)

    missing = [{"id": qid,
                "category": base_q[qid].get("category"),
                "baseline": _num(base_q[qid].get("earned")),
                "points": _num(base_q[qid].get("points"))}
               for qid in base_q if qid not in cur_q]
    added = [{"id": qid,
              "category": cur_q[qid].get("category"),
              "current": _num(cur_q[qid].get("earned")),
              "points": _num(cur_q[qid].get("points"))}
             for qid in cur_q if qid not in base_q]

    categories = []
    for cat in _category_order(baseline, current):
        b = (baseline.get("per_category") or {}).get(cat) or {}
        c = (current.get("per_category") or {}).get(cat) or {}
        b_earned, c_earned = _num(b.get("earned")), _num(c.get("earned"))
        categories.append({
            "category": cat,
            "points": _num(c.get("points"), _num(b.get("points"))),
            "baseline": b_earned,
            "current": c_earned,
            "delta": round(c_earned - b_earned, 4),
        })

    total = {
        "baseline": _total(baseline, "earned"),
        "current": _total(current, "earned"),
        "points_baseline": _total(baseline, "points"),
        "points_current": _total(current, "points"),
        "ratio_baseline": _total(baseline, "ratio"),
        "ratio_current": _total(current, "ratio"),
        "passed_baseline": int(_total(baseline, "passed")),
        "passed_current": int(_total(current, "passed")),
        "questions_baseline": int(_total(baseline, "questions")),
        "questions_current": int(_total(current, "questions")),
    }
    total["delta"] = round(total["current"] - total["baseline"], 4)

    over_tolerance = total["delta"] < -abs(tolerance)
    ok = not regressed and not missing and not over_tolerance

    reasons: list[str] = []
    if regressed:
        reasons.append("基线里全绿的题有 %d 道变红了：%s"
                       % (len(regressed), _names(regressed)))
    if missing:
        reasons.append("基线里有、这次报告里没有的题 %d 道：%s"
                       "（题库被改小了？删掉不会做的题不算通过）"
                       % (len(missing), _names(missing)))
    if over_tolerance:
        reasons.append("总分掉了 %s 分（%s → %s），超过容差 %s"
                       % (_fmt(-total["delta"]), _fmt(total["baseline"]),
                          _fmt(total["current"]), _fmt(abs(tolerance))))
    if fixed:
        reasons.append("由红变绿 %d 道：%s（%s 分）"
                       % (len(fixed), _names(fixed),
                          _fmt(sum(e["delta"] for e in fixed))))
    if changed:
        reasons.append("分数有变化、但两头都没全绿 %d 道：%s"
                       % (len(changed), _names(changed)))
    if added:
        reasons.append("出现新题 %d 道：%s——它们没进基线，"
                       "要让分数重新可比，得重建基线（--update-baseline）"
                       % (len(added), _names(added)))
    reasons.append("结论：%s（总分 %s → %s，%+.2f；全绿 %d → %d 道）"
                   % ("通过" if ok else "回归",
                      _fmt(total["baseline"]), _fmt(total["current"]),
                      total["delta"], total["passed_baseline"],
                      total["passed_current"]))

    return {
        "ok": ok,
        "tolerance": float(tolerance),
        "total": total,
        "categories": categories,
        "regressed": regressed,
        "fixed": fixed,
        "changed": changed,
        "missing": missing,
        "added": added,
        "reasons": reasons,
    }


# ======================================================================
# 渲染
# ======================================================================

def _table(entries: list, headers: list, row) -> list:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for entry in entries:
        lines.append("| " + " | ".join(row(entry)) + " |")
    lines.append("")
    return lines


def render(verdict: dict) -> str:
    total = verdict["total"]
    lines: list[str] = []
    lines.append("# 评测回归对比")
    lines.append("")
    lines.append("## 结论：%s" % ("通过" if verdict["ok"] else "回归"))
    lines.append("")
    lines.append("- 总分：%s → %s（%+.2f，%s）"
                 % (_fmt(total["baseline"]), _fmt(total["current"]),
                    total["delta"],
                    "涨" if total["delta"] > 0 else
                    ("跌" if total["delta"] < 0 else "持平")))
    lines.append("- 全绿题数：%d → %d 道（题目 %d → %d 道）"
                 % (total["passed_baseline"], total["passed_current"],
                    total["questions_baseline"], total["questions_current"]))
    lines.append("- 容差：±%s 分" % _fmt(verdict["tolerance"]))
    lines.append("")
    lines.append("## 结论一句话")
    lines.append("")
    for line in verdict["reasons"]:
        lines.append("- %s" % line)
    lines.append("")
    lines.append("## 分类别")
    lines.append("")
    lines.extend(_table(
        verdict["categories"],
        ["类别", "基线", "当前", "差值", "满分"],
        lambda c: [c["category"], _fmt(c["baseline"]), _fmt(c["current"]),
                   "%+.2f" % c["delta"], _fmt(c["points"])]))

    def section(title: str, entries: list, key_current: str):
        if not entries:
            return
        lines.append("## %s（%d 道）" % (title, len(entries)))
        lines.append("")
        lines.extend(_table(
            entries, ["题号", "类别", "基线", "当前", "差值"],
            lambda e: [e["id"], str(e["category"]),
                       _fmt(e.get("baseline", 0.0)),
                       _fmt(e.get(key_current, e.get("current", 0.0))),
                       "%+.2f" % e.get("delta", 0.0)]))

    section("由绿变红", verdict["regressed"], "current")
    section("基线里有、这次没有", verdict["missing"], "baseline")
    section("由红变绿", verdict["fixed"], "current")
    section("分数有变但两头都没全绿", verdict["changed"], "current")
    section("新题", verdict["added"], "current")
    return "\n".join(lines)


# ======================================================================
# 命令行
# ======================================================================

def _default_baseline() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把两份评测报告摆在一起看涨跌；掉了就退出码非 0")
    parser.add_argument("--baseline", default=_default_baseline(),
                        help="基线报告，默认是脚本旁边的 baseline.json")
    parser.add_argument("--current", help="这次跑出来的 report.json")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE,
                        help="总分允许掉多少分，默认 %.1f" % TOLERANCE)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    parser.add_argument("--update-baseline", action="store_true",
                        help="把 --current 削成新的基线写到 --baseline，然后退出")
    parser.add_argument("--note", default=None, help="重建基线时写进文件的说明")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.current:
        print("要给 --current 一个报告文件；重建基线时也要给", file=sys.stderr)
        return 1
    try:
        current = load_report(args.current)
    except BaselineError as exc:
        print("读不了 --current：%s" % exc, file=sys.stderr)
        return 1

    if args.update_baseline:
        try:
            update_baseline(current, args.baseline, note=args.note)
        except OSError as exc:
            print("基线写不进去：%s" % exc, file=sys.stderr)
            return 1
        print("基线已写入 %s（总分 %s）"
              % (args.baseline, _fmt(_total(current, "earned"))))
        return 0

    try:
        baseline = load_report(args.baseline)
    except BaselineError as exc:
        print("读不了 --baseline：%s\n"
              "第一次用先建基线：compare_reports.py --current %s --update-baseline"
              % (exc, args.current), file=sys.stderr)
        return 1

    verdict = compare(baseline, current, tolerance=args.tolerance)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print(render(verdict))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
