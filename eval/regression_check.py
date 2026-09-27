#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归门：起服务 → 跑公开题库 → 和基线比 → 掉了就退出码非 0。

    python3 eval/regression_check.py                 # 自己起服务（mock 模式）
    python3 eval/regression_check.py --base-url http://localhost:8000
    python3 eval/regression_check.py --only retrieval   # 只跑一类，调试用
    python3 eval/regression_check.py --update-baseline  # 重建基线

## 为什么默认是 mock 模式

CI 里没有 API Key，也不该让每次提交都花钱、都受模型服务波动影响。
所以这扇门跑的是**服务自己的降级/模板路径**（`LLM_API_KEY` 为空），
它确定、离线、和模型无关；比的是"这次改动有没有把这个确定的部分弄坏"。

覆盖到的是那些真正确定的东西：指标口径、检索命中与 top_k、数字核对、
引用卫生、版本过滤、拒答边界、契约 schema、`trace_id` 可追溯。
模型措辞类（data/doc/hybrid 的自然语言部分）本来就不确定，那种涨跌
要看 `EVAL_REPORT.md` 里的 live 轮次，用 `compare_reports.py` 两轮互比。

所以脚本会在服务起来之后**确认 `/api/health` 报的是 `llm_mode=mock`**：
如果这台机器上有 Key（`starter/.env` 里就有），门会跑成 live，
分数不可复现——那还不如不跑，直接报错退出。

## 为什么自己起服务而不是连现成的

连现成的服务，跑出来的分数取决于"你当时起的是哪个版本、哪个环境变量"。
自己起（空闲端口 + 干净的 LLM_* 环境）才谈得上可复现。

只依赖标准库 + 服务自己那份依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(EVAL_DIR)
STARTER = os.path.join(REPO, "starter")
DEFAULT_QUESTION_FILE = os.path.join(EVAL_DIR, "public_questions.jsonl")
DEFAULT_KB = os.path.join(REPO, "knowledge_base")
DEFAULT_OUT = os.path.join(EVAL_DIR, "_regression")
DEFAULT_BASELINE = os.path.join(EVAL_DIR, "baseline.json")

sys.path.insert(0, EVAL_DIR)
import compare_reports as C          # noqa: E402
import run_eval as R                 # noqa: E402

EXIT_OK, EXIT_REGRESSION, EXIT_SETUP = 0, 1, 2


# ======================================================================
# 起服务
# ======================================================================

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def service_python() -> str:
    """起服务要用**装得起 uvicorn** 的解释器。

    不一定是当前这个：这个脚本只用标准库，谁都能跑，而服务在
    `starter/.venv` 里。所以优先用项目自己的虚拟环境，找不到再退回当前解释器
    （CI 里依赖多半装在当前环境）。
    """
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        path = os.path.join(STARTER, ".venv", *rel)
        if os.path.isfile(path):
            return path
    return sys.executable


