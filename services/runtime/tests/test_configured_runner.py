from __future__ import annotations

import json

from aegora_runtime.config import load_settings
from aegora_runtime.configured_runner import (
    build_configured_messages,
    build_configured_dependencies,
    configured_planner_think,
    decide_approval,
    load_configured_context,
    parse_configured_decision,
    run_configured_turn,
    stop_replanning_after_unusable_tools,
)
from aegora_runtime.runtime_approvals import ApprovalDecision, InMemoryApprovalStore


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.messages = []
        self.models = []
        self.usage = {"total_calls": 0, "failed_calls": 0, "by_model": {}}

    def chat_json(self, messages, *, model=None, temperature=0.0):
        self.messages.append(messages)
        self.models.append(model)
        self.usage["total_calls"] += 1
        return self.payloads.pop(0)

    def usage_snapshot(self):
        return dict(self.usage)


def runtime_context_fixture() -> dict[str, object]:
    return {
        "release": {"release_id": "rel_1", "agent_id": "agent_1", "version": 1, "status": "published"},
        "agent": {
            "id": "agent_1",
            "name": "calc-agent",
            "system_prompt": "你是计算助手，只做准确计算。",
            "model": "deepseek-v4-pro",
            "enabled": True,
            "channels": ["web_console"],
        },
        "actor": {"actor_id": "u_1"},
        "channel": "web_console",
        "tools": [
            {
                "tool_id": "calculator",
                "runner_tool_id": "local.calculator",
                "name": "计算器",
                "description": "执行算术",
                "input_schema": {"type": "object", "required": ["expression"]},
                "read_only": True,
                "parallel_safe": True,
                "idempotent": True,
            }
        ],
        "tool_ids": ["calculator"],
        "tool_scopes": {"calculator": {"actions": ["calculate"]}},
    }


def approval_runtime_context_fixture(
    tool_id: str = "approval.echo",
    runner_tool_id: str = "local.approval_echo",
) -> dict[str, object]:
    context = runtime_context_fixture()
    context["tools"] = [
        {
            "tool_id": tool_id,
            "runner_tool_id": runner_tool_id,
            "name": "审批回声",
            "description": "需要审批后回显",
            "input_schema": {"type": "object", "required": ["text"]},
            "read_only": False,
            "parallel_safe": False,
            "idempotent": False,
            "requires_approval": True,
            "side_effect_level": "external_write",
        }
    ]
    context["tool_ids"] = [tool_id]
    context["tool_scopes"] = {tool_id: {"actions": ["write"]}}
    return context


def runtime_context_with_load_skill() -> dict[str, object]:
    context = runtime_context_fixture()
    context["tools"] = [
        {
            "tool_id": "runtime.load_skill",
            "runner_tool_id": "local.load_skill",
            "runner_name": "load_skill",
            "name": "加载经验",
            "description": "加载授权经验",
            "read_only": True,
            "parallel_safe": True,
            "idempotent": True,
        }
    ]
    context["tool_ids"] = ["runtime.load_skill"]
    context["tool_scopes"] = {
        "runtime.load_skill": {
            "product_ids": ["aicoin"],
            "domains": ["customer_support"],
            "skill_library_ids": ["aicoin_customer_support"],
        }
    }
    return context


def test_configured_runner_injects_release_prompt_model_and_tools() -> None:
    client = FakeClient([{"route": "answer", "answer": "结果是 7", "reason": "done"}])
    decision = configured_planner_think(
        {
            "request": type("Req", (), {"query": "1+2*3"})(),
            "context": {"runtime_context": runtime_context_fixture()},
            "tool_catalog": [{"name": "calculator", "description": "执行算术"}],
            "tool_observations": [],
        },
        client,
        load_settings(env_path=None),
    )

    assert decision.route == "answer"
    assert decision.answer == "结果是 7"
    assert client.models == ["deepseek-v4-pro"]
    assert client.messages[0][0].content == "你是计算助手，只做准确计算。"
    assert "calculator" in client.messages[0][-1].content


