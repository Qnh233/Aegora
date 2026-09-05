# Legacy Migration Record

## 来源

- Control Plane: `I:/devspaceGPT/andian/Agent-Platform/agent-platform`
- Runtime: `I:/devspaceGPT/andian/agentic rag`

## 迁移原则

1. 历史仓库保持只读，不在迁移中修改。
2. 不迁移 `.env`、日志、缓存、构建产物、node_modules 与大型业务原始数据。
3. Runtime Python 包从 `agentic_rag` 重命名为 `aegora_runtime`，避免新仓继续暴露旧业务品牌。
4. 客服 RAG 作为 Runtime 内的参考业务能力保留，但不作为 Runtime Core 的默认权限来源。
5. Control Plane 与 Runtime 通过数据库 Release + Shared Contracts 对接，不通过源码互相 import。

## 第一阶段目标

- 完成代码迁入并保证核心测试可运行；
- 统一根配置；
- 修复路径与包名；
- 建立跨服务合同包；
- 建立仓库级验证脚本。

LiteLLM Proxy 与 Langfuse 的基础接入已在迁移后的统一 Runtime/Deploy 边界内落地，Staging 进一步要求显式验证过的不可变 LiteLLM 镜像 Digest。Workflow Capability 第一阶段已经合入 `main`，采用“工作流语义 + MCP 执行适配器”的方式接入。当前集成候选同时纳入 Redis 与 Governed Learning 两条已完成 Roadmap 分支：Redis 仅缓存不可变 Release 静态配置，采用有界进程内 L1 + 版本化 Redis L2，并通过 v1 事件契约、Control Plane best-effort Publisher 与 Runtime Subscriber 做发布/撤销/工具策略精确失效；Release 状态、RBAC、工具与 MCP 当前治理事实仍实时读取 PostgreSQL。Learning Flywheel 已补齐 Agent/Release 证据血缘、Release 级 per-Agent learning policy，以及 Agent Skill 的晋级硬门禁：反思禁止跨 Agent 聚类，证据采集可关闭，Skill 提案显式 opt-in；`source=agent` Skill 若缺少绑定候选内容的通过评测、显式 criteria、通过的基线回归、绑定同一候选内容的 Canary 证据或人工审核者，则无论本地 publish 还是 Strapi 同步都不能进入 `active`。后续重点是 Workflow 生命周期、真实受控流量 Canary 证据采集、确有必要时的 MCP Session 收敛，以及 LiteLLM/Langfuse 的多 Provider、预算与评测看板硬化。

