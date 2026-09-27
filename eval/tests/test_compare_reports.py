#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`compare_reports.py` 的自测：只依赖标准库，不联网、不起服务。

运行：

    cd eval/tests
    python3 -m unittest test_compare_reports -v

要守住三件事，缺一个这套门就是摆设：

1. **总分不能掉**超过容差——这是最直观的那条；
2. **基线里绿的题一道都不许变红**——哪怕总分被别的题补平了。
   只盯总分的门挡不住"修好一题、弄坏一题"，而这正是回归最常出现的样子；
3. **题目不许悄悄消失**——把不会做的题从题库里删掉，总分当然会跌，
   但更危险的是"删了以后总分没跌"（删掉的是本来就没做对的题）。
   所以基线里有、新报告里没有的题，一律判失败。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

EVAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, EVAL_DIR)

import compare_reports as C  # noqa: E402


# ======================================================================
# 造报告：只填 compare 会读的字段
# ======================================================================

def mk_report(rows, base_url="http://127.0.0.1:8000",
              questions_file="/x/public_questions.jsonl"):
    """rows: [(id, category, points, earned), ...]"""
    questions = []
    per_category: dict[str, dict] = {}
    for qid, category, points, earned in rows:
        passed = earned == points
        questions.append({"id": qid, "category": category, "points": points,
                          "earned": earned, "passed": passed})
        entry = per_category.setdefault(
            category, {"points": 0.0, "earned": 0.0, "questions": 0, "passed": 0})
        entry["points"] += points
        entry["earned"] += earned
        entry["questions"] += 1
        entry["passed"] += 1 if passed else 0
    for entry in per_category.values():
        entry["points"] = round(entry["points"], 2)
        entry["earned"] = round(entry["earned"], 2)
        entry["ratio"] = (round(entry["earned"] / entry["points"], 4)
                          if entry["points"] else 0.0)
    total_points = sum(q["points"] for q in questions)
    total_earned = sum(q["earned"] for q in questions)
    return {
        "generated_at": "2026-09-27 21:00:00",
        "base_url": base_url,
        "questions_file": questions_file,
        "total": {
            "points": round(total_points, 2),
            "earned": round(total_earned, 2),
            "ratio": round(total_earned / total_points, 4) if total_points else 0.0,
            "questions": len(questions),
            "passed": sum(1 for q in questions if q["passed"]),
        },
        "per_category": per_category,
        "questions": questions,
    }


BASE_ROWS = [
    ("M01", "metrics", 1.0, 1.0),
    ("R01", "retrieval", 1.0, 1.0),
    ("D01", "data", 2.0, 2.0),
    ("C01", "doc", 2.0, 0.0),
    ("H01", "hybrid", 3.0, 0.0),
]


class CompareCase(unittest.TestCase):
    def setUp(self):
        self.base = mk_report(BASE_ROWS)

    def verdict(self, rows=None, **kw):
        return C.compare(self.base, mk_report(rows or BASE_ROWS), **kw)


# ======================================================================
# 1. 一模一样 = 通过
# ======================================================================

class TestNoChange(CompareCase):
    def test_identical_reports_pass(self):
        v = self.verdict()
        self.assertTrue(v["ok"], v["reasons"])
        self.assertEqual(0.0, v["total"]["delta"])

    def test_identical_reports_have_nothing_to_report(self):
        v = self.verdict()
        self.assertEqual([], v["regressed"])
        self.assertEqual([], v["fixed"])
        self.assertEqual([], v["missing"])
        self.assertEqual([], v["added"])

    def test_category_table_covers_every_category(self):
        v = self.verdict()
        self.assertEqual([c["category"] for c in v["categories"]],
                         ["metrics", "retrieval", "data", "doc", "hybrid"])


# ======================================================================
# 2. 绿的题变红 —— 哪怕总分没变，也必须失败
# ======================================================================