def test_configured_context_builds_skill_index_from_runtime_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        "aegora_runtime.configured_runner.skill_index_for_scope",
        lambda scope, settings: [{"name": "exchange_auth", "title": "交易所授权"}],
    )

    context = load_configured_context(
        type("Req", (), {"history": []})(),
        runtime_context_with_load_skill(),
        load_settings(env_path=None),
    )

    assert context["skill_index"] == [{"name": "exchange_auth", "title": "交易所授权"}]
    assert context["skill_index_source"] == "runtime_tool_scope"


def test_configured_context_loads_session_and_session_user_views(monkeypatch) -> None:
    monkeypatch.setattr(
        "aegora_runtime.configured_runner.skill_index_for_scope",
        lambda scope, settings: [],
    )
    monkeypatch.setattr(
        "aegora_runtime.configured_runner.load_session_context_views",
        lambda settings, session_id, user_id, session_limit=8, user_limit=8: {
            "session_context": [{"message_key": "m2", "role": "assistant", "content": "群上下文"}],
            "session_user_context": [{"message_key": "m1", "role": "user", "content": "该用户上下文"}],
        },
    )

    context = load_configured_context(
        type("Req", (), {"history": [], "session_id": "group-1", "user_id": "u-1"})(),
        runtime_context_fixture(),
        load_settings(env_path=None),
    )

    assert context["session_context"][0]["message_key"] == "m2"
    assert context["session_user_context"][0]["message_key"] == "m1"


def test_build_configured_messages_includes_classified_short_term_context() -> None:
    messages = build_configured_messages(
        {
            "request": type("Req", (), {"query": "会员权益", "session_id": "group-1", "user_id": "u-1"})(),
            "context": {
                "history": [],
                "session_context": [{"message_key": "m2", "role": "assistant", "content": "群上下文"}],
                "session_user_context": [{"message_key": "m1", "role": "user", "content": "该用户上下文"}],
            },
            "tool_catalog": [],
            "tool_observations": [],
        },
        runtime_context_fixture(),
    )

    payload = json.loads(messages[-1].content)
    assert payload["session_context"][0]["message_key"] == "m2"
    assert payload["session_user_context"][0]["message_key"] == "m1"


def test_run_configured_turn_preserves_scope_skill_index_for_planner(monkeypatch) -> None:
    index = [{"name": "membership_rights", "title": "会员权益", "summary": "会员权益说明"}]
    client = FakeClient([{"route": "answer", "answer": "可以查看会员权益经验。"}])
    monkeypatch.setattr("aegora_runtime.configured_runner.skill_index_for_scope", lambda scope, settings: index)
    monkeypatch.setattr("aegora_runtime.configured_runner.DeepSeekClient", lambda _settings: client)

    result = run_configured_turn(
        runtime_context_with_load_skill(),
        message="你有什么经验skill？",
        session_id="s1",
        history=[],
        settings=load_settings(env_path=None),
    )
    payload = json.loads(client.messages[0][-1].content)
    retrieval = next(item for item in result["observability"] if item.get("event") == "skill_retrieval")

    assert payload["skill_index"] == index
    assert retrieval["skill_index"] == index
    assert retrieval["reason"] == "runtime_tool_scope"


def test_parse_configured_decision_rejects_unknown_tool() -> None:
    decision = parse_configured_decision(
        {"route": "tool_call", "tool_name": "delete_database", "tool_args": {}},
        allowed_tools={"calculator"},
    )

    assert decision.route == "answer"
    assert decision.reason == "unknown_tool_requested:delete_database"
    assert "未获授权" in decision.answer


def test_configured_runner_stops_after_repeated_unusable_tool_results() -> None:
    client = FakeClient([{"route": "tool_call", "tool_name": "calculator"}])
    decision = configured_planner_think(
        {
            "request": type("Req", (), {"query": "查一下距离"})(),
            "context": {"runtime_context": runtime_context_fixture()},
            "tool_catalog": [{"name": "calculator", "description": "执行算术"}],
            "loop_count": 2,
            "tool_observations": [
                {
                    "tool_name": "mcp.gaode.maps_geo",
                    "status": "ok",
                    "output": {"mcp_tool": "maps_geo", "elapsed_ms": 100, "result": None},
                },
                {
                    "tool_name": "mcp.gaode.maps_geo",
                    "status": "error",
                    "error": "upstream timeout",
                },
            ],
        },
        client,
        load_settings(env_path=None),
    )

    assert decision.route == "answer"
    assert decision.reason == "tool_unusable_replanning_stopped"
    assert client.messages == []


