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

LiteLLM Proxy 与 Langfuse 的基础接入已在迁移后的统一 Runtime/Deploy 边界内落地。Workflow Capability 第一阶段采用“工作流语义 + MCP 执行适配器”的方式接入：控制面注册受治理 Workflow Capability，Runtime 继续复用成熟的 MCP 调用路径，避免为工作流另造一套协议。后续再逐步实现 Workflow 生命周期、Learning Flywheel、Redis L1/L2，以及模型网关/Trace 的生产级硬化，避免多个高风险能力同时大改造成不可验证状态。