class TestQuestionRegression(CompareCase):
    def test_green_question_turning_red_fails_even_when_total_is_flat(self):
        """修好一题、弄坏一题：总分一样，门必须红。

        这是整套门存在的理由。只看总分的比较器在这里会放行。
        """
        rows = list(BASE_ROWS)
        rows[0] = ("M01", "metrics", 1.0, 0.0)     # 绿 → 红
        rows[4] = ("H01", "hybrid", 3.0, 1.0)      # 红 → 还是红（多了 1 分）
        v = self.verdict(rows)
        self.assertEqual(0.0, v["total"]["delta"], "总分确实没变")
        self.assertFalse(v["ok"], "总分没掉也不能放行：M01 变红了")
        self.assertEqual(["M01"], [q["id"] for q in v["regressed"]])

    def test_regressed_entry_carries_enough_context_to_act_on(self):
        rows = list(BASE_ROWS)
        rows[2] = ("D01", "data", 2.0, 1.0)     # 3 分题只拿到一半 → 由绿变红
        v = self.verdict(rows)
        entry = v["regressed"][0]
        self.assertEqual("D01", entry["id"])
        self.assertEqual("data", entry["category"])
        self.assertEqual(2.0, entry["baseline"])
        self.assertEqual(1.0, entry["current"])

    def test_the_reason_names_the_question(self):
        rows = list(BASE_ROWS)
        rows[0] = ("M01", "metrics", 1.0, 0.0)
        v = self.verdict(rows)
        self.assertTrue(any("M01" in line for line in v["reasons"]),
                        "结论里必须点名是哪道题变红了：%s" % v["reasons"])


# ======================================================================
# 3. 总分掉了：容差之内放过，超出就红
# ======================================================================

class TestTotalTolerance(CompareCase):
    def test_tolerance_is_given_to_the_total_not_to_a_single_question(self):
        """掉 0.5 分之内放过；但这题是由绿变红，单题那条规则仍然要拦。"""
        rows = list(BASE_ROWS)
        rows[1] = ("R01", "retrieval", 1.0, 0.5)
        v = self.verdict(rows, tolerance=0.5)
        self.assertEqual(-0.5, v["total"]["delta"])
        self.assertFalse(v["ok"], "容差是给总分的，不是给单题的")
        self.assertEqual(["R01"], [q["id"] for q in v["regressed"]])

    def test_a_partial_credit_drop_within_tolerance_passes(self):
        """本来就是红的题再少拿点分，只要总分没掉出容差，不该拦。"""
        base = mk_report([("T01", "multi_turn", 3.0, 2.0)])
        cur = mk_report([("T01", "multi_turn", 3.0, 1.0)])
        v = C.compare(base, cur, tolerance=1.0)
        self.assertEqual(-1.0, v["total"]["delta"])
        self.assertEqual([], v["regressed"])
        self.assertTrue(v["ok"], v["reasons"])

    def test_drop_beyond_tolerance_fails(self):
        base = mk_report([("T01", "multi_turn", 6.0, 5.0)])
        cur = mk_report([("T01", "multi_turn", 6.0, 1.0)])
        v = C.compare(base, cur, tolerance=0.5)
        self.assertLess(v["total"]["delta"], -0.5)
        self.assertFalse(v["ok"])

    def test_tolerance_is_configurable(self):
        base = mk_report([("T01", "multi_turn", 3.0, 2.0)])
        cur = mk_report([("T01", "multi_turn", 3.0, 1.0)])
        self.assertFalse(C.compare(base, cur, tolerance=0.1)["ok"])
        self.assertTrue(C.compare(base, cur, tolerance=1.0)["ok"])


# ======================================================================
# 4. 由红变绿 = 好消息，照实报出来
# ======================================================================

class TestImprovements(CompareCase):
    def test_red_to_green_is_reported_as_fixed(self):
        rows = list(BASE_ROWS)
        rows[3] = ("C01", "doc", 2.0, 2.0)
        v = self.verdict(rows)
        self.assertTrue(v["ok"])
        self.assertEqual(["C01"], [q["id"] for q in v["fixed"]])
        self.assertEqual(2.0, v["total"]["delta"])

    def test_partial_credit_change_is_reported_separately_from_fixed(self):
        """T 类题是多轮计分，"从 2/3 到 3/3"和"从 0 到 1/3"都不是翻绿。"""
        base = mk_report([("T01", "multi_turn", 3.0, 2.0)])
        cur = mk_report([("T01", "multi_turn", 3.0, 3.0)])
        v = C.compare(base, cur)
        self.assertEqual(["T01"], [q["id"] for q in v["fixed"]])

        cur2 = mk_report([("T01", "multi_turn", 3.0, 1.0)])
        v2 = C.compare(base, cur2)
        self.assertEqual([], v2["fixed"])
        self.assertEqual([], v2["regressed"])
        self.assertEqual(["T01"], [q["id"] for q in v2["changed"]])
        self.assertEqual("multi_turn", v2["changed"][0]["category"])


# ======================================================================
# 5. 题目集合变了
# ======================================================================

