# 多网关配置驱动 Runner

本文记录真实 Runner 的运行时设计。README 只保留主线介绍，本文件承接 Gateway、Resolver、Core Runner、Tool Adapter、MCP、Skill scope 和审批机制等技术细节。

## 设计原则

- 请求只携带引用和身份：`agent_id`、`release_id`、`actor_id`、`channel`、`message`。
- 工具权限只来自数据库 release context、工具状态、角色权限和运行时 scope，不接受请求侧工具事实。
- 注册只表示 Runner 具备能力，不表示任何 Agent 可用。
- Core Runner 只执行 resolver 注入的 active tools。
- 安全收紧策略必须可实时生效，例如 `requires_approval`、`side_effect_level`、`timeout_ms`。

## HTTP Release Run

入口：

```text
POST /v1/gateway/runs
POST /v1/webhooks/runs
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `agent_id` | Agent Platform 中的 Agent 引用 |
| `release_id` | 已发布 release 引用 |
| `actor_id` | 当前调用身份 |
| `channel` | 调用渠道，例如 `web_console` |
| `message` | 用户本轮消息 |
| `session_id` | 可选。未传时 Runner 生成 |
| `metadata` | 可选。只作为上下文元数据，不承载权限事实 |
| `stream` | 预留，当前默认 `false` |

Webhook 专用请求字段：

| 字段 | 说明 |
| --- | --- |
| `agent_id` | Agent Platform 中的 Agent 引用 |
| `version` | 版本号，不直接暴露 `release_id` |
| `channel` | 渠道，例如 `oa` |
| `cid` | 外部会话窗口或群聊 ID，Runner 内部作为 `session_id` |
| `sender_uid` | 当前发言人 UID，Runner 内部同时作为 `actor_id` 和 `user_id` |
| `message` | 用户本轮消息 |
| `reply_url` | 可选。异步回调地址 |
| `event_id` / `mid` | 可选。原平台事件标识 |
| `metadata` | 可选。透传元数据 |
| `stream` | 预留，当前默认 `false` |

响应包含：

```text
run_id / session_id / agent_id / release_id / answer / route / status /
trace_id / tool_observations / approval_requests / model_usage / flow
```

鉴权分两层：

- HTTP 层沿用 `API_BEARER_TOKEN`。
- 业务层由 resolver 查询数据库判断 release、agent、actor、channel、tool scope。

当前 `gateway/runs` 的会话语义：

- `session_id` 表示会话窗口或群聊容器。
- `sender_uid` 表示当前发言人；缺失时回退 `actor_id`。
- Runner 会把本轮对话写入 `chat_messages`，后续从同一 `session_id` 的窗口上下文，以及同一 `(session_id, user_id)` 的发言人上下文里加载短期上下文。

当前 `webhooks/runs` 的会话语义：

- `cid` 直接映射为 `session_id`。
- `sender_uid` 直接映射为 `user_id`，并在首期兼作 resolver 的 `actor_id`。
- 该接口专门给 OA/IM/Webhook 接入使用，不再混入平台调试入参。

## Runtime Context Resolver

入口文件：

```text
src/aegora_runtime/runtime_context.py
```

数据库配置：

```text
AGENT_PLATFORM_DATABASE_URL=postgresql://127.0.0.1:55432/agent_platform
```

未配置时回退 `DATABASE_URL`。

Resolver 当前做这些事：

1. 校验 release 必须是 `published`。
2. 校验 agent enabled。
3. 校验 actor active。
4. 校验 release 可见度：`visibility=public` 时任意 active actor 可运行；非 public 时仅 owner 或 `platform_admin` 可运行。
5. 校验 channel 在 release 或 agent 允许范围内。
6. 读取 `agent_releases.config_json` 中的 system prompt、model、tools。
7. 用当前 `tools.status='active'` 过滤 release tools。
8. 用当前工具治理字段覆盖 release 中的安全策略。
9. 解析 `mcp_connections`，给 MCP 工具补齐连接配置。
10. 解析 `runtime.load_skill` scope，预生成授权 Skill 摘要索引。

输出的 runtime context 是 Core Runner 唯一可信配置源。

`agent_releases.visibility` 是运行时可见度事实源。兼容旧数据时，Runner 会回退读取
`config_json.agent.visibility`，再默认 `private`。`published` 只表示版本可运行，不等于对所有用户公开。

## Configured Runner Core

入口文件：

```text
src/aegora_runtime/configured_runner.py
```

Core Runner 的 prompt 来源分两层：

- 业务 system prompt：来自 release 配置。
- 固定执行协议：Runner 追加 JSON 决策协议、路由枚举、工具调用约束和 active tool schema。

这意味着 `configured_runner.py` 里的提示词不是业务人格提示词，而是执行协议。业务提示词必须来自 resolver 注入。

短期上下文注入当前不单独建 memory 表，而是直接复用 `chat_messages`：

- `history`：调用方显式传入的历史，当前 gateway 默认不传。
- `session_context`：同一 `session_id` 最近窗口上下文。
- `session_user_context`：同一 `(session_id, user_id)` 最近发言人上下文；若与 `session_context` 重复，则按 `message_key` 去重。

终态 route：

| route | 说明 |
| --- | --- |
| `answer` | 配置驱动 Runner 的通用终态回答 |
| `faq_answer` | 兼容旧客服 FAQ 回答 |
| `chat` | 兼容旧自然对话 |
| `clarify` | 需要用户补充信息 |
| `handoff` | 转人工 |
| `tool_call` | 中间态，要求执行工具 |
| `approval_required` | 工具执行前需要审批 |

未注入工具的处理：

- LLM 如果请求未注入工具，Runner 拒绝执行。
- observation 记录 `unknown_tool_requested`。
- 工具不会落到 adapter 层。

## Registry、Adapter 与 RuntimeToolExecutor

核心文件：

```text
src/aegora_runtime/registry.py
src/aegora_runtime/tools.py
src/aegora_runtime/tool_adapters.py
src/aegora_runtime/local_mcp_server.py
```

注册边界：

- `src/aegora_runtime/registry.py` 是唯一注册事实源，业务工具使用 `@registry.tool`，业务知识能力使用 `@registry.skill`。
- `src/aegora_runtime/tool_adapters.py` 不再维护第二套本地工具注册表，只把 runtime context 中已注入的工具 manifest 转成运行期 `ToolSpec`。
- `src/aegora_runtime/tools.py` 中的 `RuntimeToolExecutor` 是本轮工具执行表，负责 `run/run_many/catalog/hooks/retry`，不负责工具发现和授权。
- `src/aegora_runtime/local_aicoin_tools.py` 只保留 AiCoin 本地工具和旧 `/v1/chat` 的兼容适配，从统一 registry 读取已注册工具。

真实执行链路：

```text
ExecuteToolNode
-> AgentDependencies.run_tool / run_tools
-> ApprovalGate
-> RuntimeToolExecutor.run / run_many
-> ToolSpec.handler
-> tool_adapters.execute_tool_manifest
-> local: @registry.tool handler
   mcp: FastMCP client.call_tool
