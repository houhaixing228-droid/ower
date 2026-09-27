# LLM 接入说明

## 1. 用了什么

- 厂商/模型：开发期用 DeepSeek（模型 `deepseek-chat`），评测目标 `deepseek-flash`；
- 协议：**OpenAI 兼容 Chat Completions**（契约 7.1 推荐路线）；
- SDK：`httpx`（Python），未用任何厂商 SDK，只发标准 `/chat/completions` 请求；
- 版本见 `starter/requirements.txt`（fastapi / uvicorn / httpx / pytest）。

## 2. 配置从哪里读

| 配置 | 环境变量 | 默认值 | 读取位置 |
|---|---|---|---|
| 接口地址 | `LLM_BASE_URL` | 无（缺省走 mock） | 进程环境变量 / `starter/.env` |
| Key | `LLM_API_KEY` | 无（缺省走 mock） | 同上，**不入库**（.gitignore 已排除） |
| 模型名 | `LLM_MODEL` | 无（缺省走 mock） | 同上 |

`starter/kbqa/llm.py` 的 `load_dot_env` 只**补**环境里没有的项，环境变量优先；
服务启动时读一次。地址原样使用（含路径前缀），请求发到 `{LLM_BASE_URL}/chat/completions`，
不自己补 `/v1`、不截域名。

## 3. 怎么换成你们的

三步，不用改任何代码：

```bash
export LLM_BASE_URL=https://api.deepseek.com      # 或你们自己的兼容端点
export LLM_API_KEY=<你们的 Key>
export LLM_MODEL=deepseek-flash
```

然后重启服务（`make run` 或 uvicorn 命令）。**不需要重新执行重建命令**——
`make rebuild` 只管清洗表和检索索引，与大模型无关。

判定：`LLM_API_KEY` 非空即 `live` 模式（`/api/health` 的 `llm_mode` 如实报告）。

## 4. 怎么看到发给模型的请求

两种：

- **代理**（推荐，即你们评测时的方式）：

  ```bash
  python eval/llm_gateway.py proxy --upstream https://api.deepseek.com --log llm_traffic.jsonl
  # 用它打印的 LLM_BASE_URL 重启服务，每一条请求/响应都记进 llm_traffic.jsonl
  ```

- **trace**：每次 `/api/chat` 返回 `trace_id`，`GET /api/trace/{trace_id}` 里有
  每一步（guard/plan/tool/llm 调用）的耗时与参数；llm_calls 段含完整 messages
  与 tool_calls 摘要。看板前端的调试面板就是可视化这个。

## 5. 没有 Key 时会怎样

服务正常启动（退出码 0），`/api/health` 返回 `llm_mode: "mock"`。

| 接口 | 行为 |
|---|---|
| `/api/health` | 200，`llm_mode: "mock"` |
| `/api/metrics/*` `/api/retrieve` `/api/data_quality` | 完全正常（不依赖模型） |
| `/api/chat` | 200 + 合法 JSON，走本地模板回答（mock 模式），无 Key 也能答指标类问题 |

## 6. 依赖与安装

`make setup` 一条命令（Python 3.12 venv + requirements.txt）。无模型文件下载，
首次启动约 3–5 秒（含建索引缓存）。检索不依赖任何向量服务（BM25，本地索引）。

## 7. 自测结果

`eval/llm_gateway.py preflight` 输出（16 场景全跑，P1–P14）：

```
预检假模型已启动：http://127.0.0.1:53999/ds-gw

  export LLM_BASE_URL=http://127.0.0.1:53999/ds-gw
  export LLM_API_KEY=preflight-key-3b9c1f
  export LLM_MODEL=preflight-model-7f3a
```

（下方为最终提交前实测的 PASS/FAIL 表，见本仓库 `eval/_preflight/preflight_report.md`。
本文件在评测进行中生成，若该表有任何 FAIL 项，以报告文件为准并在第 8 节说明。）

## 8. 已知限制

- `deepseek-chat` 与 `deepseek-flash` 行为有差异：flash 偶尔把工具调用标记
  写进正文（DSML 标记），我们已做检测、补轮提示与剥离三层兜底（DEBUG_LOG D12/D16），
  但极端情况下仍可能损失一轮重试的机会。
- 思考内容（reasoning_content）只用于回传给模型（P13 要求），不进任何用户可见字段；
  trace 里的 llm_calls 记录的是请求参数与用量摘要，不是完整思考过程。
