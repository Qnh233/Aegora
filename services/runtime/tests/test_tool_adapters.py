from __future__ import annotations

import pytest

from aegora_runtime.tool_adapters import (
    MCPClientManager,
    build_runtime_tool_executor,
    call_mcp_tool_sync,
    mcp_result_to_jsonable,
    warmup_runtime_mcp_clients,
)
from aegora_runtime.registry import RunnerRegistry


def test_local_adapter_only_exposes_injected_tools() -> None:
    local = RunnerRegistry()
    calls = []

    @local.tool(
        tool_id="calculator",
        runner_tool_id="local.calculator",
        description="calc",
    )
    def calculator(args, state):
        calls.append((args, state.get("trace_id")))
        return {"result": args["expression"]}

    context = {
        "tools": [
            {
                "tool_id": "calculator",
                "runner_tool_id": "local.calculator",
                "description": "calc",
                "input_schema": {
                    "type": "object",
                    "required": ["expression"],
                    "properties": {"expression": {"type": "string"}},
                },
                "read_only": True,
                "parallel_safe": True,
                "idempotent": True,
            }
        ],
    }

    executor = build_runtime_tool_executor(context, runner_registry=local)

    catalog = executor.catalog()
    assert [item["name"] for item in catalog] == ["calculator"]
    assert catalog[0]["input_schema"]["properties"]["expression"]["type"] == "string"
    assert catalog[0]["required_args"] == ["expression"]
    assert executor.run("calculator", {"expression": "1+1"}, {"trace_id": "t1"})["output"] == {"result": "1+1"}
    assert calls == [({"expression": "1+1"}, "t1")]


def test_local_adapter_rejects_missing_registration() -> None:
    context = {"tools": [{"tool_id": "missing", "runner_tool_id": "local.missing"}]}

    executor = build_runtime_tool_executor(context, runner_registry=RunnerRegistry())

    result = executor.run_many(
        [{"call_id": "c1", "tool_name": "missing", "tool_args": {}}],
        {},
    )
    assert result["results"][0]["status"] == "error"
    assert "local tool not registered" in result["results"][0]["error"]