```

工具分类：

| 一级 | 用途 |
| --- | --- |
| `runtime` | 通用运行时能力，例如 Skill 加载、时间、计算器、受控搜索 |
| `support` | 客服/业务支持能力，例如 FAQ 查询、转人工、会员业务工具 |
| `integration` | 外部系统或第三方服务能力，例如地图、CRM、工单、支付 |
| `workflow_agent` | 由另一个工作流或 Agent 执行的能力 |

二级分类建议用 `provider_key` 或 `group_key`：

- 同一个 MCP 服务下的工具使用同一个 `provider_key`。
- 同一业务域下的客服工具使用同一个 `group_key`。
- 前端展示优先按 `category -> provider_key/group_key -> tool` 分组。

## runner_tool_id 约定

`tool_id` 是平台治理和授权对象，`runner_tool_id` 是 Runner 执行通道。

| 类型 | 格式 |
| --- | --- |
| 本地能力 | `local.<registered_name>`，resolver 可归一化为本地 MCP stdio |
| MCP HTTP | `mcp+http://host:port/mcp` |
| MCP stdio | `mcp+stdio://python?arg=/abs/server.py&arg=--flag&cwd=/abs/workdir&env=API_KEY,DATABASE_URL` |
| 普通 HTTP | 平台工具配置中的 HTTP endpoint |
| workflow agent | 平台工具配置中的 workflow/agent 引用 |

MCP 工具实际名称放在 `runner_name`。HTTP bearer 可通过 `bearer_env=MCP_TOKEN` 从 Runner 环境变量读取。

## MCP 连接配置

平台侧使用 `mcp_connections` 维护连接事实源，`tools.mcp_connection_id` 只表达工具归属。

连接配置建议字段：

