# 智能体平台技术设计详情

> 本文档承接原 README 中的技术细节，供需要理解表结构、权限模型、Runner 对接和实施决策的人阅读。项目入口、快速使用和后续计划见根目录 `README.md`。

## 项目概览

这是一个智能体平台 MVP，用来验证一条闭环：

内部用户通过 Web Console 创建、调试并运行受控 Agent；Agent 执行时只能使用授权后的工具，每次运行和工具调用都留下审计记录。

当前已具备：

- Web Console：Agent 广场、调试运行、配置中心、权限治理、Agent 列表、Runs 日志。
- 后端 API：用户、工具、Agent、运行记录的开发期接口。
- 权限治理：创建时能力校验、创建时能力快照、执行时动态裁剪工具。
- Runner：单实例共享 Runner，按请求注入 Agent 配置、工具结果和真实 LLM。
- 本地工具：`calculator`、`time_now`、`text_stats`。
- 配置中心化：后端自动读取项目根目录 `.env`，不需要 `export` 或 `source .env`。

当前还不是自主多轮 Agent 循环。Runner 目前执行的是“显式请求工具 -> 注入工具结果 -> 调一次真实 LLM”的受控流程。

## 快速启动

先准备 Conda 环境和配置：

```sh
conda activate agent-platform
cp .env.example .env
```

`.env` 使用普通键值格式：

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/agent_platform
LLM_API_KEY=
LLM_MODEL=
LLM_BASE_URL=
CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
```

后端会自动读取项目根目录 `.env`；同名真实环境变量优先级更高。也可以用 `ENV_FILE=/path/to/.env` 指定配置文件。

启动后端：

```sh
cd backend
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

启动前端：

```sh
cd frontend
npm install
npm run dev
```

访问：

- Web Console: `http://127.0.0.1:5173`
- Backend API: `http://127.0.0.1:8000`

内网内测时，把 `.env` 里的 `CORS_ORIGINS` 加上本机内网前端地址，例如：

```dotenv
CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173,http://192.168.152.69:5173
```

然后用内网后端地址启动前端：

```sh
cd frontend
VITE_API_BASE_URL=http://192.168.152.69:8000 npm run dev
```

其他内网设备访问 `http://192.168.152.69:5173`。

首次使用 Web Console 时，按顺序执行：配置中心初始化权限、创建 Agent、到 Agent 广场选择 Agent 调试。

## 架构层次

```text
Web Console
  ├─ Agent 广场
  ├─ 配置中心
  ├─ 调试运行
  └─ 运行日志

FastAPI API
  ├─ HTTP 路由与错误映射
  ├─ 用户 / 角色 / 工具 / Agent / Run 接口
  └─ 调用权限治理、数据库和 Runner

AuthZ 权限治理
  ├─ 创建 Agent 时校验工具能力
  ├─ 保存创建时能力快照
  └─ 执行时计算 effective_tools

Shared Runner
  ├─ 执行已授权工具
  ├─ 拼装 system prompt、用户消息和工具结果
  └─ 调用真实 OpenAI-compatible LLM

Tool Adapters
  ├─ calculator
  ├─ time_now
  └─ text_stats

PostgreSQL
  ├─ 用户、Agent、工具、能力、渠道绑定
  ├─ Run 记录
  └─ 权限事件、工具调用日志、审计日志
```

核心边界：

- API 不直接写业务算法，只编排 HTTP、权限、数据库和 Runner。
- 数据库读写集中在 `db.py`。
- Agent 配置字段和接口实体集中在 `models.py`。
- 权限集合规则集中在 `authz.py`。
- 本地工具集中在 `tools.py`。
- Runner 不负责决定权限，只接收已裁剪后的工具集合。

## 项目结构

```text
backend/
  app/
    main.py          # ASGI 入口，仅导出 app
    api.py           # FastAPI 路由与 HTTP 错误映射
    config.py        # 集中读取 .env 与真实环境变量
    db.py            # PostgreSQL schema、读写与审计
    models.py        # Agent、Tool、Run 等请求/响应实体
    authz.py         # 权限集合规则与能力裁剪
    tools.py         # 本地低风险工具实现
    runner.py        # 单次 Agent Runner 与真实 LLM 调用
  tests/
    test_authz.py
    test_config.py
    test_agent_routes.py
    test_agent_runner.py
    test_runs.py
frontend/
  src/
    App.tsx          # Ant Design Web Console
    main.tsx
    styles.css
  package.json
  vite.config.ts
```

## 数据库表结构

当前用幂等 DDL 自动创建表，正式生产前再引入 migration。

