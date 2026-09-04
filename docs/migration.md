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

LiteLLM Proxy 与 Langfuse 的基础接入已在迁移后的统一 Runtime/Deploy 边界内落地。Learning Flywheel 已补齐证据血缘、第一阶段 per-Agent learning policy，以及 Agent Skill 的基础晋级门禁：配置化 Agent 的会话会持久化 Agent/Release 身份与 Release 级学习策略快照，反思聚类禁止跨 Agent 混合，可按 Agent 关闭证据采集，Skill 草稿生成采用显式 opt-in；未授权的高价值聚类仅进入人工学习复核。`source=agent` Skill 若缺少通过的评测记录或人工审核者，则无论本地 publish 还是 Strapi 同步都不能进入 `active`；评测脚本现在可为单条未发布候选自动生成绑定数据集哈希与 Skill 内容哈希的证据，评测后内容变化会使原证据失效。后续继续实现 Canary 与更完整的晋级策略。Workflow Registry、Redis L1/L2 与模型网关/Trace 的生产级硬化仍按独立阶段推进，避免多个高风险能力同时大改造成不可验证状态。