| 字段 | 说明 |
| --- | --- |
| `connection_id` | 稳定 ID |
| `name` | 前端展示名 |
| `transport` | `stdio` 或 `http` |
| `url` | HTTP MCP endpoint |
| `command` | stdio 命令，例如 `python` |
| `args` | stdio 参数列表 |
| `cwd` | stdio 工作目录 |
| `env_keys` | 允许透传的环境变量名 |
| `bearer_env` | HTTP bearer token 对应环境变量 |
| `config_hash` | 连接配置 hash |
| `config_version` | 配置版本 |
| `credential_version` | 凭证版本 |
| `idle_ttl_seconds` | 空闲回收时间 |
| `discovery_ttl_seconds` | list tools 缓存时间 |
| `status` | `active/disabled` |

Runner session 缓存键：

```text
connection_id + config_hash + config_version + credential_version
```

配置或凭证版本变化会自然创建新 client。旧 client 在空闲超时、异常、LRU 超限或显式 cleanup 时关闭。Redis 不承载活连接，只适合后续做多实例配置缓存和失效通知。

## Skill Scope

`runtime.load_skill` 是通用工具，但它能加载什么由 release 中的 scope 决定。

推荐 scope 字段：

| 字段 | 说明 |
| --- | --- |
| `product_ids` | 产品边界，例如 `aicoin` |
| `domains` | 业务领域，例如 `membership`、`account` |
| `actions` | 允许动作，例如 `read`、`load` |
| `skill_library_ids` | 技术侧索引库 ID，通常由平台根据产品和领域生成 |

前端不应该让业务用户直接理解抽象 namespace。更好的展示方式是：

```text
产品 -> 业务领域 -> 经验库
```

Runner 侧只使用解析后的 `skill_library_ids` 和 scope 过滤结果。Core Runner 预先看到的是授权 Skill 摘要索引，不是正文；正文必须通过已注入的 `runtime.load_skill` 工具读取。

## 审批机制

触发条件：

- `requires_approval=true`
- `side_effect_level` 为 `internal_write`、`external_write`、`destructive`
- 后续可接入更细的 policy engine

运行态流程：

```text
LLM 生成 tool_call
-> Adapter 执行前检查 policy
-> 命中审批条件
-> 保存现场到 runner_approval_requests
-> 返回 pending_approval
-> 前端提交审批决定
-> Runner 重新解析 runtime context
-> 审批通过则按原始 tool_call 续跑
```

审批决定入口：

```text
POST /v1/gateway/approvals/{approval_id}/decide
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `decision` | `approved` 或 `rejected` |
| `decided_by` | 审批人。兼容 `actor_id` alias |
| `comment` | 可选备注 |

幂等语义：

- 已 approved 但未 executed：允许再次点击并继续同一现场。
- 已 executed：直接返回保存的 runner response，避免重复执行。
- rejected：返回拒绝 observation，不执行工具。

审批结果会以 `approval_approved` 或 `approval_rejected` observation 注入后续上下文。

## 工具参数错误反馈

当前方向不是靠启发式字符匹配适配所有 MCP，而是分层收集结构化反馈：

| 层级 | 来源 | 用途 |
| --- | --- | --- |
| 本地 schema 校验 | `input_schema.required/properties` | 缺字段、类型明显不符时给出稳定错误 |
| MCP 协议错误 | error code / data / message | 保留远端服务给出的修正线索 |
| manifest 信息 | tool description / input schema / annotations | 在 prompt 中提前减少低级错误 |
| 启发式兜底 | 错误文本关键词 | 只做分类标签，不直接改参数 |

LLM 重规划时应看到：

- 原始 tool call 参数。
- 工具 input schema。
- 远端错误信息。
- 本地分类标签，例如 `missing_required_argument`、`invalid_argument_type`、`remote_validation_error`。

这样做的理由是 MCP 服务差异很大，Runner 不能假装理解所有业务参数，但可以把可执行反馈整理成模型能修正的上下文。

## 旧 AiCoin 能力接入

旧工具保留为本地 MCP stdio 能力：

- `search_faq`
- `load_skill`
- `lookup_faq_detail`
- `save_user_memory`
- `record_handoff`

平台注册脚本：

```bash
PYTHONPATH=src python scripts/register_aicoin_platform_config.py \
  --database-url postgresql://127.0.0.1:55432/agent_platform
```

注册后仍必须经过 release 注入和运行时鉴权，不能因为本地 MCP server 存在就默认可执行。
