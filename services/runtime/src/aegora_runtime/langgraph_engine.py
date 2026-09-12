from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from aegora_runtime.agent_loop import (
    AgentDependencies,
    AgentRequest,
    ExecuteToolNode,
    LoadContextNode,
    LoadSkillsNode,
    PreGuardNode,
    SelfCheckNode,
    ThinkNode,
    make_store,
    skill_observation_summary,
)
from aegora_runtime.config import Settings
from aegora_runtime.execution_engine import attach_engine_metadata, ensure_trace_id
from aegora_runtime.logging import get_logger, log_event
from aegora_runtime.observability import agent_trace_scope, finish_agent_trace
from aegora_runtime.retrieval import trusted_top1_images


LOGGER = get_logger("langgraph_engine")
_MEMORY_SAVER = InMemorySaver()
_RUNTIME_ONLY_KEYS = {"request", "deps", "stream_handler"}


class PlannerState(TypedDict, total=False):
    tool_catalog: list[dict[str, Any]]
    loop_mode: str
    trace_id: str
    run_context: dict[str, Any]
    trace: list[dict[str, Any]]
    observability: list[dict[str, Any]]
    context: dict[str, Any]
    skills: list[dict[str, Any]]
    auto_skills: list[dict[str, Any]]
    session_skills: list[dict[str, Any]]
    skill_retrieval: dict[str, Any]
    intent: dict[str, Any]
    retrieved_faqs: list[dict[str, Any]]
    tool_observations: list[dict[str, Any]]
    loop_count: int
    route: str | None
    answer: str | None
    status: str
    errors: list[str]
    decision: dict[str, Any]
    self_check: dict[str, Any]
    approval_requests: list[dict[str, Any]]


@dataclass(frozen=True)
class EngineRuntimeContext:
    request: AgentRequest
    dependencies: AgentDependencies
    settings: Settings
    runtime_context: dict[str, object] | None = None