| 表 | 含义 |
| --- | --- |
| `users` | 开发期用户表，保存用户 ID 和启停状态。 |
| `user_sessions` | 开发期登录会话表，只保存 token hash 和撤销时间。 |
| `tools` | 工具 manifest 运行投影表，保存当前 Runtime 可解析的工具 ID、Runner 映射、scope schema、input schema 和执行安全属性。 |
| `workflow_versions` | Workflow 不可变发布版本事实表；同一 `workflow_id + version` 内容不可原地修改，同一 Workflow 同时最多一个 `active` 版本，旧版本保留为 `retired`。 |
| `mcp_connections` | MCP 连接事实源，保存 Streamable HTTP/stdio 配置、环境变量引用、连接 TTL、发现 TTL、配置版本和哈希。 |
| `roles` | 角色表，包含名称、描述、启停状态和 `is_system`；MVP 预置四个系统角色，也支持自定义角色。 |
| `user_roles` | 用户和角色绑定表，一个用户可绑定多个角色。 |
| `role_tool_permissions` | 角色可使用的工具和工具 scope。 |
| `role_change_events` | 自定义角色创建、编辑的审计事件，记录操作人、事件类型和权限快照。 |
| `agents` | Agent 主表，保存名称、图标、公开/私有类型、创建者、启停状态、`system_prompt`、模型。 |
| `agent_members` | 私有 Agent 可运行成员表；owner 隐式可管理和运行，不重复写入。 |
| `agent_tools` | Agent 配置的工具白名单和 `scope_json`。 |
| `agent_releases` | Agent 发布版本表，保存不可变 `config_json` 快照和发布/撤销状态；`config_json.tools` 内嵌发布时选定工具的 manifest 子集。 |
| `agent_permission_snapshots` | Agent 创建或更新时的工具权限快照，用于保证升权后 Agent 不自动扩权。 |
| `channel_bindings` | Agent 允许的入口渠道；当前主要是 `web_console`。 |
| `agent_runs` | 每次运行的主记录，包含消息、答案、状态、Agent、执行用户。 |
| `audit_logs` | Run 级审计日志，例如 `run_started`、`run_succeeded`、`run_failed`。 |
| `agent_permission_events` | Agent 权限与治理事件，例如越权调用、配置更新和管理员启停。 |
| `tool_call_logs` | 工具调用轨迹，记录工具 ID 和执行结果摘要。 |

## Agent 配置字段

创建 Agent 的请求实体在 `backend/app/models.py`：

