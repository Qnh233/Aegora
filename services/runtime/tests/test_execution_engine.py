from __future__ import annotations

from dataclasses import replace
import os
from uuid import uuid4

import pytest

from aegora_runtime.agent_loop import AgentDecision, AgentDependencies, AgentRequest, default_self_check
from aegora_runtime.config import ConfigError, load_settings
from aegora_runtime.execution_engine import PocoFlowEngine, get_execution_engine
from aegora_runtime.langgraph_engine import LangGraphEngine, checkpoint_saver, persisted_state
from aegora_runtime.observability import finish_agent_trace


def _settings(*, engine: str = "pocoflow", checkpoints: bool = False, database_url: str | None = None):
    settings = load_settings(env_path=None)
    return replace(
        settings,
        agent=replace(
            settings.agent,
            execution_engine=engine,
            langgraph_checkpoints_enabled=checkpoints,
            langgraph_checkpoint_database_url=database_url,
        ),
    )


def _skills(_request, _context):
    return {
        "query": "q",
        "candidates": [],
        "skills": [],
        "auto_skills": [],
        "session_skills": [],
        "skipped": True,
        "reason": "test",
    }


def _direct_dependencies(answer: str = "ok") -> AgentDependencies:
    return AgentDependencies(
        load_context=lambda request: {"history": request.history},
        load_skills=_skills,
        think=lambda state: AgentDecision(route="answer", answer=answer, reason="direct"),
        run_tool=lambda name, args, state: {"status": "ok", "output": {}},
        self_check=default_self_check,
        pre_guard=lambda request, context: {"route_hint": "planner", "reason": "test"},
    )


def _tool_dependencies() -> AgentDependencies:
    def think(state):
        if not state.get("tool_observations"):
            return AgentDecision(
                route="tool_call",
                tool_name="lookup_order",
                tool_args={"id": "A1"},
                reason="need_order",
            )
        return AgentDecision(route="answer", answer="paid", reason="tool_complete")

    return AgentDependencies(
        load_context=lambda request: {"history": request.history},
        load_skills=_skills,
        think=think,
        run_tool=lambda name, args, state: {
            "status": "ok",
            "output": {"order_id": args["id"], "state": "paid"},
        },
        self_check=default_self_check,
        pre_guard=lambda request, context: {"route_hint": "planner", "reason": "test"},
    )


def _semantic(result: dict) -> dict:
    return {
        "status": result.get("status"),
        "route": result.get("route"),
        "answer": result.get("answer"),
        "loop_count": result.get("loop_count"),
        "tool_observations": result.get("tool_observations"),
        "trace_nodes": [item.get("node") for item in result.get("trace") or []],
    }


@pytest.mark.parametrize(
    ("query", "factory"),
    [("hello", _direct_dependencies), ("lookup order", _tool_dependencies)],
)
def test_pocoflow_and_langgraph_have_same_core_semantics(query, factory) -> None:
    settings = _settings()
    poco = PocoFlowEngine().start(AgentRequest(query=query), factory(), settings)
    langgraph = LangGraphEngine().start(AgentRequest(query=query), factory(), settings)

    assert _semantic(langgraph) == _semantic(poco)
    assert poco["execution_engine"] == "pocoflow"
    assert langgraph["execution_engine"] == "langgraph"
    assert langgraph["checkpoint_id"]


def test_execution_engine_selector_and_unknown_engine() -> None:
    assert isinstance(get_execution_engine(_settings(engine="pocoflow")), PocoFlowEngine)
    assert isinstance(get_execution_engine(_settings(engine="langgraph")), LangGraphEngine)
    with pytest.raises(ConfigError, match="execution engine"):
        get_execution_engine(_settings(), "unknown")


def test_persisted_state_excludes_runtime_only_and_live_governance() -> None:
    persisted = persisted_state(
        {
            "request": object(),
            "deps": object(),
            "stream_handler": object(),
            "trace_id": "t1",
            "context": {
                "history": [{"role": "user", "content": "q"}],
                "runtime_context": {"actor": {"actor_id": "u1"}, "tool_ids": ["danger.write"]},
            },
            "decision": {"route": "answer"},
        }
    )

    assert "request" not in persisted
    assert "deps" not in persisted
    assert "stream_handler" not in persisted
    assert "runtime_context" not in persisted["context"]
    assert persisted["context"]["history"][0]["content"] == "q"


def _approval_dependencies() -> AgentDependencies:
    def think(state):
        if any(item.get("status") == "ok" for item in state.get("tool_observations") or []):
            return AgentDecision(route="answer", answer="write completed", reason="approved_result")
        return AgentDecision(
            route="tool_call",
            tool_name="danger.write",
            tool_args={"id": "A1"},
            reason="write_needed",
        )

    def run_many(calls, state):
        call = calls[0]
        return {
            "status": "pending_approval",
            "execution_mode": "approval_required",
            "answer": "needs approval",
            "approval_requests": [{"approval_id": "approval-1"}],
            "results": [
                {
                    "call_id": call["call_id"],
                    "tool_name": call["tool_name"],
                    "args": call.get("tool_args") or {},
                    "status": "pending_approval",
                    "approval_id": "approval-1",
                }
            ],
        }

    return AgentDependencies(
        load_context=lambda request: {},
        load_skills=_skills,
        think=think,
        run_tool=lambda name, args, state: pytest.fail("single tool path should not execute"),
        run_tools=run_many,
        self_check=default_self_check,
        pre_guard=lambda request, context: {"route_hint": "planner", "reason": "test"},
    )