class LangGraphEngine:
    name = "langgraph"
    schema_version = "planner-v1"

    def start(
        self,
        request: AgentRequest,
        dependencies: AgentDependencies,
        settings: Settings,
        *,
        initial_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        runtime_context: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        request = ensure_trace_id(request)
        thread_id = str((metadata or {}).get("engine_thread_id") or request.trace_id)
        state = initial_persisted_state(request, dependencies)
        for key, value in (initial_state or {}).items():
            if key not in _RUNTIME_ONLY_KEYS:
                state[key] = value
        return self._invoke(
            input_value=state,
            request=request,
            dependencies=dependencies,
            settings=settings,
            thread_id=thread_id,
            runtime_context=runtime_context,
        )

    def resume(
        self,
        *,
        thread_id: str,
        payload: dict[str, Any],
        request: AgentRequest,
        dependencies: AgentDependencies,
        settings: Settings,
        metadata: dict[str, Any] | None = None,
        runtime_context: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        request = ensure_trace_id(request)
        return self._invoke(
            input_value=Command(resume=payload),
            request=request,
            dependencies=dependencies,
            settings=settings,
            thread_id=thread_id,
            runtime_context=runtime_context,
        )

    def _invoke(
        self,
        *,
        input_value: PlannerState | Command,
        request: AgentRequest,
        dependencies: AgentDependencies,
        settings: Settings,
        thread_id: str,
        runtime_context: dict[str, object] | None,
    ) -> dict[str, Any]:
        usage_before = dependencies.model_usage() if dependencies.model_usage else None
        context = EngineRuntimeContext(
            request=request,
            dependencies=dependencies,
            settings=settings,
            runtime_context=runtime_context,
        )
        config = {"configurable": {"thread_id": thread_id}}
        with checkpoint_saver(settings) as saver:
            graph = build_langgraph(settings, saver)
            with agent_trace_scope(request, settings) as langfuse_span:
                output = dict(graph.invoke(input_value, config, context=context))
                snapshot = graph.get_state(config)
                checkpoint_id = checkpoint_id_from_config(snapshot.config)
                attach_engine_metadata(output, self, thread_id=thread_id, checkpoint_id=checkpoint_id)
                output["trace_id"] = request.trace_id
                output["model_thinking_enabled"] = settings.deepseek.enable_thinking
                output["answer_assets"] = trusted_top1_images(
                    output.get("retrieved_faqs") or [],
                    output.get("route"),
                )
                if dependencies.model_usage and usage_before is not None:
                    output["model_usage"] = diff_usage(usage_before, dependencies.model_usage())
                finish_agent_trace(langfuse_span, output)
        log_event(
            LOGGER,
            logging.INFO,
            "execution_engine_snapshot",
            trace_id=request.trace_id,
            session_id=request.session_id,
            user_id=request.user_id,
            execution_engine=self.name,
            engine_schema_version=self.schema_version,
            engine_thread_id=thread_id,
            checkpoint_id=output.get("checkpoint_id"),
            status=output.get("status"),
            route=output.get("route"),
            loop_count=output.get("loop_count"),
            loaded_skills=[skill_observation_summary(item) for item in output.get("skills") or []],
            tool_observations=output.get("tool_observations") or [],
        )
        return output


def build_langgraph(settings: Settings, saver: BaseCheckpointSaver):
    graph = StateGraph(PlannerState, context_schema=EngineRuntimeContext)
    graph.add_node("load_context", _node_adapter(LoadContextNode()))
    graph.add_node("load_skills", _node_adapter(LoadSkillsNode()))
    graph.add_node("pre_guard", _node_adapter(PreGuardNode()))
    graph.add_node("think", _node_adapter(ThinkNode(max_iterations=settings.agent.max_iterations)))
    graph.add_node("execute_tool", _node_adapter(ExecuteToolNode()))
    graph.add_node("await_approval", await_approval)
    graph.add_node("self_check", _node_adapter(SelfCheckNode()))

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "load_skills")
    graph.add_edge("load_skills", "pre_guard")
    graph.add_edge("pre_guard", "think")
    graph.add_conditional_edges(
        "think",
        route_after_think,
        {"execute_tool": "execute_tool", "self_check": "self_check"},
    )
    graph.add_conditional_edges(
        "execute_tool",
        route_after_tool,
        {"await_approval": "await_approval", "think": "think"},
    )
    graph.add_conditional_edges(
        "await_approval",
        route_after_approval,
        {"think": "think", "self_check": "self_check"},
    )
    graph.add_edge("self_check", END)
    return graph.compile(checkpointer=saver)


def initial_persisted_state(request: AgentRequest, dependencies: AgentDependencies) -> PlannerState:
    store = make_store(request, dependencies, "planner")
    return persisted_state(store.as_dict())


def _node_adapter(node):
    def invoke_node(state: PlannerState, runtime: Runtime[EngineRuntimeContext]) -> PlannerState:
        store = make_store(runtime.context.request, runtime.context.dependencies, "planner")
        for key, value in state.items():
            if key not in _RUNTIME_ONLY_KEYS:
                store[key] = value
        if runtime.context.runtime_context:
            transient_context = dict(store.get("context") or {})
            transient_context["runtime_context"] = runtime.context.runtime_context
            store["context"] = transient_context
        prep_result = node.prep(store)
        exec_result = node.exec(prep_result)
        node.post(store, prep_result, exec_result)
        return persisted_state(store.as_dict())

    return invoke_node


def persisted_state(data: dict[str, Any]) -> PlannerState:
    persisted = {key: value for key, value in data.items() if key not in _RUNTIME_ONLY_KEYS}
    context = persisted.get("context")
    if isinstance(context, dict) and "runtime_context" in context:
        persisted["context"] = {key: value for key, value in context.items() if key != "runtime_context"}
    return persisted  # type: ignore[return-value]


def route_after_think(state: PlannerState) -> str:
    return "execute_tool" if state.get("route") == "tool_call" else "self_check"


def route_after_tool(state: PlannerState) -> str:
    return "await_approval" if state.get("status") == "pending_approval" else "think"


def await_approval(state: PlannerState) -> PlannerState:
    approval_requests = state.get("approval_requests") or []
    payload = interrupt(
        {
            "type": "tool_approval",
            "approval_requests": approval_requests,
            "trace_id": state.get("trace_id"),
        }
    )
    if not isinstance(payload, dict):
        payload = {"decision": "rejected", "reason": "invalid_resume_payload"}
    decision = str(payload.get("decision") or "rejected")
    observations = list(state.get("tool_observations") or [])
    approval_observation = payload.get("approval_observation")
    if isinstance(approval_observation, dict):
        observations.append(approval_observation)
    tool_results = payload.get("tool_results")
    if isinstance(tool_results, list):
        observations.extend(item for item in tool_results if isinstance(item, dict))
    if decision == "approved":
        return {
            **state,
            "tool_observations": observations,
            "approval_requests": [],
            "status": "running",
            "route": "tool_call",
            "answer": None,
        }
    return {
        **state,
        "tool_observations": observations,
        "approval_requests": [],
        "status": "running",
        "route": "answer",
        "answer": str(payload.get("answer") or "审批已拒绝，本次工具调用不会执行。"),
    }


def route_after_approval(state: PlannerState) -> str:
    return "think" if state.get("route") == "tool_call" else "self_check"


@contextmanager
def checkpoint_saver(settings: Settings) -> Iterator[BaseCheckpointSaver]:
    if not settings.agent.langgraph_checkpoints_enabled:
        yield _MEMORY_SAVER
        return
    database_url = settings.agent.langgraph_checkpoint_database_url
    if not database_url:
        raise RuntimeError("LANGGRAPH_CHECKPOINT_DATABASE_URL 未配置")
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(database_url) as saver:
        saver.setup()
        yield saver


def checkpoint_id_from_config(config: dict[str, Any] | None) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get("checkpoint_id")
    return str(value) if value else None


def diff_usage(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in set(before) | set(after):
        left = before.get(key)
        right = after.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            result[key] = right - left
        elif right != left:
            result[key] = right
    return result
