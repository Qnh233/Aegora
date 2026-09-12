from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol
from uuid import uuid4

from aegora_runtime.agent_loop import AgentDependencies, AgentRequest, run_agent
from aegora_runtime.config import ConfigError, Settings


class ExecutionEngine(Protocol):
    """Engine-neutral contract for one Agent execution lifecycle.

    Platform governance, tool authorization, audit records and durable approval facts
    live outside this boundary. Engines only own orchestration/execution state.
    """

    name: str
    schema_version: str

    def start(
        self,
        request: AgentRequest,
        dependencies: AgentDependencies,
        settings: Settings,
        *,
        initial_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        runtime_context: dict[str, object] | None = None,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...


class PocoFlowEngine:
    name = "pocoflow"
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
        result = run_agent(
            request,
            dependencies,
            settings,
            loop_mode="planner",
            enable_pocoflow_db=False,
            initial_state=initial_state,
        )
        return attach_engine_metadata(result, self, thread_id=str(request.trace_id))

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
        raise ConfigError("PocoFlowEngine 不支持 engine-native durable resume；审批仍由 Aegora snapshot 兼容路径恢复")


def ensure_trace_id(request: AgentRequest) -> AgentRequest:
    if request.trace_id:
        return request
    return replace(request, trace_id=f"turn-{uuid4().hex}")


def attach_engine_metadata(
    result: dict[str, Any],
    engine: ExecutionEngine,
    *,
    thread_id: str,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    result["execution_engine"] = engine.name
    result["engine_schema_version"] = engine.schema_version
    result["engine_thread_id"] = thread_id
    if checkpoint_id:
        result["checkpoint_id"] = checkpoint_id
    return result


def get_execution_engine(settings: Settings, engine_name: str | None = None) -> ExecutionEngine:
    selected = (engine_name or settings.agent.execution_engine).strip().lower()
    if selected == "pocoflow":
        return PocoFlowEngine()
    if selected == "langgraph":
        # Keep LangGraph optional at import time so the legacy engine can still boot
        # when a minimal environment intentionally excludes LangGraph dependencies.
        from aegora_runtime.langgraph_engine import LangGraphEngine

        return LangGraphEngine()
    raise ConfigError(f"不支持的 execution engine: {selected}")
