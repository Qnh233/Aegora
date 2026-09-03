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

LiteLLM Proxy 与 Langfuse 的基础接入已在迁移后的统一 Runtime/Deploy 边界内落地。Workflow Capability 第一阶段已通过独立 Roadmap PR 实现“工作流语义 + MCP 执行适配器”，尚未自动合并 main。Redis 缓存阶段则只缓存不可变 Release 静态配置，并刻意把 Release 状态、RBAC、工具与 MCP 当前治理事实留在 PostgreSQL 实时读取路径，避免缓存扩大权限或延迟撤销；当前 Roadmap 分支已形成有界进程内 L1 + 版本化 Redis L2，并加入 v1 版本化事件契约、控制面 best-effort 发布器、Runtime 订阅失效路径，以及事件计数/消费延迟/订阅重连失败的 Prometheus 指标。后续再逐步实现 Workflow 生命周期、控制面 Publisher 投递观测与必要的 MCP Session 收敛、Learning Flywheel，以及模型网关/Trace 的生产级硬化，避免多个高风险能力同时大改造成不可验证状态。

