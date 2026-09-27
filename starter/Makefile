# 经营看板 + 问答服务
# 换数据或知识库：make rebuild DATA_DIR=/path/data KB_DIR=/path/kb
#
# Windows 上加 PY 覆盖解释器路径：
#   make test PY=.venv/Scripts/python.exe

PY ?= .venv/bin/python
PY_ABS := $(abspath $(PY))
PORT ?= 8000
export DATA_DIR KB_DIR VAR_DIR LLM_BASE_URL LLM_API_KEY LLM_MODEL

.PHONY: setup rebuild run test test-eval eval eval-baseline clean

setup:          ## 建虚拟环境并装依赖
	uv venv --python 3.12
	uv pip install --python $(PY) -r requirements.txt

rebuild:        ## 从 DATA_DIR 与 KB_DIR 重建清洗表和检索索引
	$(PY) -m kbqa.rebuild

run:            ## 启动服务，默认 8000 端口
	$(PY) -m uvicorn kbqa.server:app --host 127.0.0.1 --port $(PORT)

test:           ## 跑测试
	$(PY) -m pytest tests -q

test-eval:      ## 跑评测脚本自己的测试（不需要服务、不需要 Key）
	cd ../eval/tests && $(PY_ABS) -m unittest test_run_eval test_run_eval_public \
		test_compare_reports test_regression_baseline test_extra_questions

eval:           ## 评测即回归：自己起服务跑公开题库，和 eval/baseline.json 比；有回归退出码非 0
	$(PY_ABS) ../eval/regression_check.py

eval-baseline:  ## 重建回归基线（题库或分值改了以后要跑一次）
	$(PY_ABS) ../eval/regression_check.py --update-baseline

clean:          ## 删掉清洗表
	rm -rf var