```json
{
  "owner_user_id": "u_console",
  "name": "office-agent",
  "icon": "calculator",
  "system_prompt": "你是内部工具助手。优先根据工具结果回答，中文简短。",
  "model": null,
  "tools": [
    {
      "tool_id": "calculator",
      "scope": {
        "actions": ["calculate"]
      }
    },
    "time_now",
    "text_stats"
  ],
  "channels": ["web_console"]
}
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `owner_user_id` | Agent 创建者，拥有管理、发布和运行权限。 |
| `name` | Agent 名称，用于 Console 展示。 |
| `icon` | Agent 展示图标，当前前端支持 `robot`、`calculator`、`time`、`text`、`desktop`。 |
| `visibility` | Agent 类型：`private` 表示私有自建 Agent，`public` 表示公开发布 Agent。 |
| `members` | 私有 Agent 的可运行成员；owner 隐式拥有权限，不重复写入成员表。 |
| `system_prompt` | Runner 注入给 LLM 的系统提示词。 |
| `model` | Agent 指定模型；为空时使用 `.env` 里的 `LLM_MODEL`。 |
| `tools` | Agent 配置工具白名单。兼容旧格式 `["calculator"]`，也支持新格式 `[{tool_id, scope}]`。 |
| `channels` | 允许入口渠道，MVP 默认 `web_console`。 |

## 工具 Manifest 字段

平台侧 `tools` 表不是工具实现本身，而是真实 Runner ToolRegistry 的治理镜像。真实工具仍在 Runner 侧注册、持有凭证并执行；平台保存 manifest 用于角色授权、Agent 配置、发布快照和审计。

### Runner Manifest 交付

真实 Runner 是工具实现的事实源，平台 `tools` 是治理事实源。两者不由 Runner 进程启动事件同步，而由部署流水线交付版本化 manifest：

1. 开发者在 Runner 仓库同时提交工具实现和 manifest。
2. CI 校验 `tool_id`、版本、schema 和安全属性，产出完整 manifest bundle。
3. 部署流水线使用独立服务凭证调用平台批量导入 API。
4. 平台按 `tool_id` 增量 upsert manifest，但不覆盖管理员的 `status`。
5. 新工具不自动进入角色或 Agent，必须经过显式授权、配置和发布。

首次部署和后续发布都上传完整 bundle，便于检测清单漂移；平台只写入新增和变更项。消失项先进入待下线状态，确认没有 Agent 草稿、角色授权和发布快照引用后再禁用，不做物理删除。无状态 Runner 实例只负责执行，不持有平台写权限。

当前 `PUT /tools/{tool_id}` 仅是开发期单工具注册入口，还不是生产 CI 合同。生产化需新增受保护的批量导入 API、服务凭证、bundle hash 和导入审计。

MCP 工具不再在每条 `tools` 记录中重复维护连接配置。`tools.mcp_connection_id` 指向
`mcp_connections.id`，`runner_name` 保存远端 MCP tool 名。发布快照固化工具 manifest 和连接快照，
真实 Runner resolver 再按 connection ID 刷新当前 active 连接配置，以支持地址、TTL和凭证引用轮换；
连接停用时对应工具不进入 runtime context。

平台使用官方 MCP Python SDK `1.x` 完成工具发现：

- 支持 Streamable HTTP 和 stdio，执行 `initialize` 后分页调用 `tools/list`。
- Streamable HTTP 连接支持直接保存请求 Header，例如 `Authorization`、`x-api-key` 和 `Accept`；发现工具时按 `mcp_connections.config_json.headers` 发送。
- `bearer_env` 和 stdio `env_vars` 保留为兼容旧部署方式，只保存变量名，实际值统一通过配置模块从进程环境或 `.env` 读取；当 headers 已显式配置 `Authorization` 时不会被 `bearer_env` 覆盖。
- 当前 Header 值会保存在 `mcp_connections.config_json.headers`；内网 MVP 先由平台管理员治理，后续开放更广用户前需要增加加密、脱敏回显和只写更新。
- 平台工具 ID 固定为 `mcp.{connection_id}.{remote_name}`，`runner_name` 保留远端原名。
- MCP annotations 映射为只读、幂等、副作用和网络边界；缺失 annotations 时先按工具名做有限只读启发，
  例如 `search/lookup/weather/geo/distance/direction` 归为 `external_read`，`create/update/delete/send/book/take_taxi`
  仍归为需审批写操作。启发只用于补齐远端 manifest 缺失的安全字段，管理员仍可通过停用工具或角色权限收缩可用范围。
- 同一次发现批量更新 manifest；远端已移除工具改为 `disabled`，不删除历史引用。
- 重新发现不会覆盖管理员手动设置的工具启停状态。
- Web Console 按 `layer -> source/connection -> tool` 收纳展示自动发现工具；Agent 广场只展示工具组和数量，配置中心按层级和 MCP/来源组二级折叠，MCP 组默认收起并支持整组全选或清空，用户中心按同一分组展示当前用户的角色工具授权和 scope。MCP `title/description` 是展示事实源，前端不硬翻译远端工具名，无 `description` 时才展示连接、远端工具名和参数摘要作为兜底。

当前核心字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 平台稳定工具 ID，角色、Agent 和发布快照都引用它。 |
| `runner_tool_id` | Runner 侧真实工具 ID，例如 `crm.customer.lookup`。 |
| `runner_name` | Runner 服务或工具注册域，MVP 可为空。 |
| `status` | `active` / `disabled`；禁用后不能进入新的发布快照。 |
| `source` | 工具来源：`local`、`mcp`、`http`、`workflow_agent`。 |
| `version` | Runner 工具声明版本。 |
| `manifest_hash` | manifest 的确定性 hash，用于发现 Runner 工具声明漂移。 |
| `input_schema_json` | LLM tool calling 注入用参数 schema。 |
| `scope_schema_json` | 平台角色和 Agent 配置可选择的 allow-list scope。 |
| `read_only` | 是否只读，影响自动执行和审批策略。 |
| `idempotent` | 是否可安全重试。 |
| `parallel_safe` | 是否可以和其他工具并发执行。 |
| `requires_approval` | 是否必须人工确认。 |
| `side_effect_level` | `none`、`external_read`、`internal_write`、`external_write`、`destructive`。 |
| `data_sensitivity` | `public`、`internal`、`confidential`、`secret`。 |
| `network_access` | `none`、`internal_only`、`external`。 |
| `timeout_ms` | Runner 执行保护上限。 |

不把 `risk_level=low/medium/high` 作为核心治理字段。风险等级过于笼统，无法直接指导 Runner 是否并发、能否重试、是否需要审批、能否把结果完整返回给模型。后续 UI 如果需要风险展示，可以由上述具体字段派生。

### 工具治理边界

MVP 的工具治理页面只做两件事：

- 查看工具 manifest 详情。
- 开启或关闭工具，即修改 `tools.status`。

控制台不允许手工编辑 `runner_tool_id`、`input_schema`、`scope_schema`、`read_only`、`idempotent`、`parallel_safe`、`side_effect_level`、`version`、`manifest_hash` 等 Runner Source 字段。这些字段应来自 Runner manifest 同步或本地 seed，避免平台把 Runner 实际不支持的工具定义发布出去。

`tools.status` 语义：

| 状态 | 语义 |
| --- | --- |
| `active` | 可配置、可授权、可发布、可运行注入。 |
| `disabled` | 不可新配置、不可新发布；Debug Runner 和真实 Runner 网关解析时都应过滤，不注入给模型。 |

已发布 release 的 `config_json` 仍保持不可变。Runner 网关在解析 release snapshot 时必须再检查当前 `agents.enabled` 和 `tools.status`：Agent 被平台停用时直接拒绝运行；工具关闭后不再注入给模型；如果模型仍请求 disabled 或未注入工具，Runner 执行前必须拒绝。

## 权限模型

当前权限以角色和工具 scope 为主。

### 登录态

Web Console 已接入开发期登录态：

- `POST /auth/login` 使用已存在且 active 的 `user_id` 登录。
- 后端签发 bearer token，数据库只保存 token hash。
- `GET /auth/me` 返回当前用户状态、角色和 role scope。
- `POST /auth/logout` 撤销当前 session。
- 前端把 token 保存在浏览器 `localStorage`，后续请求会带 `Authorization: Bearer <token>`。

这不是正式企业登录；没有密码、MFA、SSO、组织身份同步和 token 过期策略。正式接入 IM/OA/SSO 后，替换 `/auth/login` 的身份来源即可，用户、角色和 scope 表可以继续复用。

执行时有效工具集合满足：

```text
effective_tools = configured_tools
                ∩ created_at_permission_snapshot
                ∩ user_current_role_tools
