from __future__ import annotations

from contextlib import contextmanager

import pytest

from aegora_runtime import runtime_context


@pytest.fixture(autouse=True)
def no_runtime_policy_refresh(monkeypatch):
    monkeypatch.setattr(
        runtime_context,
        "fetch_active_tool_runtime_policies",
        lambda tool_ids: {},
    )


def release_fixture() -> dict[str, object]:
    return {
        "release_id": "release_1",
        "agent_id": "agent_1",
        "version": 2,
        "status": "published",
        "visibility": "private",
        "config_json": {
            "agent": {
                "id": "agent_1",
                "name": "office-agent",
                "icon": "calculator",
                "owner_user_id": "u_1",
                "system_prompt": "执行计算",
                "model": "deepseek-v4-pro",
                "enabled": True,
                "channels": ["web_console", "im"],
            },
            "tools": [
                {
                    "tool_id": "calculator",
                    "runner_tool_id": "local.calculator",
                    "name": "计算器",
                    "description": "执行算术",
                    "scope": {"actions": ["calculate"]},
                    "input_schema": {"type": "object"},
                    "read_only": True,
                    "parallel_safe": True,
                    "idempotent": True,
                },
                {
                    "tool_id": "time_now",
                    "runner_tool_id": "local.time_now",
                    "scope": {"actions": ["read"]},
                },
            ],
        },
    }


def test_build_runtime_context_filters_disabled_tools(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda _: {"calculator"})
    monkeypatch.setattr(
        runtime_context,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate"]}},
    )

    context = runtime_context.build_runtime_context(
        release_fixture(),
        actor_id="u_1",
        channel="web_console",
    )

    assert context["release"]["release_id"] == "release_1"
    assert context["agent"]["system_prompt"] == "执行计算"
    assert context["tool_ids"] == ["calculator"]
    assert context["tool_scopes"] == {"calculator": {"actions": ["calculate"]}}
    assert context["policy"]["disabled_tools_filtered"] == ["time_now"]
    assert str(context["tools"][0]["runner_tool_id"]).startswith("mcp+stdio://")
    assert context["tools"][0]["runner_name"] == "calculator"
    assert context["tools"][0]["source"] == "mcp"