def can_import(python: str, module: str) -> bool:
    try:
        done = subprocess.run([python, "-c", "import %s" % module],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def clean_env() -> dict:
    """把 `LLM_*` 显式置空。

    不能直接 `del`：`load_dot_env` 只补环境里没有的项，删掉反而会让
    `starter/.env` 里的 Key 生效，门就跑成 live 了（tests/conftest.py 踩过这个）。
    """
    env = dict(os.environ)
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        env[key] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def health_of(base_url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(base_url + "/api/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_health(base_url: str, seconds: float = 90.0) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        health = health_of(base_url)
        if health:
            return health
        time.sleep(0.5)
    return None


class Service:
    """在空闲端口上起服务；退出时一定要 stop()。"""

    def __init__(self, log_path: str, python: str | None = None):
        self.python = python or service_python()
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.log_path = log_path
        self.log = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [self.python, "-m", "uvicorn", "kbqa.server:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=STARTER, env=clean_env(),
            stdout=self.log, stderr=subprocess.STDOUT)

    def log_tail(self, chars: int = 2000) -> str:
        self.log.flush()
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return fh.read()[-chars:]
        except OSError:
            return "（日志读不出来：%s）" % self.log_path

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            self.log.close()
        except OSError:
            pass


# ======================================================================
# 主流程
# ======================================================================

def run_eval_into(base_url: str, questions: str, out_dir: str, kb: str,
                  only: str | None, timeout: float) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    argv = ["--base-url", base_url, "--questions", questions, "--kb", kb,
            "--out", out_dir, "--timeout", str(timeout)]
    if only:
        argv += ["--only", only]
    code = R.main(argv)
    path = os.path.join(out_dir, "report.json")
    if code != 0 or not os.path.isfile(path):
        raise C.BaselineError("评测脚本没有正常收尾（退出码 %s），报告：%s"
                              % (code, path))
    return C.load_report(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="跑一遍公开题库并和基线比；有回归就退出码非 0")
    parser.add_argument("--base-url", default=None,
                        help="评测已有的服务，不给就自己起一个 mock 模式的服务")
    parser.add_argument("--questions", default=DEFAULT_QUESTION_FILE,
                        help="题库 JSONL，默认 eval/public_questions.jsonl")
    parser.add_argument("--kb", default=DEFAULT_KB, help="知识库目录")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE,
                        help="基线文件，默认 eval/baseline.json")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="报告落到哪个目录，默认 eval/_regression/")
    parser.add_argument("--only", default=None,
                        choices=R.CATEGORY_ORDER, help="只跑一个类别")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单次请求超时秒数；mock 模式很快，默认 60")
    parser.add_argument("--tolerance", type=float, default=C.TOLERANCE,
                        help="总分允许掉多少分，默认 %.1f" % C.TOLERANCE)
    parser.add_argument("--update-baseline", action="store_true",
                        help="把这次的报告写成新基线（不判红）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    service = None
    base_url = args.base_url
    try:
        if base_url is None:
            log_path = os.path.join(args.out, "service.log")
            os.makedirs(args.out, exist_ok=True)
            python = service_python()
            if not can_import(python, "uvicorn"):
                print("这个解释器起不了服务（没有 uvicorn）：%s\n"
                      "先装依赖：starter/Makefile 的 `make setup`，"
                      "或把 --base-url 指向一个已经跑着的服务。" % python,
                      file=sys.stderr)
                return EXIT_SETUP
            service = Service(log_path, python=python)
            base_url = service.url
            print("起了个服务：%s（%s，日志 %s）" % (base_url, python, log_path))
            health = wait_health(base_url)
            if health is None:
                print("服务没起来。日志尾部：\n%s" % service.log_tail(),
                      file=sys.stderr)
                return EXIT_SETUP
            mode = health.get("llm_mode")
            print("服务就绪：llm_mode=%s kb_docs=%s valid_sales_rows=%s"
                  % (mode, health.get("kb_docs"), health.get("valid_sales_rows")))
            if mode != "mock":
                print("这台机器上有可用的模型配置，服务跑成了 %s 模式。\n"
                      "回归门要的是可复现：请清掉 LLM_API_KEY（或 starter/.env）"
                      "再跑，或者用 --base-url 指向一个确定的服务。"
                      % mode, file=sys.stderr)
                return EXIT_SETUP
        else:
            health = health_of(base_url)
            if health is None:
                print("连不上 %s" % base_url, file=sys.stderr)
                return EXIT_SETUP
            print("评测已有服务：%s（llm_mode=%s）" % (base_url, health.get("llm_mode")))

        report = run_eval_into(base_url, args.questions, args.out, args.kb,
                               args.only, args.timeout)

        if args.update_baseline:
            C.update_baseline(
                report, args.baseline,
                note="mock 模式的公开题库基线；总分 %s / %s"
                     % (report["total"]["earned"], report["total"]["points"]))
            print("基线已写入 %s" % args.baseline)
            return EXIT_OK

        try:
            baseline = C.load_report(args.baseline)
        except C.BaselineError as exc:
            print("%s\n第一次用先建基线：%s --update-baseline"
                  % (exc, os.path.basename(__file__)), file=sys.stderr)
            return EXIT_SETUP

        verdict = C.compare(baseline, report, tolerance=args.tolerance)
        print()
        print(json.dumps(verdict, ensure_ascii=False, indent=2)
              if args.json else C.render(verdict))
        return EXIT_OK if verdict["ok"] else EXIT_REGRESSION
    except C.BaselineError as exc:
        print("跑不动：%s" % exc, file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        print("\n中断了", file=sys.stderr)
        return EXIT_SETUP
    finally:
        if service is not None:
            service.stop()


if __name__ == "__main__":
    sys.exit(main())
