# Agent Loop 核心

当前核心 loop 基于 `pocoflow==0.2.1`，入口在 `src/aegora_runtime/agent_loop.py`。

## 拓扑

```text
LoadContext
  -> ClassifyIntent
  -> HybridSearch
  -> Think
      -> tool_call -> ExecuteTool -> Think
      -> faq_answer -> SelfCheck
      -> clarify    -> SelfCheck
      -> handoff    -> SelfCheck
```

`Think` 节点的循环次数由 `settings.agent.max_iterations` 控制，默认 8 次。
工具执行失败也会回到 `Think`，由 Agent 基于错误 observation 决定重试、反问或转人工。

## 依赖注入

业务能力通过 `AgentDependencies` 注入：

|依赖|用途|
|---|---|
|`load_context`|读取用户画像、记忆、最近摘要|
|`classify_intent`|轻模型或规则做意图分类|
|`retrieve`|混合检索 FAQ，后续接 PG `vector + tsvector`|
|`think`|主模型 ReAct 决策：回答、工具、反问、转人工|
|`run_tool`|执行业务工具|
|`self_check`|检查答案依据、完整性和安全性|

这样核心编排不依赖具体模型、数据库或工具实现，后续替换依赖即可。

## Hooks 和日志

Pocoflow 提供 flow 级 hook：

|Hook|当前处理|
|---|---|
|`node_start`|写入 `store["observability"]`，输出结构化日志|
|`node_end`|记录节点 action 和耗时|
|`node_error`|记录异常类型和错误信息|
|`flow_end`|记录总步数、最终状态和路由|

项目 hook 入口在 `src/aegora_runtime/hooks.py`，由 planner flow 默认挂载。

统一日志入口在 `src/aegora_runtime/logging.py`：

```python
from aegora_runtime.logging import setup_logging

setup_logging(log_dir="logs", console=False)
```

这会初始化项目日志，并调用 Pocoflow 自带的 `pocoflow.logging.setup_logging(...)` 生成文件日志。

## 工具生命周期

工具系统在 `src/aegora_runtime/tools.py`，不是 Pocoflow 内置业务概念，而是项目层定义。`RuntimeToolExecutor` 只执行本轮已注入的 `ToolSpec`，真实工具实现由 `ToolSpec.handler` 继续分发到本地 registry 或 MCP。

生命周期：

```text
validate_args
  -> before_call hooks
  -> handler
  -> after_call hooks
```

异常路径：

```text
validate_args / handler error
  -> on_error hooks
  -> retry if configured
  -> ToolError
  -> ExecuteToolNode records error observation
  -> Think
```

工具定义：

```python
ToolSpec(
    name="lookup_order",
    description="lookup order by id",
    input_schema={"id": str},
    handler=lambda args, state: {"order_id": args["id"]},
)
```

## 测试

```bash
PYTHONPATH=src conda run -n agentic-rag python -m unittest tests.test_agent_loop
```