def test_build_runtime_context_filters_tools_by_runtime_role_permissions(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(
        runtime_context,
        "get_user_role_tool_scopes",
        lambda _: {"time_now": {"actions": ["read"]}},
    )

    context = runtime_context.build_runtime_context(
        release_fixture(),
        actor_id="u_1",
        channel="web_console",
    )

    assert context["tool_ids"] == ["time_now"]
    assert context["policy"]["runtime_authz_filtered"] == ["calculator"]


def test_build_runtime_context_refreshes_current_mcp_connection(monkeypatch) -> None:
    release = release_fixture()
    release["config_json"]["tools"] = [
        {
            "tool_id": "web_search",
            "source": "mcp",
            "runner_tool_id": "mcp+https://old.example.com/mcp",
            "runner_name": "search",
            "mcp_connection_id": "search",
            "mcp_connection": {
                "connection_id": "search",
                "transport": "streamable_http",
                "config": {"url": "https://old.example.com/mcp"},
                "config_hash": "sha256:v1",
                "config_version": 1,
            },
            "scope": {"actions": ["search"]},
        }
    ]
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(
        runtime_context,
        "fetch_active_mcp_connections",
        lambda _: {
            "search": {
                "connection_id": "search",
                "name": "公共搜索",
                "status": "active",
                "transport": "streamable_http",
                "config": {"url": "https://new.example.com/mcp"},
                "config_hash": "sha256:v2",
                "config_version": 2,
            }
        },
    )
    monkeypatch.setattr(
        runtime_context,
        "get_user_role_tool_scopes",
        lambda _: {"web_search": {"actions": ["search"]}},
    )

    context = runtime_context.build_runtime_context(
        release,
        actor_id="u_1",
        channel="web_console",
    )

    assert context["tools"][0]["mcp_connection"]["config_hash"] == "sha256:v2"
    assert context["tools"][0]["mcp_connection"]["config"]["url"] == "https://new.example.com/mcp"
    assert context["policy"]["mcp_connection_filtered_tools"] == []


def test_build_runtime_context_refreshes_current_tool_approval_policy(monkeypatch) -> None:
    release = release_fixture()
    release["config_json"]["tools"] = [
        {
            "tool_id": "mcp.gaode.maps_geo",
            "source": "mcp",
            "runner_tool_id": "mcp+https://mcp.amap.com/mcp",
            "runner_name": "maps_geo",
            "scope": {"actions": ["geo"]},
            "read_only": True,
            "idempotent": True,
            "parallel_safe": True,
            "requires_approval": False,
            "side_effect_level": "external_read",
        }
    ]
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(
        runtime_context,
        "fetch_active_tool_runtime_policies",
        lambda _: {
            "mcp.gaode.maps_geo": {
                "read_only": True,
                "idempotent": True,
                "parallel_safe": False,
                "requires_approval": True,
                "side_effect_level": "external_read",
                "data_sensitivity": "internal",
                "network_access": "external",
                "timeout_ms": 5000,
            }
        },
    )
    monkeypatch.setattr(
        runtime_context,
        "get_user_role_tool_scopes",
        lambda _: {"mcp.gaode.maps_geo": {"actions": ["geo"]}},
    )

    context = runtime_context.build_runtime_context(
        release,
        actor_id="u_1",
        channel="web_console",
    )

    tool = context["tools"][0]
    assert tool["requires_approval"] is True
    assert tool["parallel_safe"] is False
    assert tool["timeout_ms"] == 5000
    assert context["policy"]["runtime_tool_policy_refreshed"] == ["mcp.gaode.maps_geo"]


def test_validate_runtime_context_rejects_unpublished_release(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(runtime_context, "get_user_role_tool_scopes", lambda _: {"calculator": {}, "time_now": {}})
    context = runtime_context.build_runtime_context(
        {**release_fixture(), "status": "revoked"},
        actor_id="u_1",
        channel="web_console",
    )
    monkeypatch.setattr(runtime_context, "user_is_active", lambda _: True)

    with pytest.raises(runtime_context.RuntimeContextError, match="发布版本不可运行"):
        runtime_context.validate_runtime_context(context)


def test_validate_runtime_context_rejects_inactive_actor(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(runtime_context, "get_user_role_tool_scopes", lambda _: {"calculator": {}, "time_now": {}})
    context = runtime_context.build_runtime_context(
        release_fixture(),
        actor_id="u_1",
        channel="web_console",
    )
    monkeypatch.setattr(runtime_context, "user_is_active", lambda _: False)

    with pytest.raises(runtime_context.RuntimeContextError, match="执行用户不可用"):
        runtime_context.validate_runtime_context(context)


def test_resolve_runtime_context_by_version_uses_version_lookup(monkeypatch) -> None:
    captured = {}

    def fake_fetch(agent_id, version):
        captured["fetch"] = (agent_id, version)
        return release_fixture()

    def fake_validate(context):
        captured["validate"] = context["release"]["release_id"]

    monkeypatch.setattr(runtime_context, "fetch_agent_release_by_version", fake_fetch)
    monkeypatch.setattr(runtime_context, "build_runtime_context", lambda release, actor_id, channel: {"release": {"release_id": release["release_id"]}})
    monkeypatch.setattr(runtime_context, "validate_runtime_context", fake_validate)

    context = runtime_context.resolve_runtime_context_by_version("agent_1", 2, "u_1", "web_console")

    assert captured["fetch"] == ("agent_1", 2)
    assert captured["validate"] == "release_1"
    assert context["release"]["release_id"] == "release_1"


def test_fetch_agent_release_by_version_does_not_require_created_at(monkeypatch) -> None:
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

        def fetchone(self):
            return {
                "release_id": "release_1",
                "agent_id": "agent_1",
                "version": 1,
                "status": "published",
                "visibility": "private",
                "config_json": {"agent": {}, "tools": []},
            }

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_connect_agent_platform():
        yield FakeConn()

    monkeypatch.setattr(runtime_context, "connect_agent_platform", fake_connect_agent_platform)

    release = runtime_context.fetch_agent_release_by_version("agent_1", 1)

    assert release["release_id"] == "release_1"
    assert captured["params"] == ("agent_1", 1)
    assert "published_at DESC NULLS LAST" in captured["query"]
    assert "created_at" not in captured["query"]


def test_validate_runtime_context_allows_public_release_for_active_non_owner(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(runtime_context, "get_user_role_tool_scopes", lambda _: {"calculator": {}, "time_now": {}})
    context = runtime_context.build_runtime_context(
        {**release_fixture(), "visibility": "public"},
        actor_id="u_public",
        channel="web_console",
    )
    monkeypatch.setattr(runtime_context, "user_is_active", lambda _: True)
    monkeypatch.setattr(runtime_context, "user_has_role", lambda *_: False)

    runtime_context.validate_runtime_context(context)


def test_validate_runtime_context_rejects_private_release_for_non_owner_without_admin(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(runtime_context, "get_user_role_tool_scopes", lambda _: {"calculator": {}, "time_now": {}})
    context = runtime_context.build_runtime_context(
        release_fixture(),
        actor_id="u_public",
        channel="web_console",
    )
    monkeypatch.setattr(runtime_context, "user_is_active", lambda _: True)
    monkeypatch.setattr(runtime_context, "user_has_role", lambda *_: False)

    with pytest.raises(runtime_context.RuntimeContextError, match="当前用户不能执行该发布版本"):
        runtime_context.validate_runtime_context(context)


def test_validate_runtime_context_rejects_channel_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(runtime_context, "get_user_role_tool_scopes", lambda _: {"calculator": {}, "time_now": {}})
    context = runtime_context.build_runtime_context(
        release_fixture(),
        actor_id="u_1",
        channel="email",
    )
    monkeypatch.setattr(runtime_context, "user_is_active", lambda _: True)

    with pytest.raises(runtime_context.RuntimeContextError, match="渠道未授权"):
        runtime_context.validate_runtime_context(context)


def test_validate_runtime_context_keeps_channel_limit_for_public_release(monkeypatch) -> None:
    monkeypatch.setattr(runtime_context, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(runtime_context, "get_user_role_tool_scopes", lambda _: {"calculator": {}, "time_now": {}})
    context = runtime_context.build_runtime_context(
        {**release_fixture(), "visibility": "public"},
        actor_id="u_public",
        channel="email",
    )
    monkeypatch.setattr(runtime_context, "user_is_active", lambda _: True)
    monkeypatch.setattr(runtime_context, "user_has_role", lambda *_: False)

    with pytest.raises(runtime_context.RuntimeContextError, match="渠道未授权"):
        runtime_context.validate_runtime_context(context)