```

含义：

- 创建 Agent 时，工具和 scope 必须来自创建者当前角色权限。
- Agent 创建或更新配置时会保存创建者当时的工具权限快照。
- 用户降权后，Agent 下次执行立即自动收缩工具。
- 用户升权后，已有 Agent 不自动获得新增工具。
- Runner 只能执行 API 已确认属于 `effective_tools` 的工具。
- 拒绝事件写入 `agent_permission_events`。

- 用户可绑定多个角色。
- 多角色工具 scope 按 key 合并，数组取并集。
- scope 只支持 `dict[str, list[str]]` 的 allow-list。
- 工具 `scope_schema` 为空时表示工具级授权，角色可保存空 scope `{}`；只有工具声明了可裁剪 scope 时才要求选择具体 allow-list 值。
- 如果用户已有角色权限，Agent 创建时工具和 scope 必须是当前角色权限的子集。
- 如果用户没有角色工具权限，不能创建带工具的 Agent。

当前执行授权规则：

- `public` Agent：所有 active 用户可见可运行，但可执行工具仍要和执行用户角色 scope 求交集。
- `private` Agent：仅 owner、`agent_members` 成员或 `platform_admin` 可运行。
- owner 或 `platform_admin` 可读取和更新草稿、发布和撤销 Agent；成员和公开用户只有运行权限。
- 仅 `platform_admin` 可启停 Agent；停用后草稿和发布版本都不能运行，但不会改写历史发布快照。
- 更新与发布接口同时校验 bearer token 和请求 `actor_id`，公开可见不等于可管理。
- 外部渠道鉴权仍由后续 Runner Gateway 接入。

### 权限治理页面与演示用户

左侧橘色的“权限治理”仅在当前登录用户拥有 `platform_admin` 时显示。服务端对以下接口也强制校验 bearer token 对应的管理员角色，不能只依赖前端隐藏入口：

- `GET /admin/roles`
- `GET /admin/users`
- `PUT /admin/users/{user_id}/roles`
- `PUT /admin/agents/{agent_id}/status`

Agent 配置管理接口：

- `POST /agents`：登录用户只能以自己为 owner 创建，`platform_admin` 可代建。
- `GET /agents/{agent_id}`：仅 owner 或 `platform_admin` 可读取完整草稿。
- `PUT /agents/{agent_id}`：仅 owner 或 `platform_admin` 可更新；工具仍按 owner 当前角色范围校验。
- `POST /agents/{agent_id}/releases`：仅 owner 或 `platform_admin` 可发布，且登录用户必须等于 `actor_id`。

角色分配请求中的 `actor_id` 必须与 bearer token 对应用户一致，避免客户端伪造管理员 ID。

执行“初始化”或调用 `POST /admin/seed-roles` 后，会创建以下可登录的演示用户：

| 用户 | 初始角色 | 用途 |
| --- | --- | --- |
| `u_console` | `platform_admin`、`office_tools` | 平台管理员与全量本地工具测试。 |
| `u_analyst` | `analyst_tools` | 计算器、文本统计工具测试。 |
| `u_timekeeper` | `time_tools` | 当前时间工具测试。 |
| `u_guest` | 无 | 普通无权限用户测试。 |

预置角色的工具范围如下：

| 角色 | 工具 scope |
| --- | --- |
| `platform_admin` | 无运行工具权限，仅平台治理。 |
| `office_tools` | `calculator.calculate`、`text_stats.read`、`time_now.read`。 |
| `analyst_tools` | `calculator.calculate`、`text_stats.read`。 |
| `time_tools` | `time_now.read`。 |

系统角色仅用于平台默认能力，不允许编辑或删除；管理员可在“权限治理”创建、编辑、停用自定义角色。自定义角色 ID 支持中英文、数字、点、下划线和横线，例如 `finance_readonly` 或 `高德mcp`，创建后不可修改。权限治理支持按 `layer -> source/connection` 工具组批量加入或移除授权；有 `scope_schema_json` 的工具会按声明的 allow-list scope 授权，无 scope schema 的外部工具保存空 scope `{}` 表示工具级权限。当前本地工具只支持 `actions`：

| 工具 | 允许的 `actions` |
| --- | --- |
| `calculator` | `calculate` |
| `time_now` | `read` |
| `text_stats` | `read` |

## Runner 执行流程

`POST /agents/{agent_id}/runs` 当前流程：

```text
接收 actor_id / message / tool_ids
  -> 加载 Agent 配置
  -> 校验 owner、渠道、用户状态
  -> 计算 effective_tools
  -> 过滤当前 disabled 工具
  -> 拒绝未授权 tool_ids
  -> 写 agent_runs: running
  -> 执行请求中显式传入的工具
  -> 写 tool_call_logs
  -> 拼装 LLM messages
  -> 调用真实 LLM
  -> 写 agent_runs: succeeded / failed / pending_approval
  -> 返回统一 runner response
```

当前不是模型自主 tool calling 循环；后端仍接收 `tool_ids`，但 Web Console 调试页不再允许手动选择工具，会固定使用所选 Agent 已配置且当前 `active` 的工具集合。下一阶段可以接 OpenAI-compatible tool calling，并用最大轮数限制工具循环。

真实 Gateway Runner 返回结构至少应兼容：

```json
{
  "run_id": "run_x",
  "status": "succeeded",
  "answer": "完成",
  "tool_calls": []
}
```

当需要人工审批时返回：

```json
{
  "run_id": "run_x",
  "status": "pending_approval",
  "answer": "等待人工审批",
  "approval_requests": [
    {
      "approval_id": "approval_x",
      "tool_id": "tool_x",
      "reason": "需要确认外部写操作",
      "arguments": {}
    }
  ]
}
```

平台后端会把顶层、`data`、`trace` 中的待审批信息归一化到顶层 `approval_requests`；当 runner 当前状态仍是
`pending_approval` 时，也会从 `tool_calls/tool_observations` 中提取最后一个待审批工具调用，
以兼容一次对话中“审批 -> 工具失败 -> 再次审批”的多轮场景。已完成响应里的历史 pending 工具轨迹不会再次触发审批卡片。
Web Console 只展示归一化后的 `approval_requests[0]`。
审批动作经平台后端代理到真实 Runner，返回值仍按同一 response 结构覆盖当前调试结果。

## 本地工具

| 工具 | 用途 | 外部依赖 |
| --- | --- | --- |
| `calculator` | 执行安全的基础算术表达式。 | 无 |
| `time_now` | 返回服务端当前时间。 | 无 |
| `text_stats` | 统计消息字符数和 token 数。 | 无 |

初始化工具示例。通常直接调用 `POST /admin/seed-roles` 即可注册内置工具；手动注册外部 Runner 工具时，需要提交 manifest：

```sh
curl -X PUT http://127.0.0.1:8000/tools/crm_customer_lookup \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "CRM Customer Lookup",
    "description": "查询客户基础信息",
    "source": "workflow_agent",
    "runner_tool_id": "crm.customer.lookup",
    "runner_name": "main-runner",
    "version": "2026-06-24.1",
    "read_only": true,
    "idempotent": true,
    "parallel_safe": true,
    "requires_approval": false,
    "side_effect_level": "external_read",
    "data_sensitivity": "confidential",
    "network_access": "internal_only",
    "timeout_ms": 8000,
    "input_schema": {
      "type": "object",
      "properties": {
        "customer_name": {"type": "string"}
      },
      "required": ["customer_name"]
    },
    "scope_schema": {
      "actions": ["read"],
      "resources": ["customer_profile"]
    }
  }'
