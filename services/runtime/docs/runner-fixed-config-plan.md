# 固定配置 Runner 与缓存后续计划

## 当前结论

首期仍以 `/v1/gateway/runs` 为主入口：请求只带 `agent_id`、`release_id`、`actor_id`、`channel` 和消息内容，由 resolver 每次解析发布快照、工具状态和运行时权限，再把 runtime context 注入 configured runner。

后续可以增加固定配置 Runner 入口，但它不替代网关解析链路，而是服务于已经发布且稳定的 Agent 场景：

```text
Platform 发布 release
-> 绑定 runner_id / release_id / config_hash
-> Runner 预加载 prompt、active tools、MCP clients
-> 请求进入 /v1/runners/{runner_id}/runs
-> 运行时只做 actor/channel/authz 校验
-> Core Runner 执行
```

## 决策 1：本地工具统一走 MCP stdio

理由：

- 本地工具和外部 MCP 工具使用同一个 adapter 协议，core runner 不需要关心工具实现位置。
- stdio server 能把旧 AiCoin RAG 工具从主进程执行路径隔离出去，避免 runner 默认拥有全部本地能力。
- 数据库里的权限边界仍然按 `tool_id` 判断；`runner_tool_id` 只决定执行通道。

后置影响：

- `source=local` 或 `runner_tool_id=local.<name>` 会在 resolver 中归一化为本机 `mcp+stdio://...aegora_runtime.local_mcp_server`。
- 旧工具能否执行仍由 release active tools、`tools.status`、actor role tool permission 三层决定。
- 如果 stdio server 启动失败，只影响被注入的对应工具，不扩大到未授权工具。

## 决策 2：Runner 预初始化 MCP clients / stdio server

理由：

- 原来每次 MCP 调用都会创建 `fastmcp.Client`，stdio 工具还会重复启动子进程和握手。
- 预初始化后按 `runner_tool_id` 复用 client，能减少工具调用前的固定开销，尤其适合本地 stdio 工具。
- 复用层放在 `tool_adapters.py`，gateway、resolver、core runner 都不需要感知连接生命周期。

后置影响：

- configured runner 构建依赖时会对 runtime context 中的 MCP 工具做 best-effort warmup，并把结果写入 `runtime_context.policy.mcp_warmup`。
- 同一个 `runner_tool_id` 在进程内复用同一个 client；配置变更如果生成新的 `runner_tool_id`，会自然创建新连接。
- HTTP bearer token 如果通过 `bearer_env` 注入且运行中轮换，需要后续提供 runner reload 或 client manager reset 入口。

## 决策 3：暂不做启动扫描注册

理由：

- 本地 registry 只能说明 runner 进程“具备能力”，不能代表某个 Agent “被授权使用能力”。
- 启动扫描后，如果平台数据库中 release、tools status、role permission 变化，runner 进程不会天然感知。
- 先让 resolver 每次以数据库发布快照和运行时权限为准，能避免 registry 与 DB 漂移造成越权。

后置影响：

- `@registry.tool` 和 `@registry.skill` 只作为本地能力声明，不作为权限来源。
- skill 注册先预留接口，暂不进入自动注入链路。
- 后续如果要扫描，只能扫描“能力清单”，不能跳过 DB authz。

## 决策 4：固定配置 Runner 可以做，但只缓存配置，不缓存长期权限事实

理由：

- 固定 Runner 对稳定发布版本有价值：可以提前加载 system prompt、tool schema、MCP client、模型参数。
- 真正省掉的 DB 成本主要是 release/config/tools 几次查询和 JSON 解析，通常是毫秒级；LLM 和工具 I/O 才是主耗时。
- actor active、role、channel permission 这类运行时权限可能随时变化，长期缓存会带来权限滞后风险。

建议入口：

- 平台侧增加 `runner_bindings` 或等价配置：`runner_id`、`agent_id`、`release_id`、`release_version`、`config_hash`、`allowed_channels`、`status`。
- Runner 侧增加 `POST /v1/runners/{runner_id}/runs`，请求只带 `actor_id`、`channel`、`message`、`session_id`、`metadata`。
- Runner 侧增加 `POST /v1/runners/{runner_id}/reload` 和 `GET /v1/runners/{runner_id}/health`，用于配置刷新和预热状态检查。

