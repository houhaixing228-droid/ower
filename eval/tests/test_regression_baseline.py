#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`eval/baseline.json` 的体检：基线本身也要有人守。

运行：

    cd eval/tests
    python3 -m unittest test_regression_baseline -v

回归门最怕的不是"比错了"，是**基线悄悄失效**：

* 题库加了题、改了分值，基线还停在老的题号上——门会把新题当成"新增"放过去，
  分数也就不可比了；
* 有人手工把 baseline 里的总分改高一点，好让门变绿；
* 有人把基线换成 live 模式跑出来的——下次跑 mock 模式就对不上，
  或者反过来：门变成"看模型心情"的随机测试；
* 有人干脆把完整报告当基线提交，几十万字的回答进 git。

这四条各有一个用例。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

EVAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, EVAL_DIR)

import compare_reports as C          # noqa: E402
import run_eval as R                 # noqa: E402

BASELINE = os.path.join(EVAL_DIR, "baseline.json")
PUBLIC_QUESTIONS = os.path.join(EVAL_DIR, "public_questions.jsonl")
#: 基线要能一眼看完，几十 KB 以内。超了多半是把回答正文一起写进去了。
MAX_BASELINE_BYTES = 64 * 1024


class BaselineCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = C.load_report(BASELINE)
        cls.public = R.load_questions(PUBLIC_QUESTIONS)


class TestBaselineTracksTheQuestionFile(BaselineCase):
    def test_baseline_covers_exactly_the_public_question_set(self):
        base_ids = sorted(q["id"] for q in self.baseline["questions"])
        public_ids = sorted(q["id"] for q in self.public)
        self.assertEqual(public_ids, base_ids,
                         "题库和基线对不上了——改了题库就要重建基线：\n"
                         "    python3 eval/regression_check.py --update-baseline")

    def test_points_match_the_question_file(self):
        """分值改了但基线没重算，总分就不再可比，门会误判。"""
        by_id = {q["id"]: q for q in self.public}
        for q in self.baseline["questions"]:
            self.assertEqual(by_id[q["id"]]["points"], q["points"],
                             "%s 的分值和题库不一致" % q["id"])

    def test_category_matches_the_question_file(self):
        by_id = {q["id"]: q for q in self.public}
        for q in self.baseline["questions"]:
            self.assertEqual(by_id[q["id"]]["category"], q["category"],
                             "%s 的类别和题库不一致" % q["id"])


class TestBaselineAddsUp(BaselineCase):
    def test_total_equals_the_sum_of_the_questions(self):
        earned = round(sum(q["earned"] for q in self.baseline["questions"]), 2)
        points = round(sum(q["points"] for q in self.baseline["questions"]), 2)
        self.assertAlmostEqual(earned, self.baseline["total"]["earned"], places=2,
                              msg="总分和逐题加起来对不上，基线被人改过？")
        self.assertAlmostEqual(points, self.baseline["total"]["points"], places=2)

    def test_per_category_equals_the_sum_of_its_questions(self):
        for cat, entry in self.baseline["per_category"].items():
            rows = [q for q in self.baseline["questions"] if q["category"] == cat]
            self.assertAlmostEqual(round(sum(q["earned"] for q in rows), 2),
                                  entry["earned"], places=2,
                                  msg="分类别 %s 的分和逐题加起来对不上" % cat)

    def test_passed_flag_agrees_with_the_score(self):
        for q in self.baseline["questions"]:
            self.assertEqual(q["passed"], q["earned"] == q["points"],
                             "%s 的 passed 和分值对不上" % q["id"])


class TestBaselineIsReproducible(BaselineCase):
    def test_baseline_was_captured_in_mock_mode(self):
        """门的价值全在"确定"两个字上。

        基线要是 live 模式跑出来的，下次跑 mock 分数必然对不上；
        更糟的是有人照着它把门调成 live，门就变成看模型心情的随机测试。
        """
        mode = (self.baseline.get(C.BASELINE_KEY) or {}).get("llm_mode")
        self.assertEqual("mock", mode,
                         "基线必须是 mock 模式跑出来的（不需要 Key、离线、可复现）")
        health = self.baseline.get("health") or {}
        if health:
            self.assertEqual("mock", health.get("llm_mode"))

    def test_baseline_records_which_question_file_it_belongs_to(self):
        self.assertEqual(os.path.basename(PUBLIC_QUESTIONS),
                         os.path.basename(self.baseline["questions_file"] or ""))

    def test_baseline_has_the_full_question_count(self):
        self.assertEqual(len(self.public), len(self.baseline["questions"]))
        self.assertEqual(len(self.public), self.baseline["total"]["questions"])


class TestBaselineStaysSmall(BaselineCase):
    def test_questions_carry_only_what_compare_reads(self):
        for q in self.baseline["questions"]:
            self.assertEqual({"id", "category", "points", "earned", "passed"},
                             set(q), "基线里不该有回答正文之类的东西")

    def test_file_is_small_enough_to_read(self):
        size = os.path.getsize(BASELINE)
        self.assertLess(size, MAX_BASELINE_BYTES,
                        "基线 %d 字节，太大了：多半是把完整报告当基线提交了" % size)


class TestBaselineIsUsableByTheGate(BaselineCase):
    def test_a_fresh_report_compared_against_itself_passes(self):
        self.assertTrue(C.compare(self.baseline, self.baseline)["ok"])

    def test_baseline_lists_a_category_for_every_question(self):
        for q in self.baseline["questions"]:
            self.assertIn(q["category"], R.CATEGORY_ORDER,
                          "%s 的类别不在评测脚本认得的类别里" % q["id"])


if __name__ == "__main__":
    unittest.main()