```

## API 一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/auth/login` | 使用 active 用户 ID 创建开发期登录 session。 |
| `GET` | `/auth/me` | 根据 bearer token 查询当前用户信息。 |
| `POST` | `/auth/logout` | 撤销当前登录 session。 |
| `PUT` | `/users/{user_id}` | 创建或更新用户状态。 |
| `GET` | `/tools` | 查询平台已同步的工具 manifest。 |
| `PUT` | `/tools/{tool_id}` | 注册工具。 |
| `PUT` | `/admin/tools/{tool_id}/status` | 平台管理员开启或关闭工具；只修改 `tools.status`。 |
| `GET` | `/admin/mcp-connections` | 平台管理员查询 MCP 连接配置。 |
| `PUT` | `/admin/mcp-connections/{connection_id}` | 新增或更新 MCP 连接；配置变化时自动递增版本。 |
| `POST` | `/admin/mcp-connections/{connection_id}/discover` | 连接 MCP 并同步 `tools/list`；仅平台管理员可用。 |
| `POST` | `/admin/seed-roles` | 初始化预设角色、内置工具和 bootstrap 管理员。 |
| `GET` | `/admin/roles` | 平台管理员查询角色和角色工具权限。 |
| `POST` | `/admin/roles` | 平台管理员创建自定义角色。 |
| `PUT` | `/admin/roles/{role_id}` | 平台管理员编辑或停用自定义角色，系统角色不可编辑。 |
| `GET` | `/admin/users` | 平台管理员查询用户和已绑定角色。 |
| `PUT` | `/admin/users/{user_id}/roles` | 平台管理员给用户覆盖绑定角色；`actor_id` 必须与 bearer token 一致。 |
| `POST` | `/agents` | 创建 Agent，并保存创建者能力快照。 |
| `GET` | `/agents` | 查询最近 Agent 列表，供 Web Console 使用。 |
| `POST` | `/agents/{agent_id}/releases` | 发布 Agent，生成不可变 `agent_releases.config_json`。 |
| `GET` | `/agents/{agent_id}/releases` | 查询 Agent 发布版本列表，供 Debug Runner 选择发布版本。 |
| `POST` | `/agents/{agent_id}/releases/{release_id}/revoke` | 撤销已发布版本。 |
| `GET` | `/agents/{agent_id}/releases/{release_id}` | 查询发布版本配置，供 Runner 网关模拟读取。 |
| `POST` | `/agents/{agent_id}/releases/{release_id}/runs` | 按发布版本运行 Debug Runner，使用 release snapshot + 当前 `tools.status`。 |
| `POST` | `/agents/{agent_id}/authorize` | 按 Agent 配置、快照和当前能力裁剪工具。 |
| `POST` | `/agents/{agent_id}/runs` | 通过共享 Runner 运行 Agent。 |
| `POST` | `/gateway/approvals/{approval_id}/decide` | 平台代理审批决定到真实 Gateway Runner；`actor_id` 必须与 bearer token 一致。 |
| `GET` | `/runs` | 查询最近运行记录。 |
| `POST` | `/runs` | Phase 0 手动 LLM 调用接口。 |

## 测试与验收

后端：

```sh
conda run -n agent-platform --cwd "$PWD/backend" pytest -q tests
conda run -n agent-platform --cwd "$PWD/backend" python -m compileall -q app
```

前端：

```sh
cd frontend
npm run build
npm audit --audit-level=low
```

当前常见 warning：

- FastAPI/Starlette TestClient 关于 `httpx` 的 deprecation warning，不影响当前功能。
- Ant Design 单页构建包较大，MVP 暂不做代码分片。

## 下一阶段权限与发布设计

本平台的长期定位是 Agent Control Plane：负责 Agent 注册、配置、权限治理、发布和调试。生产流量由独立 Runner / 渠道网关承载。本平台内的 Runner 只作为 Debug Runner / Gateway Simulator，不和生产 Runner 强耦合。

### 生产 Runner 配置来源

生产 Runner 不直接读取控制面草稿表，例如 `agents`、`agent_tools`。当前 MVP 倾向控制台和真实 Runner 共用同一个 PostgreSQL 库、同一张 `tools` 表；平台 API 负责写入和治理 `tools`，真实 Runner 运行时读取 `agent_releases`、`tools` 和 actor 当前角色权限：

- `agent_releases` 提供已发布、不可变的运行配置。
- `tools` 提供当前工具 manifest 和 `status`。
- `user_roles + role_tool_permissions` 提供执行用户当前工具上限，用于和发布快照求交集。
- 真实 Runner 不写 `tools`，也不读取草稿态 Agent 配置。

这里先不强调独立只读账号，先通过 Runner 代码路径和平台 API 边界约束操作。后续生产安全加固时，再把 DB 权限、服务账号和 `/runner/resolve` 抽象层补上。

