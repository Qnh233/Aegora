# 运行、部署与排障

本文记录本项目的本地运行、HTTP API、企业微信、Docker、Data Ops、日志和评估命令。README 只保留快速入口。

## 环境变量

非敏感默认值在 `config/settings.toml`，环境差异和密钥写入 `.env`。

### App

```env
APP_ENV=local
APP_MODE=api
APP_PRODUCT_ID=aicoin
API_HOST=0.0.0.0
API_PORT=5000
API_WORKERS=1
API_BEARER_TOKEN=
APP_CONSOLE_LOG_ENABLED=false
```

### LLM Gateway

```env
LLM_GATEWAY_API_KEY=replace-me
LLM_GATEWAY_BASE_URL=https://gateway.llmgtw.io/v1
LLM_GATEWAY_CHAT_MODEL=deepseek-v4-pro
LLM_GATEWAY_FAST_MODEL=deepseek-v4-flash
LLM_GATEWAY_TIMEOUT_SECONDS=30
LLM_GATEWAY_ENABLE_THINKING=false
```

请求使用 OpenAI 兼容的 `/chat/completions` 协议。默认关闭 thinking，只持久化最终回答，不保存模型内部推理。

### PostgreSQL 与 Agent Platform

```env
PG_HOST=127.0.0.1
PG_PORT=5432
PG_DATABASE=aegora_runtime
PG_USER=aegora_runtime
PG_PASSWORD=replace-me
PG_SSLMODE=prefer
PG_POOL_MIN_SIZE=1
PG_POOL_MAX_SIZE=10

AGENT_PLATFORM_DATABASE_URL=postgresql://127.0.0.1:55432/agent_platform
```

`AGENT_PLATFORM_DATABASE_URL` 未设置时，runtime resolver 回退 `DATABASE_URL`。

### Embedding

```env
EMBEDDING_PROVIDER=siliconflow
EMBEDDING_API_KEY=replace-me
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=8
EMBEDDING_MAX_RETRIES=3
```

本地模型只作为开发回退，生产默认使用线上 Embedding API。

### MCP Session

```env
MCP_SESSION_IDLE_TTL_SECONDS=900
MCP_SESSION_CLEANUP_INTERVAL_SECONDS=60
MCP_SESSION_MAX_SIZE=100
MCP_DISCOVERY_TTL_SECONDS=300
```

连接级 TTL 可由平台 `mcp_connections` 覆盖。

## FastAPI 服务

启动：

```bash
APP_MODE=api AGENT_LOOP_MODE=planner \
PYTHONPATH=src uvicorn apps.api_app:app --host 0.0.0.0 --port 5000
```

主要接口：

| 接口 | 用途 |
| --- | --- |
| `GET /healthz` | 健康检查 |
| `POST /v1/chat` | 旧客服 Agent 单轮对话 |
| `POST /v1/chat/stream` | 旧客服 Agent SSE 事件流 |
| `POST /v1/gateway/runs` | 配置驱动 release run |
| `POST /v1/webhooks/runs` | webhook 专用 run，按 `agent_id + version + cid + sender_uid` 调用 |
| `POST /v1/gateway/approvals/{approval_id}/decide` | 审批决定 |
| `POST /v1/wecom/aibot` | 企业微信 HTTP 消息入口 |
| `GET /v1/sessions/{session_id}/messages` | 读取最近会话消息 |
| `POST /v1/messages/{assistant_message_id}/feedback` | 保存回答反馈 |

如果配置 `API_BEARER_TOKEN`，请求必须带：

```text
Authorization: Bearer <token>
```

OA/IM 渠道如果启用了平台侧鉴权，需要在 OA 管理后台手动填写鉴权接口地址。这个地址通常由平台网关或 Runner 对外入口提供，必须能被 OA 平台访问；`API_BEARER_TOKEN` 只用于保护 Runner HTTP API，不能替代 OA 平台后台里的鉴权接口配置。

FastAPI endpoint 使用同步函数，FastAPI 在线程池中处理并发请求。同一个 `session_id` 通过 PostgreSQL advisory lock 串行执行，不同 session 可以并发。

## 本地观测

Runner 暴露 Prometheus 指标：

```text
GET /metrics
```

本地启动 Prometheus 和 Grafana：

```bash
docker compose -f docker-compose.observability.yml up -d
```

默认地址：

| 服务 | 地址 |
| --- | --- |
| Runner metrics | `http://127.0.0.1:5000/metrics` |
| Prometheus | `http://127.0.0.1:9090` |
| Grafana | `http://127.0.0.1:3000`，账号密码 `admin/admin` |

Prometheus 本地配置在 `observability/prometheus/prometheus.yml`，默认抓取
`host.docker.internal:5000`。如果 Runner 使用其他端口，需要同步修改该 target。

首期非侵入指标：

| 指标 | 说明 |
| --- | --- |
| `aegora_runtime_http_requests_total` | HTTP 请求计数，按 method/path/status 分组 |
| `aegora_runtime_http_request_duration_seconds` | HTTP 请求耗时直方图 |
| `aegora_runtime_http_requests_in_progress` | 当前进行中的 HTTP 请求数 |