def test_configured_runner_allows_first_tool_error_replanning() -> None:
    decision = stop_replanning_after_unusable_tools(
        {
            "loop_count": 1,
            "tool_observations": [
                {
                    "tool_name": "mcp.gaode.maps_geo",
                    "status": "error",
                    "error": "missing required arg",
                },
                {
                    "tool_name": "mcp.gaode.maps_geo",
                    "status": "error",
                    "error": "missing required arg",
                },
            ],
        }
    )

    assert decision is None


def test_run_configured_turn_executes_only_injected_local_tool(monkeypatch) -> None:
    client = FakeClient(
        [
            {
                "route": "tool_call",
                "tool_name": "calculator",
                "tool_args": {"expression": "1+2*3"},
                "reason": "need_calc",
            },
            {"route": "answer", "answer": "结果是 7", "reason": "done"},
        ]
    )

    from aegora_runtime.registry import registry

    @registry.tool(tool_id="calculator", runner_tool_id="local.calculator")
    def calculator(args, state):
        return {"result": 7, "expression": args["expression"]}

    monkeypatch.setattr("aegora_runtime.configured_runner.DeepSeekClient", lambda _settings: client)
    result = run_configured_turn(
        runtime_context_fixture(),
        message="1+2*3",
        session_id="s1",
        history=[],
        settings=load_settings(env_path=None),
    )

    assert result["status"] == "completed"
    assert result["route"] == "answer"
    assert result["answer"] == "结果是 7"
    assert result["tool_observations"][0]["tool_name"] == "calculator"


def test_configured_turn_returns_pending_approval_without_executing_tool(monkeypatch) -> None:
    client = FakeClient(
        [
            {
                "route": "tool_call",
                "tool_name": "approval.echo",
                "tool_args": {"text": "charge"},
                "reason": "needs_write",
            }
        ]
    )
    executed = {"count": 0}

    from aegora_runtime.registry import registry

    if not registry.has_tool("local.approval_echo"):
        @registry.tool(tool_id="approval.echo", runner_tool_id="local.approval_echo")
        def approval_echo(args, state):
            executed["count"] += 1
            return {"echo": args["text"]}

    monkeypatch.setattr("aegora_runtime.configured_runner.DeepSeekClient", lambda _settings: client)
    store = InMemoryApprovalStore()

    result = run_configured_turn(
        approval_runtime_context_fixture(),
        message="执行写操作",
        session_id="s1",
        history=[],
        settings=load_settings(env_path=None),
        metadata={"gateway_run_id": "run_approval_1"},
        approval_store=store,
    )

    assert executed["count"] == 0
    assert result["status"] == "pending_approval"
    assert result["route"] == "approval_required"
    assert result["approval_requests"][0]["tool_id"] == "approval.echo"
    assert result["tool_observations"][0]["status"] == "pending_approval"