保留原则：

- 固定 Runner 可以跳过 release 配置解析，但不能跳过 actor/channel/tool runtime authz。
- 如果平台明确把某个 runner 绑定为“专用内网任务 Runner”，也要在配置上显式声明可接受的 channel 和 actor 范围。

## 决策 5：Redis 缓存后置，不在首期直接加入

理由：

- Redis 会新增基础设施依赖、连接配置、失效策略和测试矩阵，不适合在当前改造里直接引入。
- 当前每次解析主要省的是几次查表和 JSON 处理；如果数据库在同 VPC 或本机，收益通常小于 MCP/LLM 调用优化。
- 权限数据对实时性敏感，Redis 没有配套失效事件时容易出现“平台已禁用，runner 仍可用”的窗口。

推荐演进：

1. 进程内 TTL + `config_hash/release_version` 缓存，只缓存 release config 和 active tool manifest，TTL 约 10-30 秒。
2. 保持 actor active、role permission、channel authz 短 TTL 或实时查库。
3. 多实例和高 QPS 后再引入 Redis，并配合平台发布/禁用事件做 pub/sub 失效。
4. 最终可结合 PostgreSQL `LISTEN/NOTIFY` 或平台 webhook，让 runner 主动 reload 指定 `runner_id`。

## 预期收益

- 只做固定配置缓存：主要节省 release/config/tools 查询和 JSON 解析，常见收益约 5-20ms；远程 DB 或无连接池时可能更高。
- 做 MCP stdio client 复用：能省掉每次工具调用的进程启动和握手成本，收益通常比 Redis 配置缓存更直接。
- 做固定 Runner + MCP 预热：收益不只在延迟，还包括启动时暴露配置错误、工具不可用、schema 不一致等问题。

## 决策 6：提供通用工具目录，但不提供默认工具权限

理由：

- 注册只说明 Runner 认识某项能力，不能代表任意 Agent 或 actor 可以使用。
- 不同通用工具的风险差异很大：计算器、时间查询接近纯函数，网页搜索会访问外部数据，Shell 则可能读取 Runner 凭证、文件和内网。
- 如果把“平台注册”和“Release 授权”合并，后续新增工具会被现有 Agent 自动获得，违反最小权限原则。

建议分层：

| 层级 | 示例 | 默认策略 |
| --- | --- | --- |
| `runtime.safe` | 时间、计算器、JSON 校验 | 可由平台策略批量授权，仍需进入 Release |
| `runtime.retrieve` | 网页搜索、外部知识检索 | Release 按需启用 |
| `runtime.compute` | Python、Shell、代码执行 | 默认禁用，必须使用隔离执行服务 |
| `support.*` | FAQ、会员、转人工 | 按业务 Release 授权 |
| `integration.*` | CRM、工单、支付、外部 MCP | 按 provider/group 和 action 授权 |

保留原则：

- 工具必须依次经过“平台注册、Release 注入、resolver 过滤、adapter 执行前鉴权”四个状态，注册成功不等于可执行。
- Resolver 负责静态裁剪：release、tool active、actor/role、tenant、product/domain、channel。
- Adapter 在调用前重新检查容易变化的权限和 scope，数据库禁用或角色撤权后应拒绝已解析但尚未执行的调用。
- 权限策略优先复用 `read_safe`、`external_read`、`business_write`、`sensitive_write`、`isolated_compute` 等少量能力等级，避免为每个工具发明独立权限模型。
- 对外部 MCP 的弱错误和空结果做本轮熔断：连续无可用证据并且已重规划一次后停止继续 tool_call。
  决策依据是 LLM 重规划和外部工具 I/O 是主耗时，重复空结果不会增加事实边界，只会放大 HTTP 超时风险。
- 地图、搜索、天气、距离等查询型 MCP 工具应归为 `external_read` 且允许并行；导入时如果远端缺少
  `readOnlyHint`，平台可用有限名称启发补齐，但写入/下单/发送/删除类名称仍保持需审批。

## 决策 7：网页搜索可以进入公共目录，Shell/代码执行暂缓

网页搜索计划：