常用 PromQL：

```promql
sum by (path, status) (rate(aegora_runtime_http_requests_total[1m]))
histogram_quantile(0.95, sum by (le, path) (rate(aegora_runtime_http_request_duration_seconds_bucket[5m])))
sum(aegora_runtime_http_requests_in_progress)
```

## Gradio 调试页

启动：

```bash
GRADIO_SERVER_PORT=5000 PYTHONPATH=src python apps/chat_app.py
```

单用户登录：

```env
GRADIO_AUTH_USERNAME=demo
GRADIO_AUTH_PASSWORD=change-me
```

多用户优先：

```env
GRADIO_AUTH_USERS=alice:password1,bob:password2
```

Gradio 只是内部调试入口。公网或长期部署应放在反向代理、SSO 或公司访问控制之后。`GRADIO_SHARE` 默认关闭。

## 企业微信长连接

生产接入企业微信智能机器人建议使用长连接 worker，不需要公网回调 URL：

```bash
APP_MODE=wecom AGENT_LOOP_MODE=planner PYTHONPATH=src python apps/wecom_aibot_app.py
```

必要配置：

```env
WECOM_AIBOT_ID=
WECOM_AIBOT_SECRET=
WECOM_AIBOT_MAX_RECONNECT_ATTEMPTS=-1
```

运行边界：

- 同一个 `WECOM_AIBOT_ID` 只能保持一个活跃长连接。
- 高可用应由进程管理器或 Kubernetes 保持单副本拉起。
- 群聊中的 `@机器人` 前缀会在进入 Agent 前剥离。
- 当前核心 LLM 仍是一次性生成，企业微信只消费通用 SSE/flow 事件做传输层流式。

## Docker

构建：

```bash
docker build -t agentic-rag:latest .
```

启动 API：

```bash
docker run -d \
  --name agentic-rag \
  --restart unless-stopped \
  --env-file .env \
  -p 5000:5000 \
  agentic-rag:latest
```

启动企业微信 worker：

```bash
docker run -d \
  --name agentic-rag-wecom \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  --env-file .env \
  -e APP_MODE=wecom \
  -e AGENT_LOOP_MODE=planner \
  -e PG_HOST=host.docker.internal \
  -e RUN_DB_MIGRATIONS=false \
  agentic-rag:latest
```

部署约束：

- PostgreSQL 必须安装 `pgvector`。
- PostgreSQL 在宿主机时，容器内不能用 `PG_HOST=127.0.0.1` 指代宿主机。
- 容器默认使用线上 Embedding API，不打包本地 torch 模型。
- Runner 自有 PostgreSQL 迁移脚本已存在：首次部署先跑 `PYTHONPATH=src python scripts/db_bootstrap.py`，再跑 `PYTHONPATH=src python scripts/db_migrate.py`；当前 Docker entrypoint 不自动执行迁移。
- 生产建议关闭本地 SQLite/文件日志，用 stdout 和集中日志系统采集。
- `APP_MODE=api` 是默认 HTTP 服务；`APP_MODE=gradio` 仅用于内部体验；`APP_MODE=wecom` 用于长连接 worker；`APP_MODE=data_ops` 用于数据任务。

## Data Ops

Data Ops 是独立运行模式，不处理在线请求。

任务：

| 任务 | 用途 |
| --- | --- |
| `sync-content` | Strapi FAQ/Skill 同步到 PG 检索副本 |
| `embed-pending` | 处理 FAQ/Skill pending embedding |
| `weekly-reflection` | 分析近 N 天消息和反馈，生成报告或 Skill draft |

常驻服务：

```bash
docker run -d \
  --name agentic-rag-data-ops \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  --env-file .env.data-ops \
  -e APP_MODE=data_ops \
  agentic-rag:latest
```

手动触发：

```bash
PYTHONPATH=src python apps/data_ops_app.py sync-content --content all
PYTHONPATH=src python apps/data_ops_app.py embed-pending --content all --retry-failed
PYTHONPATH=src python apps/data_ops_app.py weekly-reflection --days 7 --dry-run
```

真正写 Skill 草稿：

```bash
PYTHONPATH=src python apps/data_ops_app.py weekly-reflection --days 7 --write-skill-drafts
```

## 数据库与内容同步

迁移：

```bash
PYTHONPATH=src python scripts/db_migrate.py
```

检查：

```bash
PYTHONPATH=src python scripts/db_ping.py
PYTHONPATH=src python scripts/db_check_schema.py
PYTHONPATH=src python scripts/check_embeddings.py
```

Strapi schema：

```bash
PYTHONPATH=src:. python scripts/strapi_schema.py check
PYTHONPATH=src:. python scripts/strapi_schema.py create
```

同步内容并生成向量：

```bash
PYTHONPATH=src:. python scripts/sync_strapi_content.py --content all
PYTHONPATH=src python scripts/embed_faqs.py
PYTHONPATH=src python scripts/skills.py embed
```

旧 PG 运行态数据回填 Strapi：

```bash
PYTHONPATH=src:. python scripts/migrate_pg_runtime_to_strapi.py --dry-run
PYTHONPATH=src:. python scripts/migrate_pg_runtime_to_strapi.py
```