def test_adapter_validates_json_schema_required_and_type() -> None:
    local = RunnerRegistry()

    @local.tool(tool_id="echo", runner_tool_id="local.echo")
    def echo(args, state):
        return args

    executor = build_runtime_tool_executor(
        {
            "tools": [
                {
                    "tool_id": "echo",
                    "runner_tool_id": "local.echo",
                    "input_schema": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ]
        },
        runner_registry=local,
    )

    result = executor.run_many(
        [{"call_id": "c1", "tool_name": "echo", "tool_args": {"text": 1}}],
        {},
    )
    assert result["results"][0]["status"] == "error"
    assert "expects string" in result["results"][0]["error"]


def test_fastmcp_adapter_calls_in_memory_server(monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp")
    mcp = fastmcp.FastMCP("unit-test")

    @mcp.tool
    async def add(a: int, b: int) -> int:
        return a + b

    monkeypatch.setattr("aegora_runtime.tool_adapters.build_mcp_transport", lambda _runner_tool_id: mcp)

    result = call_mcp_tool_sync(
        {
            "tool_id": "calculator",
            "runner_tool_id": "mcp+http://unit-test/mcp",
            "runner_name": "add",
        },
        {"a": 2, "b": 3},
        {},
    )

    assert result["mcp_tool"] == "add"
    assert result["result"] == 5


def test_mcp_result_keeps_content_when_fastmcp_data_is_none() -> None:
    class TextItem:
        text = '{"results":[{"location":"114.057939,22.543527"}]}'

    class Result:
        data = None
        structured_content = None
        content = [TextItem()]

    assert mcp_result_to_jsonable(Result()) == {
        "results": [{"location": "114.057939,22.543527"}]
    }


def test_warmup_runtime_mcp_clients_initializes_unique_mcp_tools(monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp")
    mcp = fastmcp.FastMCP("warmup-test")

    @mcp.tool
    async def ping() -> str:
        return "pong"

    monkeypatch.setattr("aegora_runtime.tool_adapters.build_mcp_transport", lambda _runner_tool_id: mcp)

    results = warmup_runtime_mcp_clients(
        {
            "tools": [
                {"tool_id": "ping_a", "runner_tool_id": "mcp+http://warmup-test/mcp", "runner_name": "ping"},
                {"tool_id": "ping_b", "runner_tool_id": "mcp+http://warmup-test/mcp", "runner_name": "ping"},
                {"tool_id": "local_only", "runner_tool_id": "local.local_only"},
            ]
        }
    )

    assert list(results) == ["ping_a"]
    assert results["ping_a"]["status"] == "ok"
    assert "ping" in results["ping_a"]["tools"]


def test_warmup_uses_structured_connection_and_caches_discovery(monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp")
    mcp = fastmcp.FastMCP("managed-connection")

    @mcp.tool
    async def ping() -> str:
        return "pong"

    monkeypatch.setattr("aegora_runtime.tool_adapters.build_mcp_transport", lambda _tool: mcp)
    manager = MCPClientManager(
        idle_ttl_seconds=60,
        discovery_ttl_seconds=30,
        cleanup_interval_seconds=60,
    )
    context = {
        "tools": [
            {
                "tool_id": "ping_a",
                "runner_name": "ping",
                "mcp_connection": {
                    "connection_id": "shared",
                    "transport": "streamable_http",
                    "config_hash": "sha256:v1",
                    "config_version": 1,
                    "config": {"url": "https://example.test/mcp"},
                },
            },
            {
                "tool_id": "ping_b",
                "runner_name": "ping",
                "mcp_connection": {
                    "connection_id": "shared",
                    "transport": "streamable_http",
                    "config_hash": "sha256:v1",
                    "config_version": 1,
                    "config": {"url": "https://example.test/mcp"},
                },
            },
        ]
    }

    try:
        first = warmup_runtime_mcp_clients(context, manager=manager)
        second = warmup_runtime_mcp_clients(context, manager=manager)
        assert list(first) == ["ping_a"]
        assert first["ping_a"]["cached"] is False
        assert second["ping_a"]["cached"] is True
        assert manager.session_count == 1
    finally:
        manager.close()


def test_mcp_session_idle_ttl_and_config_change_evict_connections(monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp")
    mcp = fastmcp.FastMCP("session-lifecycle")
    now = [100.0]

    @mcp.tool
    async def ping() -> str:
        return "pong"

    monkeypatch.setattr("aegora_runtime.tool_adapters.build_mcp_transport", lambda _tool: mcp)
    manager = MCPClientManager(
        idle_ttl_seconds=10,
        discovery_ttl_seconds=30,
        cleanup_interval_seconds=60,
        clock=lambda: now[0],
    )

    def tool(config_hash: str, version: int) -> dict:
        return {
            "tool_id": "ping",
            "runner_name": "ping",
            "mcp_connection": {
                "connection_id": "shared",
                "transport": "streamable_http",
                "config_hash": config_hash,
                "config_version": version,
                "config": {"url": "https://example.test/mcp"},
            },
        }

    try:
        manager.warmup(tool("sha256:v1", 1))
        assert manager.session_count == 1

        manager.warmup(tool("sha256:v2", 2))
        assert manager.session_count == 1

        now[0] += 11
        assert manager.cleanup_idle() == 1
        assert manager.session_count == 0
    finally:
        manager.close()


def test_runner_registry_supports_skill_registration() -> None:
    registry = RunnerRegistry()

    @registry.skill(skill_id="refund_policy", name="退款政策", tags=["support"])
    def refund_policy():
        return {"content": "先确认订单状态。"}

    skills = registry.skills()
    assert len(skills) == 1
    assert skills[0].skill_id == "refund_policy"
    assert skills[0].handler is refund_policy