当前表：

```text
agent_releases
  id UUID PRIMARY KEY
  agent_id UUID NOT NULL
  version INTEGER NOT NULL
  status TEXT NOT NULL          # published / revoked
  config_json JSONB NOT NULL
  published_by TEXT NOT NULL
  published_at TIMESTAMPTZ NOT NULL
  revoked_at TIMESTAMPTZ
  UNIQUE(agent_id, version)
```

发布版本必须不可变。编辑 Agent 草稿不会影响生产，只有重新发布才生成新版本。撤销发布通过 `status=revoked` 或 `revoked_at` 表达。

Runner 网关 MVP 请求先使用简单字段，不启用 token：

```json
{
  "agent_id": "agent_x",
  "release_id": "release_y",
  "version": 3,
  "release_token": null,
  "channel": "im",
  "external_user_id": "u_123",
  "message": "..."
}
```

`release_token` 字段只预留。当前内网可信调用先用 `agent_id + release_id/version` 查询发布表；后续接多个渠道或不可信调用方时，再把 `release_token` 做成平台签发的 HMAC/JWKS 签名引用。

审批决定请求由平台转发给真实 Runner：

```text
POST /v1/gateway/approvals/{approval_id}/decide
```

```json
{
  "actor_id": "u_console",
  "decision": "approved",
  "comment": ""
}
```

`decision` 当前只允许 `approved` 或 `rejected`。真实 Runner 应返回统一 runner response；如果审批通过后继续执行并完成，则返回 `status=succeeded` 和最终 `answer/tool_calls`，如果拒绝则返回拒绝后的业务状态和可展示说明。

### 角色与工具 Scope

当前采用角色维度权限：

```text
roles
  id
  name
  status

user_roles
  user_id
  role_id

role_tool_permissions
  role_id
  tool_id
  scope_json
```

MVP 规则：

- 用户可拥有多个角色。
- 角色不做层级继承。
- 用户最终权限为多个角色的工具 scope 并集。
- scope 只支持字符串数组 allow-list，不支持 deny、数字合并、布尔覆盖和嵌套策略。

scope 示例：

```json
{
  "actions": ["calculate", "read"],
  "schemas": ["finance"]
}
```

同一工具的多个角色 scope 合并规则：

```text
同 key 数组取并集
```

Agent 配置工具时可以显式选择 scope。旧字符串数组仍兼容，新请求结构：

```json
{
  "tools": [
    {
      "tool_id": "calculator",
      "scope": {
        "actions": ["calculate"]
      }
    }
  ]
}
```

`agent_tools.scope_json` 已用于保存真实权限配置。Agent 配置的工具 scope 必须是创建者当前角色权限 scope 的子集。

### 平台管理员与角色授权

MVP 引入特殊角色 `platform_admin`。

`platform_admin` 能力：

- 初始化或创建预设角色。
- 给用户绑定角色。
- 查看所有 Agent。
- 后续可撤销发布。

普通用户能力：

- 只能使用自己角色赋予的工具 scope。
- 只能创建自己的 Agent。
- 只能调试和发布自己的 Agent。
- 不能给自己或其他用户授权角色。

第一个管理员通过 `.env` 配置：

```dotenv
BOOTSTRAP_ADMIN_USERS=u_console
```

配置中心初始化时读取 `BOOTSTRAP_ADMIN_USERS`，创建 `platform_admin` 并绑定这些用户。生产环境需要限制为仅首次 bootstrap，已有 admin 后拒绝重复自举。

当前已实现接口：

```http
POST /admin/seed-roles
GET /admin/roles
POST /admin/roles
PUT /admin/roles/{role_id}
GET /admin/users
PUT /admin/users/{user_id}/roles
```

角色授权请求：

```json
{
  "actor_id": "u_console",
  "roles": ["office_tools"]
}
```

`actor_id` 必须拥有 `platform_admin`，且必须等于 bearer token 解析出的登录用户。正式登录接入前 token 仍来自开发期用户 ID 登录。

### 发布快照内容

发布时 `agent_releases.config_json` 同时保存运行配置、工具 manifest 子集和权限来源快照。

- `agent_tools` 是草稿态工具配置，编辑 Agent 时可变。
- `agent_releases.config_json.tools` 是发布态工具快照，不可变，供 Runner 运行时读取。
- MVP 不单独新建 `agent_release_tools` 快照表，避免过早增加 join 和一致性维护成本。
- 后续如果要按工具反查影响面、下线前分析所有 release、或做 SQL 统计，再增加 `agent_release_tools` 作为索引表；即使增加索引表，`config_json` 仍保留完整运行快照。

```json
{
  "agent": {
    "id": "agent_x",
    "name": "office-agent",
    "icon": "calculator",
    "owner_user_id": "u_console",
    "system_prompt": "...",
    "model": null,
    "channels": ["web_console"],
    "enabled": true
  },
  "tools": [
    {
      "tool_id": "calculator",
      "runner_tool_id": "local.calculator",
      "runner_name": null,
      "name": "Calculator",
      "description": "执行安全的基础算术表达式。",
      "source": "local",
      "version": "local-v1",
      "read_only": true,
      "idempotent": true,
      "parallel_safe": true,
      "requires_approval": false,
      "side_effect_level": "none",
      "data_sensitivity": "internal",
      "network_access": "none",
      "timeout_ms": 8000,
      "input_schema": {
        "type": "object",
        "properties": {
          "expression": {"type": "string"}
        },
        "required": ["expression"]
      },
      "scope_schema": {
        "actions": ["calculate"]
      },
      "scope": {
        "actions": ["calculate"]
      },
      "manifest_hash": "sha256:..."
    }
  ],
  "runtime_policy": {
    "release_token_enabled": false
  },
  "permission_snapshot": {
    "published_by": "u_console",
    "published_by_roles": ["office_tools"],
    "role_tool_scopes": {
      "calculator": {
        "actions": ["calculate"]
      }
    }
  }
}
```