## 日志与观测

统一日志入口：

```python
from aegora_runtime.logging import setup_logging

setup_logging(log_dir="logs", console=False)
```

关键事件：

| 事件 | 内容 |
| --- | --- |
| `node_start` | 节点开始 |
| `node_end` | 节点 action、耗时、token delta |
| `node_error` | 异常类型和错误信息 |
| `flow_end` | 总步数、最终状态、route、token usage |
| `llm_call` | 单次模型调用耗时、状态、model、usage |
| `tool_call` | 工具参数、结果摘要、状态、耗时 |
| `approval_*` | 审批创建、通过、拒绝、续跑结果 |
| `api_request_validation_error` | 请求体字段缺失、类型错误、长度越界等 FastAPI/Pydantic 422 明细 |
| `api_http_error_422` | 业务代码主动返回的 422 明细，例如 `stream=true` 或空消息 |
| `api_http_error` | 403、404、409、500 等 HTTP 错误明细，例如 actor/channel/release 鉴权失败 |

默认本地写入 `logs/`。需要 stdout 采集时设置：

```env
APP_CONSOLE_LOG_ENABLED=true
```

按会话查询：

```bash
rg '"session_id": "gradio:user1"' logs/aegora_runtime-*.log
```

按 trace 查询：

```bash
rg 'turn-xxxx' logs/aegora_runtime-*.log
```

查看当天完整 Flow：

```bash
rg 'flow_snapshot' logs/aegora_runtime-$(date +%Y%m%d)-*.log
```

## 评估

评估集在 `evals/`：

| 数据集 | 用途 |
| --- | --- |
| `eval_core_100.jsonl` | 核心 FAQ、路由和回答 |
| `eval_retrieval_sample_300.jsonl` | 检索开发验证 |
| `eval_retrieval_holdout_300.jsonl` | 与开发集去重隔离的 holdout |
| `eval_bad_feedback_100.jsonl` | 低置信度、失败反馈和异常路由 |
| `eval_security_50.jsonl` | 注入、隐私和危险输入 |
| `eval_perf_seed_100.jsonl` | 延迟、吞吐和成本 |
| `eval_skills.jsonl` | Skill 作用域、商业门控和误注入 |

运行检索评估：

```bash
PYTHONPATH=src python scripts/eval_rrf_retrieval.py
```

运行 Agent 评估：

```bash
PYTHONPATH=src python scripts/run_eval.py \
  --agent real \
  --loop-mode planner \
  --limit 20 \
  --output evals/latest_real_agent_core_smoke.json \
  evals/eval_core_100.jsonl
```

运行性能评估：

```bash
PYTHONPATH=src python scripts/run_perf_eval.py \
  --agent real \
  --loop-mode planner \
  --limit 20 \
  --concurrency 3 \
  --output evals/latest_perf_eval_current_c3_limit20.json
```

运行 Skill 评估：

```bash
PYTHONPATH=src python scripts/eval_skills.py
```

对单条未发布 Agent Skill 生成可审计的晋级证据：

```bash
PYTHONPATH=src python scripts/eval_skills.py \
  evals/eval_skills.jsonl \
  --skill-file /path/to/candidate-skill.json \
  --evidence-output /path/to/evaluation.json
```

`evaluation.json` 会自动写入评测集 SHA-256、候选 Skill 内容哈希、指标、门槛和 UTC 评测时间。该文件作为审核输入回填到 `metadata.evaluation`；如果 Skill 内容在评测后发生变化，晋级门禁会因内容哈希不一致而拒绝上线，必须重新评测。

Agent 生成的 Skill 不允许仅靠修改生命周期状态直接上线。`source=agent` 在进入 `active` 前必须同时满足：

- `metadata.evaluation.status = "passed"`
- `metadata.evaluation.dataset` 记录评测集或其版本/哈希
- `metadata.evaluation.content_hash` 与当前 Skill 内容哈希一致
- `metadata.evaluation.metrics` 为非空指标对象
- `metadata.evaluation.evaluated_at` 记录评测时间
- `reviewed_by` 为明确人工审核者

该门禁同时作用于本地 `scripts/skills.py publish` 与 Strapi 内容同步，避免 CMS 路径绕过 Runtime 治理。

## 常用排障

| 问题 | 检查 |
| --- | --- |
| API 无法启动 | `.env`、依赖、端口占用、`PYTHONPATH=src` |
| 数据库连接失败 | `scripts/db_ping.py`、`PG_HOST/PG_PORT/PG_SSLMODE` |
| 检索没有向量结果 | `scripts/check_embeddings.py`、Embedding API key、`faq_embeddings.status` |
| Gateway 无工具 | release tools、`tools.status`、actor/channel 权限、resolver 日志 |
| MCP 未接入 | 平台是否走 Gateway Runner，而不是旧 Debug Runner |
| 审批后未续跑 | approval id 状态、`runner_approval_requests`、工具是否仍 active |
| 企业微信断线 | Bot 是否多副本、secret、网络和 SDK reconnect 日志 |

关闭本地调试页：

```bash
pkill -f 'apps/chat_app.py'
```