class TestQuestionSetChanges(CompareCase):
    def test_a_question_missing_from_the_current_report_fails_the_gate(self):
        """题库被改小是一种"作弊式通过"：删掉不会做的题，分数看起来更好。

        所以只要基线里有、新报告里没有，无论总分涨没涨，一律判红。
        这里把容差放到 100，就是为了把"总分"那条规则排除掉，单独验这一条。
        """
        base = mk_report(BASE_ROWS[:4] + [("H01", "hybrid", 3.0, 3.0)])
        rows = [r for r in BASE_ROWS if r[0] != "H01"]
        v = C.compare(base, mk_report(rows), tolerance=100.0)
        self.assertEqual(["H01"], [e["id"] for e in v["missing"]])
        self.assertFalse(v["ok"])
        self.assertTrue(any("H01" in line for line in v["reasons"]))

    def test_a_new_question_is_reported_but_does_not_fail(self):
        rows = BASE_ROWS + [("N09", "safety", 2.0, 0.0)]
        v = self.verdict(rows)
        self.assertEqual(["N09"], [e["id"] for e in v["added"]])
        self.assertTrue(v["ok"], "新增题目本身不是回归")

    def test_added_question_is_flagged_in_the_reasons(self):
        rows = BASE_ROWS + [("N09", "safety", 2.0, 2.0)]
        v = self.verdict(rows)
        self.assertTrue(any("N09" in line for line in v["reasons"]),
                        "新题要让基线重算一遍，得说出来：%s" % v["reasons"])


# ======================================================================
# 6. 分类别：说清分数是从哪儿掉的
# ======================================================================

class TestCategories(CompareCase):
    def test_category_delta_shows_where_the_score_moved(self):
        rows = list(BASE_ROWS)
        rows[3] = ("C01", "doc", 2.0, 2.0)          # doc +2
        rows[4] = ("H01", "hybrid", 3.0, 1.0)       # hybrid +1
        v = self.verdict(rows)
        table = {c["category"]: c for c in v["categories"]}
        self.assertEqual(2.0, table["doc"]["delta"])
        self.assertEqual(1.0, table["hybrid"]["delta"])
        self.assertEqual(0.0, table["metrics"]["delta"])

    def test_a_category_present_only_in_one_report_does_not_crash(self):
        cur = mk_report(BASE_ROWS + [("N09", "safety", 2.0, 2.0)])
        v = C.compare(self.base, cur)
        table = {c["category"]: c for c in v["categories"]}
        self.assertEqual(0.0, table["safety"]["baseline"])
        self.assertEqual(2.0, table["safety"]["current"])


# ======================================================================
# 7. 落盘：基线文件要小、可比、可读
# ======================================================================

class TestBaselineFile(CompareCase):
    def test_slim_baseline_round_trips_as_identical(self):
        full = mk_report(BASE_ROWS)
        full["questions"][0]["turns"] = [{"answer": "很长很长的回答" * 50}]
        slim = C.slim(full)
        v = C.compare(slim, full)
        self.assertTrue(v["ok"], v["reasons"])
        self.assertEqual(0.0, v["total"]["delta"])

    def test_slim_drops_the_transcript(self):
        full = mk_report(BASE_ROWS)
        full["questions"][0]["turns"] = [{"answer": "很长很长的回答" * 50,
                                          "checks": [{"name": "x"}]}]
        slim = C.slim(full)
        q = slim["questions"][0]
        self.assertNotIn("turns", q)
        self.assertEqual({"id", "category", "points", "earned", "passed"}, set(q))

    def test_update_baseline_writes_a_json_file(self):
        full = mk_report(BASE_ROWS)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "baseline.json")
            C.update_baseline(full, path, note="mock 模式")
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as fh:
                written = json.load(fh)
            self.assertEqual(0.0, C.compare(written, full)["total"]["delta"])
            self.assertEqual("mock 模式", written["_baseline"]["note"])

    def test_missing_baseline_file_is_an_error_not_a_traceback(self):
        with self.assertRaises(C.BaselineError):
            C.load_report(os.path.join(tempfile.gettempdir(), "no-such-report.json"))


# ======================================================================
# 8. 渲染
# ======================================================================

class TestRender(CompareCase):
    def test_markdown_has_a_row_per_category_and_the_verdict(self):
        rows = list(BASE_ROWS)
        rows[3] = ("C01", "doc", 2.0, 2.0)
        v = self.verdict(rows)
        text = C.render(v)
        for needle in ("metrics", "retrieval", "hybrid", "总分", "C01"):
            self.assertIn(needle, text)

    def test_markdown_of_a_regression_says_so(self):
        rows = list(BASE_ROWS)
        rows[0] = ("M01", "metrics", 1.0, 0.0)
        text = C.render(self.verdict(rows))
        self.assertIn("M01", text)
        self.assertTrue("回归" in text or "失败" in text or "红" in text,
                        "回归的结论要写在最显眼处：%s" % text[:200])


if __name__ == "__main__":
    unittest.main()