Runner 只关心 `agent`、`tools` 和 `runtime_policy`；平台审计关心 `permission_snapshot`。Runner 执行时应以发布快照里的 `tool_id + runner_tool_id + manifest_hash + scope` 为准，不能使用前端临时传入的工具列表扩权。

### Runner 联动方式

控制面和生产 Runner 的基本联动不是让 Runner 猜工具，而是由控制面发布一份不可变运行配置；Runner 再从同库 `tools` 表读取当前状态：

```text
control-plane draft
  -> publish
  -> agent_releases.config_json
  -> runner gateway reads agent_releases.config_json
  -> runner gateway reads tools.status for released tool_ids
  -> runner gateway filters disabled tools
  -> runner builds tool registry from active config_json.tools
  -> runner injects active tool specs into LLM call
  -> runner executes only tools injected in this request
```

`config_json.tools` 是 Runner 的工具白名单和动态注入来源。每个工具项至少包含：

```json
{
  "tool_id": "calculator",
  "runner_tool_id": "local.calculator",
  "read_only": true,
  "idempotent": true,
  "parallel_safe": true,
  "requires_approval": false,
  "side_effect_level": "none",
  "input_schema": {"type": "object"},
  "scope_schema": {
    "actions": ["calculate"]
  },
  "scope": {
    "actions": ["calculate"]
  },
  "manifest_hash": "sha256:..."
}
```

生产 Runner 侧建议维护一个本地 `ToolRegistry`：

```text
runner_tool_id -> ToolAdapter
```

Runner 收到请求后按 `agent_id + release_id/version` 读取 `agent_releases.config_json`，再用 release 中的 `tool_id` 查询同库 `tools.status`，并读取 actor 当前角色工具权限，把发布工具、active 工具和 actor role tools 求交集，不注入 disabled 或 actor 无权工具。随后用 `runner_tool_id` 从本地内存 `ToolRegistry` 找到真实工具实现，组装模型可见的 tool schema 或工具说明。模型即使请求了未发布、已关闭、actor 无权或未注入工具，Runner 也必须拒绝；scope 也只能按发布快照和 actor 当前 role scope 的交集裁剪。`read_only`、`idempotent`、`parallel_safe` 等字段可直接用于执行计划，例如并发执行、重试控制和人工审批。

真实 Runner 侧最小读取 SQL：

```sql
SELECT config_json
FROM agent_releases
WHERE agent_id = $1
  AND id = $2
  AND status = 'published';

SELECT id, status
FROM tools
WHERE id = ANY($1);

SELECT rtp.tool_id, rtp.scope_json
FROM user_roles ur
JOIN roles r ON r.id = ur.role_id AND r.status = 'active'
JOIN role_tool_permissions rtp ON rtp.role_id = ur.role_id
WHERE ur.user_id = $3;
```

关键点是：Runner 只依赖发布快照、当前 `tools.status` 和 actor 当前角色权限，不依赖控制面草稿表，也不接受前端或渠道请求临时传来的工具列表作为生产授权依据。请求里如果后续加入 `tool_allowlist`，它也只能缩小工具集合，不能扩大 release 或 actor 已授权集合。

### 真实 Runner 最小适配计划

真实 Runner 的目标是无状态、无身份、多实例可水平扩展。每个实例不注册自己，不推送 manifest，不保存平台配置，只在请求内解析运行上下文。

当前仓库已用 `backend/app/runtime_context.py` 抽出最小 Runtime Context Resolver 合同，Debug Runner 的发布版本模式也复用该逻辑：

- `fetch_runtime_context(agent_id, release_id, actor_id, channel)` 读取 `agent_releases`。
- `build_runtime_context(release)` 从 release snapshot 提取 Agent、工具和 scope。
- `db.active_tool_ids(tool_ids)` 读取同库 `tools.status`，过滤 disabled 工具。
- `runner_agent_from_context(context)` 把 Runtime Context 适配为当前 Debug Runner 可执行结构。

真实 Runner 先作为一个服务落地，但内部按模块分层，不急着拆进程：

```text
runner_service/
  gateway/
    im_adapter.py
    oa_adapter.py
    http_adapter.py
    request_normalizer.py

  resolver/
    runtime_context.py
    release_resolver.py
    actor_resolver.py
    tool_resolver.py
    authz.py

  core/
    llm_runner.py
    tool_registry.py
    tool_executor.py
```

各层职责：

| 层 | 职责 |
| --- | --- |
| Gateway Layer | 对接 IM/OA/API 等渠道，把不同输入统一成 `agent_id/release_id/actor/channel/message/trace_id`。 |
| Runtime Context Resolver | 从平台 DB 读取 `agent_releases`、`tools.status` 和 actor roles，产出一致的运行上下文。 |
| Runner Core | 只消费 Runtime Context，注入工具 schema、调用 LLM、执行本地 ToolAdapter。 |

Runtime Context 目标结构：