def test_approved_request_executes_saved_call_and_feeds_observation(monkeypatch) -> None:
    client = FakeClient(
        [
            {
                "route": "tool_call",
                "tool_name": "approval.echo_resume",
                "tool_args": {"text": "charge"},
                "reason": "needs_write",
            },
            {"route": "answer", "answer": "审批后已执行。", "reason": "tool_done"},
        ]
    )
    executed = {"count": 0}

    from aegora_runtime.registry import registry

    if not registry.has_tool("local.approval_echo_resume"):
        @registry.tool(tool_id="approval.echo_resume", runner_tool_id="local.approval_echo_resume")
        def approval_echo_resume(args, state):
            executed["count"] += 1
            return {"echo": args["text"]}

    monkeypatch.setattr("aegora_runtime.configured_runner.DeepSeekClient", lambda _settings: client)
    monkeypatch.setattr(
        "aegora_runtime.configured_runner.runtime_context_resolver.resolve_runtime_context",
        lambda agent_id, release_id, actor_id, channel: approval_runtime_context_fixture(
            tool_id="approval.echo_resume",
            runner_tool_id="local.approval_echo_resume",
        ),
    )
    store = InMemoryApprovalStore()

    pending = run_configured_turn(
        approval_runtime_context_fixture(
            tool_id="approval.echo_resume",
            runner_tool_id="local.approval_echo_resume",
        ),
        message="执行写操作",
        session_id="s1",
        history=[],
        settings=load_settings(env_path=None),
        metadata={"gateway_run_id": "run_approval_2"},
        approval_store=store,
    )
    resumed = decide_approval(
        pending["approval_requests"][0]["approval_id"],
        ApprovalDecision(decision="approved", decided_by="u_reviewer", comment="允许本次执行"),
        approval_store=store,
        settings=load_settings(env_path=None),
    )

    assert executed["count"] == 1
    assert resumed["status"] == "completed"
    assert resumed["answer"] == "审批后已执行。"
    assert any(item["status"] == "approval_approved" for item in resumed["tool_observations"])
    assert any(item.get("output") == {"echo": "charge"} for item in resumed["tool_observations"])


def test_approved_but_unexecuted_request_can_resume_idempotently(monkeypatch) -> None:
    client = FakeClient(
        [
            {
                "route": "tool_call",
                "tool_name": "approval.echo_idempotent",
                "tool_args": {"text": "charge"},
                "reason": "needs_write",
            },
            {"route": "answer", "answer": "续跑完成。", "reason": "tool_done"},
        ]
    )
    executed = {"count": 0}

    from aegora_runtime.registry import registry

    if not registry.has_tool("local.approval_echo_idempotent"):
        @registry.tool(
            tool_id="approval.echo_idempotent",
            runner_tool_id="local.approval_echo_idempotent",
        )
        def approval_echo_idempotent(args, state):
            executed["count"] += 1
            return {"echo": args["text"]}

    monkeypatch.setattr("aegora_runtime.configured_runner.DeepSeekClient", lambda _settings: client)
    monkeypatch.setattr(
        "aegora_runtime.configured_runner.runtime_context_resolver.resolve_runtime_context",
        lambda agent_id, release_id, actor_id, channel: approval_runtime_context_fixture(
            tool_id="approval.echo_idempotent",
            runner_tool_id="local.approval_echo_idempotent",
        ),
    )
    store = InMemoryApprovalStore()

    pending = run_configured_turn(
        approval_runtime_context_fixture(
            tool_id="approval.echo_idempotent",
            runner_tool_id="local.approval_echo_idempotent",
        ),
        message="执行写操作",
        session_id="s1",
        history=[],
        settings=load_settings(env_path=None),
        metadata={"gateway_run_id": "run_approval_retry"},
        approval_store=store,
    )
    approval_id = pending["approval_requests"][0]["approval_id"]
    store.decide(
        approval_id,
        ApprovalDecision(decision="approved", decided_by="u_reviewer", comment="第一次已置 approved"),
    )

    resumed = decide_approval(
        approval_id,
        ApprovalDecision(decision="approved", decided_by="u_reviewer", comment="重试"),
        approval_store=store,
        settings=load_settings(env_path=None),
    )

    assert executed["count"] == 1
    assert resumed["status"] == "completed"
    assert resumed["approval"]["status"] == "executed"


def test_approval_store_sanitizes_non_json_result() -> None:
    store = InMemoryApprovalStore()
    approval = store.create_pending(
        snapshot={},
        approval={
            "run_id": "run_1",
            "session_id": "s1",
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "web_console",
            "tool_id": "approval.echo",
        },
    )

    store.mark_executed(approval["approval_id"], {"request": type("AgentRequest", (), {})()})
    record = store.get(approval["approval_id"])

    json.dumps(record["result"], ensure_ascii=False)
    assert isinstance(record["result"]["request"], str)
