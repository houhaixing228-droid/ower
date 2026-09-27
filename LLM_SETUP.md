# LLM 接入说明

## 1. 用了什么

- 厂商/模型：DeepSeek（`deepseek-chat` 与 `deepseek-flash` 都跑过，评测目标是 `deepseek-flash`）；
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

代理日志每条是一行 JSON（`llm_traffic.jsonl`，一次请求一行，响应体也记在同一行里）。
认证头由工具自己打码（只记长度，不记值）。下面是日志里一条记录的**字段结构**
（照 `eval/llm_gateway.py` 的 `_make_record` 字段列出，内容为示意，已截短）：

```json
{"ts":"2026-09-27T20:11:03.412+08:00","monotonic":418855.271,"scenario":"proxy",
 "method":"POST","path":"/chat/completions","query":"",
 "headers":{"Authorization":"<redacted len=51>","Content-Type":"application/json",
   "User-Agent":"python-httpx/0.27.0","Content-Length":"1130"},
 "authorization":{"present":true,"scheme":"Bearer","value_length":51,"token_length":45,
   "matches_expected":false},
 "body":{"model":"deepseek-flash","temperature":0,
   "messages":[{"role":"system","content":"你是合味餐饮的经营助手……（约 1.8k 字）"},
               {"role":"user","content":"7 月 S02 的净营业额是多少？"}],
   "tools":[{"type":"function","function":{"name":"query_metrics",
     "description":"按口径查询区间经营指标","parameters":{"type":"object",
     "properties":{"metric":{"type":"string"},"start":{"type":"string"},
     "end":{"type":"string"},"store_id":{"type":"string"}}}}}]},
 "body_text":"","response_status":200,"response_note":null,
 "response_body":{"id":"chatcmpl-…","object":"chat.completion",
   "choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":null,
     "reasoning_content":"…（截短）…","tool_calls":[{"id":"call_00_9fQ…","type":"function",
       "function":{"name":"query_metrics",
         "arguments":"{\"metric\":\"net_revenue\",\"start\":\"2026-07-01\",\"end\":\"2026-07-31\",\"store_id\":\"S02\"}"}}]}}],
   "usage":{"prompt_tokens":2134,"completion_tokens":118,"total_tokens":2252}},
 "latency_ms":2462.5,
 "usage":{"prompt_tokens":2134,"completion_tokens":118,"total_tokens":2252}}
```

从这一条能确认三件事：请求发到的是 `LLM_BASE_URL` 原样拼 `/chat/completions`、
`body.model` 来自 `LLM_MODEL`、Key 走 `Authorization: Bearer`（工具只记长度）。
思考内容在 `reasoning_content` 里，下一轮回传时原样带上，不进任何用户可见字段。

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

走的就是 OpenAI 兼容协议，所以贴 `eval/llm_gateway.py preflight` 的**实测输出**。

```bash
python eval/llm_gateway.py preflight --service-url http://localhost:8000
```

它会起一个本机假模型（不联网、不需要 Key），打印三个环境变量；**用这三个变量重启服务之后**
它驱动 `/api/chat`，跑完 16 个场景 × 2 个问题。下面是本次提交前实测的完整输出
（端口每次都不一样；机器可读版在 `eval/_preflight/preflight_report.json`）：