```json
{
  "release": {
    "release_id": "release_1",
    "agent_id": "agent_1",
    "version": 1,
    "status": "published"
  },
  "agent": {
    "id": "agent_1",
    "name": "office-agent",
    "owner_user_id": "u_console",
    "system_prompt": "...",
    "model": "deepseek-v4-pro",
    "enabled": true,
    "channels": ["web_console", "im"]
  },
  "actor": {
    "actor_id": "u_123"
  },
  "channel": "im",
  "tools": [
    {
      "tool_id": "calculator",
      "runner_tool_id": "local.calculator",
      "input_schema": {"type": "object"},
      "scope": {"actions": ["calculate"]}
    }
  ],
  "tool_ids": ["calculator"],
  "tool_scopes": {
    "calculator": {"actions": ["calculate"]}
  },
  "policy": {
    "source": "agent_release",
    "disabled_tools_filtered": []
  }
}
```

Runner 本地只需要内存 `ToolRegistry`：

```text
runner_tool_id -> ToolAdapter
```

一次生产请求的最小流程：

```text
1. 接收 agent_id / release_id / channel / actor / message
2. 查询 agent_releases，拿到 release config
3. 从 release config.tools 提取 tool_id 列表
4. 查询 tools 表当前 status
5. 过滤 disabled 工具
6. 用 active 工具的 input_schema 注入 LLM
7. 模型发起 tool call
8. 校验 tool_id 在本轮注入列表内
9. 用 runner_tool_id 找本地 ToolAdapter 执行
10. 记录 run/tool trace
```

Runner 不需要持久化 ToolRegistry；工具实现跟随 Runner 镜像或代码发布。`tools` 表中的 manifest 用于平台治理和发布快照，真实执行最终仍以 Runner 本地 `ToolAdapter` 为准。

当前 API 层操作边界：

| 组件 | 允许操作 |
| --- | --- |
| Web Console / 平台 API | 查询、创建、更新 Agent 草稿；发布 release；开启/关闭 tools。 |
| 真实 Runner | 查询 `agent_releases` 和 `tools.status`；执行本地 ToolAdapter。 |
| 渠道请求 | 提供 `agent_id/release_id/message`，不能直接扩大工具集合。 |

### Debug Runner 发布版本模式

Web Console 调试页现在支持两种模式：

| 模式 | 配置来源 | 用途 |
| --- | --- | --- |
| 草稿配置 | `agents + agent_tools + 当前角色权限 + tools.status` | 配置阶段快速验证。 |
| 发布版本 | `agent_releases.config_json.tools + tools.status` | 模拟真实 Runner 消费发布快照。 |

发布版本模式只使用 release snapshot 内的工具，再按当前 `tools.status` 过滤 active 工具；它不读取 `agent_tools` 草稿配置，也不使用请求临时工具列表扩权。

## 已确定的技术决策

| 决策 | 依据 |
| --- | --- |
| 后端采用 Python + FastAPI | 贴近 Agent/RAG 生态，MVP 可用较少代码验证 HTTP 与 LLM 闭环。 |
| Python 3.12，Conda 环境 `agent-platform` | 比系统 Python 3.14 有更成熟的 FastAPI、数据库和 LLM SDK 兼容性。 |
| PostgreSQL 使用本地 `127.0.0.1:55432` | 本地端口已可连接，权限、Agent、工具和审计是关系数据。 |
| 配置集中在 `config.py` | `.env` 不需要 shell 语法，服务启动方式更稳定。 |
| LLM 使用 OpenAI 兼容接口 | 用真实模型验证调用链，同时通过 `LLM_*` 切换供应商和模型。 |
| 单实例共享 Runner | 先验证动态注入 Agent 配置和工具结果，后续可演进为多副本无状态 Runner。 |
| 真实 Runner 与平台共用 `tools` 表 | 内网 MVP 先让平台写 `tools`、Runner 读 `tools.status`，避免双表同步和状态不一致。 |
| Web Console 使用 Ant Design | 内部后台以表单、表格、日志和工作台为主。 |
| Tool Adapter 先于 MCP | 当前工具少，权限边界比协议标准化更优先。 |
| 工具治理使用具体执行属性，不使用笼统风险等级 | `read_only`、`idempotent`、`parallel_safe`、`requires_approval`、`side_effect_level` 能直接驱动 Runner 执行计划和审批策略。 |
| 发布快照内嵌工具 manifest 子集 | MVP 先保证 Runner 获取单份 release config 即可运行；影响面分析需求出现后再增加 `agent_release_tools` 索引表。 |
| 工具治理页只允许查看和启停 | Runner Source 字段不在平台手改；关闭工具通过 `tools.status` 让 Debug Runner 和生产 Runner 网关不再注入。 |
| 管理员 API 绑定 bearer session | 前端隐藏导航只改善体验，服务端必须依据登录用户的 `platform_admin` 角色拒绝越权请求。 |
| 系统角色与自定义角色分离 | 平台基线角色不可编辑；业务差异通过管理员创建的 role + 工具 scope 表达，避免预置角色无限膨胀。 |

## MVP 边界

当前不做：

- IM 渠道接入。
- Redis / 队列。
- MCP / FastMCP。
- 每 Agent 独立容器。
- A2A 多 Agent 协作。
- 低代码画布。
- 高危工具，如 `shell_exec`、`db_write`。
- 企业级 SSO/OA 登录、复杂资源 scope 策略。

这些能力等真实使用路径和风险需求明确后再引入。

## 下一步计划

1. 用户角色绑定增加审计日志，记录谁给谁绑定了哪些角色。
2. 发布版本列表补充撤销按钮和影响面提示。
3. 接 OpenAI-compatible tool calling，让模型根据发布配置自主选择工具，并设置最大循环轮数。
4. 为真实 Runner 工具包定义 manifest bundle 和受保护的批量导入协议。
5. 引入 migration，替换当前幂等 DDL。
6. 接入企业 SSO/OA 后移除开发期 `actor_id`，改为服务端从企业身份令牌解析执行用户。