def test_langgraph_interrupt_can_resume_in_new_engine_instance_with_approved_result() -> None:
    settings = _settings(engine="langgraph")
    request = AgentRequest(query="write", trace_id="approval-thread-approved")

    pending = LangGraphEngine().start(request, _approval_dependencies(), settings)
    assert pending["status"] == "pending_approval"
    assert pending["checkpoint_id"]

    resumed = LangGraphEngine().resume(
        thread_id="approval-thread-approved",
        payload={
            "decision": "approved",
            "approval_observation": {
                "call_id": "tool-call-1",
                "tool_name": "danger.write",
                "status": "approval_approved",
            },
            "tool_results": [
                {
                    "call_id": "tool-call-1",
                    "tool_name": "danger.write",
                    "args": {"id": "A1"},
                    "status": "ok",
                    "output": {"written": True},
                }
            ],
        },
        request=request,
        dependencies=_approval_dependencies(),
        settings=settings,
    )

    assert resumed["status"] == "completed"
    assert resumed["route"] == "answer"
    assert resumed["answer"] == "write completed"
    assert any(item.get("status") == "approval_approved" for item in resumed["tool_observations"])
    assert any(item.get("status") == "ok" for item in resumed["tool_observations"])


def test_langgraph_interrupt_can_resume_rejected_without_tool_result() -> None:
    settings = _settings(engine="langgraph")
    request = AgentRequest(query="write", trace_id="approval-thread-rejected")
    LangGraphEngine().start(request, _approval_dependencies(), settings)

    resumed = LangGraphEngine().resume(
        thread_id="approval-thread-rejected",
        payload={
            "decision": "rejected",
            "approval_observation": {
                "call_id": "tool-call-1",
                "tool_name": "danger.write",
                "status": "approval_rejected",
            },
            "answer": "rejected by reviewer",
        },
        request=request,
        dependencies=_approval_dependencies(),
        settings=settings,
    )

    assert resumed["status"] == "completed"
    assert resumed["route"] == "answer"
    assert resumed["answer"] == "rejected by reviewer"
    assert not any(item.get("status") == "ok" for item in resumed["tool_observations"])


def test_postgres_checkpoint_mode_requires_database_url() -> None:
    settings = _settings(engine="langgraph", checkpoints=True, database_url=None)
    with pytest.raises(RuntimeError, match="LANGGRAPH_CHECKPOINT_DATABASE_URL"):
        with checkpoint_saver(settings):
            pass


def test_postgres_checkpoint_saver_setup_is_invoked(monkeypatch) -> None:
    import langgraph.checkpoint.postgres as checkpoint_postgres

    calls: dict[str, object] = {}

    class FakeSaver:
        def setup(self):
            calls["setup"] = True

    class FakeContext:
        def __enter__(self):
            calls["enter"] = True
            return FakeSaver()

        def __exit__(self, exc_type, exc, tb):
            calls["exit"] = True

    class FakePostgresSaver:
        @classmethod
        def from_conn_string(cls, database_url):
            calls["database_url"] = database_url
            return FakeContext()

    monkeypatch.setattr(checkpoint_postgres, "PostgresSaver", FakePostgresSaver)
    settings = _settings(
        engine="langgraph",
        checkpoints=True,
        database_url="postgresql://user:secret@db.example/aegora",
    )

    with checkpoint_saver(settings) as saver:
        assert isinstance(saver, FakeSaver)

    assert calls == {
        "database_url": "postgresql://user:secret@db.example/aegora",
        "enter": True,
        "setup": True,
        "exit": True,
    }


@pytest.mark.skipif(
    not os.getenv("LANGGRAPH_TEST_DATABASE_URL"),
    reason="requires PostgreSQL for durable LangGraph checkpoint integration",
)
def test_postgres_checkpoint_survives_new_engine_instance() -> None:
    database_url = os.environ["LANGGRAPH_TEST_DATABASE_URL"]
    settings = _settings(engine="langgraph", checkpoints=True, database_url=database_url)
    thread_id = f"pg-approval-{uuid4().hex}"
    request = AgentRequest(query="write", trace_id=thread_id)

    pending = LangGraphEngine().start(request, _approval_dependencies(), settings)
    assert pending["status"] == "pending_approval"
    assert pending["checkpoint_id"]

    resumed = LangGraphEngine().resume(
        thread_id=thread_id,
        payload={
            "decision": "approved",
            "approval_observation": {
                "call_id": "tool-call-1",
                "tool_name": "danger.write",
                "status": "approval_approved",
            },
            "tool_results": [
                {
                    "call_id": "tool-call-1",
                    "tool_name": "danger.write",
                    "args": {"id": "A1"},
                    "status": "ok",
                    "output": {"written": True},
                }
            ],
        },
        request=request,
        dependencies=_approval_dependencies(),
        settings=settings,
    )

    assert resumed["status"] == "completed"
    assert resumed["answer"] == "write completed"
    assert resumed["engine_thread_id"] == thread_id
    assert resumed["checkpoint_id"]


def test_langfuse_finish_records_engine_checkpoint_correlation() -> None:
    class FakeObservation:
        def __init__(self):
            self.kwargs = None

        def update(self, **kwargs):
            self.kwargs = kwargs

    observation = FakeObservation()
    finish_agent_trace(
        observation,
        {
            "status": "completed",
            "route": "answer",
            "answer": "ok",
            "tool_observations": [],
            "execution_engine": "langgraph",
            "engine_schema_version": "planner-v1",
            "engine_thread_id": "thread-1",
            "checkpoint_id": "checkpoint-1",
        },
    )

    assert observation.kwargs["metadata"] == {
        "intent": "",
        "tool_call_count": "0",
        "execution_engine": "langgraph",
        "engine_schema_version": "planner-v1",
        "engine_thread_id": "thread-1",
        "checkpoint_id": "checkpoint-1",
    }