- 注册为 `runtime.retrieve.web_search`，但不默认注入所有 Agent。
- 搜索和任意 URL 抓取拆成不同 action，分别授权。
- 限制调用次数、超时、费用、域名和返回体大小，禁止访问本机、内网和云元数据地址。
- 查询参数不得携带凭证和受限数据；外部网页内容必须标记为不可信输入，不能覆盖系统提示词和工具权限。
- 优先通过独立 HTTP/MCP 服务执行，让网络出口、审计和配额不与 Runner 主进程耦合。

Shell/代码执行后置理由：

- MCP stdio 是传输协议，不是安全边界；同用户子进程仍可能读取 Runner 环境变量、数据库凭证、本地文件和内网。
- 在临时容器或 microVM、只读文件系统、环境变量白名单、网络出口控制、CPU/内存/时间配额、输出上限和审计完整之前，不注册通用 Shell 工具。
- 隔离执行能力完成后，将其作为 `runtime.compute` 独立服务接入，而不是在本地 MCP server 中直接调用 `subprocess`。

后续实施顺序：

1. 先补充通用工具风险等级和 capability policy 字段。
2. 实现低风险的时间、计算器等工具及受控网页搜索 adapter。
3. 补充 resolver 与 adapter 双层鉴权测试，覆盖解析后撤权、工具禁用和 scope 变化。
4. 最后单独建设隔离计算服务，再评估 Python/Shell 工具。

## 已实施：MCP 连接独立治理与进程内 Session 生命周期

平台新增 `mcp_connections` 作为连接事实源，`tools.mcp_connection_id` 只维护工具与连接的归属。
Release 继续固化工具权限、schema 和 scope；Runner resolver 根据 connection ID 刷新当前 active
连接配置，因此连接地址、TTL和凭证环境变量引用更新后不需要扩权或重建工具记录。

Runner 使用 `connection_id + config_hash + config_version + credential_version` 作为 session
缓存键。相同连接下多个工具只预热一次；配置版本变化、连接异常、空闲超时或LRU超限时关闭旧
FastMCP Client。`list_tools` 使用独立 discovery TTL。Redis仍不承载活连接，只保留为后续多实例
配置缓存和失效通知方案。

## 已实施：需审批工具的 durable interrupt

决策：

- 审批属于运行态状态机，不属于工具静态 manifest，也不只是一条审计事件。
- Runner 在工具 adapter 执行前拦截 `requires_approval=true`、`internal_write`、`external_write`
  或 `destructive` 工具，保存现场并返回 `pending_approval`。
- 审批通过后恢复保存的原始 `tool_call` 和参数，不重新让 LLM 规划同一个工具调用。
- 恢复执行前重新解析 release、actor、channel、tool active、MCP connection active 和 role scope；
  审批通过只允许本次 pending call 继续，不绕过实时鉴权。
- Resolver 每轮从当前 `tools` 表覆盖运行时治理字段：
  `read_only/idempotent/parallel_safe/requires_approval/side_effect_level/data_sensitivity/network_access/timeout_ms`。
  发布快照仍固化工具列表、schema 和 scope，但平台把工具改为需审批必须立即收紧到已有 release。

落地：

- 表：`runner_approval_requests`
- 运行态入口：`POST /v1/gateway/runs`
- 审批决定入口：`POST /v1/gateway/approvals/{approval_id}/decide`
- 现场快照：message、history、runtime context、planner decision、tool call、已有 observations、
  flow 和 loop_count。
- 上下文注入：审批结果以 `approval_approved` / `approval_rejected` observation 进入后续 planner
  上下文；前端只展示 `tool_args_preview` 和策略摘要，避免把完整 prompt/history 暴露在审批页面。

后置理由：

- 暂不支持审批人编辑 tool args。编辑参数会改变模型原始决策，需要重新校验 schema、scope 和审计语义；
  首期只允许 approved/rejected。
- 批量 tool_call 中若有需审批工具，首期按第一个需审批调用挂起，避免先执行部分工具造成不可回滚副作用。
- 多实例下审批表已经持久化，但正在执行的 resume 仍由接收审批请求的 Runner 执行；后续如果需要异步队列，
  再引入 job worker 和 run 状态查询接口。
- 只实时覆盖治理字段，不实时覆盖 `input_schema/scope/runner_name`。原因是后者会改变发布时的能力和授权边界，
  应通过重新发布生效；前者属于安全收紧策略，必须允许平台即时生效。