```
==============================================================================
预检假模型已启动：http://127.0.0.1:50119/ds-gw
它只提供 POST http://127.0.0.1:50119/ds-gw/chat/completions，其余任何路径都会返回 404 并被记下来。

请用下面三个环境变量重启你的服务（模型名是故意取的怪名字，写死模型名会被查出来）：

  export LLM_BASE_URL=http://127.0.0.1:50119/ds-gw
  export LLM_API_KEY=preflight-key-3b9c1f
  export LLM_MODEL=preflight-model-7f3a
==============================================================================

开始检查 http://127.0.0.1:8000 ……
  [normal] 你们的退款规则是怎么规定的？ → HTTP 200，0.25 秒
  [normal] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.16 秒
  [thinking_starved] 你们的退款规则是怎么规定的？ → HTTP 200，0.23 秒
  [thinking_starved] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.17 秒
  [empty_content] 你们的退款规则是怎么规定的？ → HTTP 200，0.61 秒
  [empty_content] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.62 秒
  [json_empty] … HTTP 200，0.16 秒 / 0.17 秒
  [bad_tool_args] … HTTP 200，0.08 秒 / 0.11 秒
  [content_filter] … HTTP 200，0.05 秒 / 0.08 秒
  [insufficient_resource] … HTTP 200，0.59 秒 / 0.62 秒
  [aborted] … HTTP 200，0.05 秒 / 0.05 秒
  [http_401] … HTTP 200，0.06 秒 / 0.06 秒
  [http_402] … HTTP 200，0.06 秒 / 0.06 秒
  [http_422] … HTTP 200，0.08 秒 / 0.08 秒
  [http_429] … HTTP 200，0.62 秒 / 0.61 秒
  [http_500] … HTTP 200，0.59 秒 / 0.66 秒
  [http_503] … HTTP 200，0.61 秒 / 0.61 秒
  [slow] … HTTP 200，18.23 秒 / 18.28 秒
  [hang] … HTTP 200，120.06 秒 / 120.06 秒

编号  检查项                                                            结果
----------------------------------------------------------------------------------
P1    服务确实把请求发到了注入的 LLM_BASE_URL（含路径前缀）             通过
P2    请求里的 model 等于注入的 LLM_MODEL                               通过
P3    注入的 Key 以 Authorization: Bearer 发送                          通过
P4    只用了 DeepSeek 文档列出的顶层参数                                通过
P5    max_tokens 不设，或不小于 2048                                    通过
P6    没有访问 {prefix}/chat/completions 之外的任何路径                 通过
P7    工具定义规范，且每一个工具调用都以 role=tool + tool_call_id 回传  通过
P8    每个场景下 /api/chat 都返回 HTTP 200 与字段完整的合法 JSON        通过
P9    模型不可用时给出结构化 refusal，answer 从不是空串                 通过
P10   思考内容没有漏进 answer / citations / data_evidence               通过
P11   /api/chat 在时限内返回（含长时间无响应的场景）                    通过
P12   注入环境变量后 /api/health 报告 llm_mode = live                   通过
P13   多轮工具调用之间 reasoning_content 原样回传（没有触发 400）       通过
P14   保持连接的空行与 SSE 注释没有把服务弄坏                           通过

预检通过：在 OpenAI 兼容这条路线上，我们能原样接上你的服务。
```

**14 项全过，没有 FAIL、没有 SKIP。** 逐项的原始证据（每个场景的请求次数、
工具调用数、实测耗时、看到的参数名）都在 `eval/_preflight/preflight_report.md` 里，
下面几条是这份报告里值得单独看一眼的：

| 检查项 | 实测到的东西 |
|---|---|
| P1 / P6 | 60 次请求全部落在 `POST /ds-gw/chat/completions`，没有碰任何别的路径（地址是原样拼 `/chat/completions`，没有自己补 `/v1`、没有截域名） |
| P2 / P3 | 60 次请求的 `model` 都是注入的 `preflight-model-7f3a`，Key 都走 `Authorization: Bearer` |
| P5 | `max_tokens` 只出现过 `4096` 一个值 |
| P7 | 44 个工具调用，44 个 `role: "tool"` + `tool_call_id` 回传，数量对得上 |
| P9 | 12 个"模型不可用"场景下都是结构化 `refusal`，`answer` 从不是空串，也没有把失败的模型输出当回答 |
| P10 | 18 个场景的思考标记（`RSN-…`）一次都没有出现在 `answer` / `citations` / `data_evidence` 里 |
| P11 | 最慢是 `hang` 场景的 120.06 秒，仍在 180 秒时限内 |
| P13 | 18 次多轮请求都原样回传了 `reasoning_content`，没有触发 400 |
| P14 | `: keep-alive` 注释与正文前的空行都被跳过，`slow` 场景照常作答 |

## 8. 已知限制

- `deepseek-chat` 与 `deepseek-flash` 行为有差异：flash 偶尔把工具调用标记
  写进正文（DSML 标记），我们已做检测、补轮提示与剥离三层兜底（DEBUG_LOG D12/D16），
  但极端情况下仍可能损失一轮重试的机会。
- 思考内容（reasoning_content）只用于回传给模型（P13 要求），不进任何用户可见字段；
  trace 里的 llm_calls 记录的是请求参数与用量摘要，不是完整思考过程。
