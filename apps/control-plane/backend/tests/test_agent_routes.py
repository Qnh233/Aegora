from fastapi.testclient import TestClient
from types import SimpleNamespace

from app import api, identity, runner
from app.models import (
    AgentRunResponse,
    AgentSummary,
    MCPConnectionDefinition,
    RoleSummary,
    RunSummary,
    ToolDefinition,
)


def login_headers(monkeypatch, user_id="u_1"):
    monkeypatch.setattr(api.db, "get_session_user", lambda _: user_id)
    return {"Authorization": "Bearer test-token"}


def test_login_returns_token_for_active_user(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_status", lambda _: "active")
    monkeypatch.setattr(api.db, "create_user_session", lambda token, user_id: None)
    monkeypatch.setattr(api.db, "get_user_roles", lambda _: {"office_tools"})
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate"]}},
    )
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)

    response = TestClient(api.app).post("/auth/login", json={"user_id": "u_1"})

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]
    assert response.json()["user"]["user_id"] == "u_1"
    assert response.json()["user"]["roles"] == ["office_tools"]


def test_login_rejects_inactive_or_missing_user(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: False)

    response = TestClient(api.app).post("/auth/login", json={"user_id": "u_1"})

    assert response.status_code == 403


def test_me_requires_bearer_token(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)

    response = TestClient(api.app).get("/auth/me")

    assert response.status_code == 401


def test_me_returns_current_user_profile(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda token: "u_1" if token == "token" else None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_status", lambda _: "active")
    monkeypatch.setattr(api.db, "get_user_roles", lambda _: {"platform_admin"})
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {})
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)

    response = TestClient(api.app).get(
        "/auth/me", headers={"Authorization": "Bearer token"}
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "u_1"
    assert response.json()["is_platform_admin"] is True


def test_me_uses_oa_identity_as_platform_user(monkeypatch):
    ensured = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.config,
        "get_config",
        lambda: SimpleNamespace(auth_mode="oa", oa_auth_me_url="http://oa/api/oa/me"),
    )
    monkeypatch.setattr(
        api.identity,
        "fetch_oa_current_user",
        lambda token: identity.ExternalIdentity(uid="oa_user_1", name="OA User"),
    )
    monkeypatch.setattr(
        api.db,
        "ensure_user_exists",
        lambda user_id, status: ensured.update({"user_id": user_id, "status": status}),
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_status", lambda _: "active")
    monkeypatch.setattr(api.db, "get_user_roles", lambda _: {"office_tools"})
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {})
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)

    response = TestClient(api.app).get(
        "/auth/me", headers={"Authorization": "Bearer oa-token"}
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "oa_user_1"
    assert response.json()["roles"] == ["office_tools"]
    assert ensured == {"user_id": "oa_user_1", "status": "active"}


def test_me_rejects_disabled_platform_user_after_oa_auth(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.config,
        "get_config",
        lambda: SimpleNamespace(auth_mode="oa", oa_auth_me_url="http://oa/api/oa/me"),
    )
    monkeypatch.setattr(
        api.identity,
        "fetch_oa_current_user",
        lambda token: identity.ExternalIdentity(uid="oa_user_1", name="OA User"),
    )
    monkeypatch.setattr(api.db, "ensure_user_exists", lambda *_: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: False)

    response = TestClient(api.app).get(
        "/auth/me", headers={"Authorization": "Bearer oa-token"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "当前用户不可用"


def test_local_login_is_disabled_in_oa_mode(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.config,
        "get_config",
        lambda: SimpleNamespace(auth_mode="oa", oa_auth_me_url="http://oa/api/oa/me"),
    )

    response = TestClient(api.app).post("/auth/login", json={"user_id": "u_1"})

    assert response.status_code == 403
    assert "OA 登录" in response.json()["detail"]


def test_gateway_approval_decision_is_proxied_with_current_actor(monkeypatch):
    captured = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "update_run", lambda *args, **kwargs: captured.update({"update": args}))

    def decide(approval_id, payload):
        captured["approval_id"] = approval_id
        captured["payload"] = payload
        return AgentRunResponse(
            run_id="run_1",
            answer="已继续执行",
            tool_calls=[],
            status="succeeded",
        )

    monkeypatch.setattr(api.runner, "decide_gateway_approval", decide)

    response = TestClient(api.app).post(
        "/gateway/approvals/approval_1/decide",
        json={"actor_id": "u_1", "decision": "approved"},
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "已继续执行"
    assert captured["approval_id"] == "approval_1"
    assert captured["payload"]["decision"] == "approved"
    assert captured["payload"]["actor_id"] == "u_1"
    assert captured["payload"]["decided_by"] == "u_1"
    assert captured["update"] == ("run_1", "succeeded")


def test_gateway_approval_conflict_is_forwarded(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)

    def decide(approval_id, payload):
        raise runner.GatewayRunnerError(409, "审批请求已处理")

    monkeypatch.setattr(api.runner, "decide_gateway_approval", decide)

    response = TestClient(api.app).post(
        "/gateway/approvals/approval_1/decide",
        json={"actor_id": "u_1", "decision": "approved"},
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "审批请求已处理"


def test_admin_can_register_mcp_connection(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(
        api.db,
        "upsert_mcp_connection",
        lambda connection_id, request: saved.update(
            {"connection_id": connection_id, "request": request}
        )
        or MCPConnectionDefinition(
            connection_id=connection_id,
            name=request.name,
            status=request.status,
            transport=request.transport,
            config=request.runtime_config(),
            config_version=1,
            config_hash="sha256:test",
        ),
    )

    response = TestClient(api.app).put(
        "/admin/mcp-connections/search",
        json={
            "name": "公共搜索",
            "transport": "streamable_http",
            "url": "https://search.example.com/mcp",
            "bearer_env": "SEARCH_MCP_TOKEN",
            "idle_ttl_seconds": 900,
            "discovery_ttl_seconds": 300,
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 200
    assert response.json()["connection_id"] == "search"
    assert response.json()["config"]["url"] == "https://search.example.com/mcp"
    assert saved["request"].bearer_env == "SEARCH_MCP_TOKEN"


def test_admin_can_register_mcp_connection_with_headers(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(
        api.db,
        "upsert_mcp_connection",
        lambda connection_id, request: saved.update(
            {"connection_id": connection_id, "request": request}
        )
        or MCPConnectionDefinition(
            connection_id=connection_id,
            name=request.name,
            status=request.status,
            transport=request.transport,
            config=request.runtime_config(),
            config_version=1,
            config_hash="sha256:test",
        ),
    )

    response = TestClient(api.app).put(
        "/admin/mcp-connections/search",
        json={
            "name": "公共搜索",
            "transport": "streamable_http",
            "url": "https://search.example.com/mcp",
            "headers": {
                "Authorization": "Bearer user-token",
                "x-api-key": "key-1",
            },
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 200
    assert response.json()["config"]["headers"]["Authorization"] == "Bearer user-token"
    assert saved["request"].headers["x-api-key"] == "key-1"


def test_mcp_connection_rejects_invalid_header_value(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)

    response = TestClient(api.app).put(
        "/admin/mcp-connections/search",
        json={
            "name": "公共搜索",
            "transport": "streamable_http",
            "url": "https://search.example.com/mcp",
            "headers": {"x-api-key": "bad\nvalue"},
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 422


def test_mcp_connection_rejects_invalid_transport_config(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)

    response = TestClient(api.app).put(
        "/admin/mcp-connections/broken",
        json={"name": "broken", "transport": "stdio", "url": "https://example.com/mcp"},
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 422


def test_admin_discovers_and_persists_mcp_tools(monkeypatch):
    connection = MCPConnectionDefinition(
        connection_id="search",
        name="公共搜索",
        status="active",
        transport="streamable_http",
        config={"url": "https://search.example.com/mcp"},
        config_version=1,
        config_hash="sha256:v1",
    )
    tool = ToolDefinition(
        tool_id="mcp.search.web_search",
        name="网页搜索",
        description="搜索公开网页",
        source="mcp",
        runner_name="web_search",
        mcp_connection_id="search",
    )
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "fetch_mcp_connection", lambda _: connection)

    async def discover(_connection):
        return [tool]

    monkeypatch.setattr(api.mcp_discovery, "discover_tools", discover)
    monkeypatch.setattr(
        api.db,
        "replace_discovered_mcp_tools",
        lambda connection_id, tools: saved.update(
            {"connection_id": connection_id, "tools": tools}
        )
        or ["mcp.search.removed"],
    )

    response = TestClient(api.app).post(
        "/admin/mcp-connections/search/discover",
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 200
    assert response.json()["tool_ids"] == ["mcp.search.web_search"]
    assert response.json()["disabled_tool_ids"] == ["mcp.search.removed"]
    assert saved["connection_id"] == "search"


def test_mcp_discovery_error_unwraps_exception_group(monkeypatch):
    connection = MCPConnectionDefinition(
        connection_id="search",
        name="公共搜索",
        status="active",
        transport="streamable_http",
        config={"url": "https://search.example.com/mcp"},
        config_version=1,
        config_hash="sha256:v1",
    )
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "fetch_mcp_connection", lambda _: connection)

    async def discover(_connection):
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [RuntimeError("HTTP 401 Unauthorized")],
        )

    monkeypatch.setattr(api.mcp_discovery, "discover_tools", discover)

    response = TestClient(api.app).post(
        "/admin/mcp-connections/search/discover",
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "MCP 工具发现失败: RuntimeError: HTTP 401 Unauthorized"


def test_tool_can_reference_mcp_connection(monkeypatch):
    saved = {}
    connection = MCPConnectionDefinition(
        connection_id="search",
        name="公共搜索",
        status="active",
        transport="streamable_http",
        config={"url": "https://search.example.com/mcp"},
        config_version=2,
        config_hash="sha256:v2",
    )
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "fetch_mcp_connection", lambda _: connection)
    monkeypatch.setattr(api.db, "upsert_tool", lambda tool: saved.update({"tool": tool}))

    response = TestClient(api.app).put(
        "/tools/runtime.retrieve.web_search",
        json={
            "name": "网页搜索",
            "source": "mcp",
            "mcp_connection_id": "search",
            "runner_name": "web_search",
        },
    )

    assert response.status_code == 200
    assert response.json()["mcp_connection_id"] == "search"
    assert response.json()["mcp_connection"]["config_hash"] == "sha256:v2"
    assert saved["tool"].runner_tool_id == connection.runner_tool_id


def test_create_agent_rejects_owner_without_role_tool_scope(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {})
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).post(
        "/agents",
        json={
            "owner_user_id": "u_1",
            "name": "finance",
            "tools": ["kb_read", "sql_readonly"],
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "创建者缺少角色工具权限"


def test_create_agent_accepts_scoped_tool_inside_role_scope(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate", "read"]}},
    )
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)
    monkeypatch.setattr(
        api.db,
        "save_agent",
        lambda agent_id, request, permissions, tool_scopes: saved.update(
            {
                "permissions": permissions,
                "tool_scopes": tool_scopes,
            }
        ),
    )

    response = TestClient(api.app).post(
        "/agents",
        json={
            "owner_user_id": "u_1",
            "name": "calculator-agent",
            "tools": [
                {
                    "tool_id": "calculator",
                    "scope": {"actions": ["calculate"]},
                }
            ],
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 201
    assert response.json()["tools"] == ["calculator"]
    assert saved["permissions"] == {"calculator"}
    assert saved["tool_scopes"] == {"calculator": {"actions": ["calculate"]}}


def test_create_agent_rejects_scope_outside_role_scope(monkeypatch):
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate"]}},
    )
    monkeypatch.setattr(api.db, "record_permission_event", lambda *event: events.append(event))

    response = TestClient(api.app).post(
        "/agents",
        json={
            "owner_user_id": "u_1",
            "name": "calculator-agent",
            "tools": [
                {"tool_id": "calculator", "scope": {"actions": ["delete"]}}
            ],
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 403
    assert events[-1][2] == "tool_denied"


def test_create_agent_rejects_disabled_tool(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda _: set())
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).post(
        "/agents",
        json={
            "owner_user_id": "u_1",
            "name": "calculator-agent",
            "tools": ["calculator"],
        },
        headers=login_headers(monkeypatch),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "包含未注册或已关闭工具"


def test_create_agent_rejects_impersonated_owner(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)

    response = TestClient(api.app).post(
        "/agents",
        json={"owner_user_id": "u_owner", "name": "spoofed", "tools": []},
        headers=login_headers(monkeypatch, "u_other"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "当前用户不能代替他人创建 Agent"


def test_authorize_rejects_tool_removed_from_current_role_scope(monkeypatch):
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_1",
            "enabled": True,
            "tools": {"kb_read", "sql_readonly"},
            "snapshot_tools": {"kb_read", "sql_readonly"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"kb_read": {}})
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "record_permission_event", lambda *event: events.append(event))

    response = TestClient(api.app).post(
        "/agents/agent_1/authorize",
        json={"actor_id": "u_1", "channel": "web_console", "tool_id": "sql_readonly"},
    )

    assert response.status_code == 403
    assert events[-1][2] == "tool_denied"


def test_authorize_filters_disabled_tool_from_effective_tools(monkeypatch):
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_1",
            "enabled": True,
            "tools": {"calculator"},
            "snapshot_tools": {"calculator"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"calculator": {}})
    monkeypatch.setattr(api.db, "active_tool_ids", lambda _: set())
    monkeypatch.setattr(api.db, "record_permission_event", lambda *event: events.append(event))

    response = TestClient(api.app).post(
        "/agents/agent_1/authorize",
        json={"actor_id": "u_1", "channel": "web_console", "tool_id": "calculator"},
    )

    assert response.status_code == 403
    assert events[-1][2] == "tool_denied"


def test_authorize_does_not_grant_new_role_tool_without_agent_upgrade(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_1",
            "enabled": True,
            "tools": {"kb_read"},
            "snapshot_tools": {"kb_read"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(
        api.db, "get_user_role_tool_scopes", lambda _: {"kb_read": {}, "sql_readonly": {}}
    )
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).post(
        "/agents/agent_1/authorize",
        json={"actor_id": "u_1", "channel": "web_console", "tool_id": "sql_readonly"},
    )

    assert response.status_code == 403


def test_authorize_allows_private_agent_member_with_role_scope(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_owner",
            "visibility": "private",
            "members": {"u_member"},
            "enabled": True,
            "tools": {"calculator"},
            "snapshot_tools": {"calculator"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"calculator": {}})
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).post(
        "/agents/agent_1/authorize",
        json={"actor_id": "u_member", "channel": "web_console", "tool_id": "calculator"},
    )

    assert response.status_code == 200
    assert response.json()["effective_tools"] == ["calculator"]


def test_authorize_rejects_private_agent_non_member(monkeypatch):
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_owner",
            "visibility": "private",
            "members": set(),
            "enabled": True,
            "tools": {"calculator"},
            "snapshot_tools": {"calculator"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(api.db, "record_permission_event", lambda *event: events.append(event))

    response = TestClient(api.app).post(
        "/agents/agent_1/authorize",
        json={"actor_id": "u_other", "channel": "web_console", "tool_id": "calculator"},
    )

    assert response.status_code == 403
    assert events[-1][3] == "actor cannot run agent"


def test_authorize_public_agent_still_intersects_actor_role_scope(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_owner",
            "visibility": "public",
            "members": set(),
            "enabled": True,
            "tools": {"calculator", "time_now"},
            "snapshot_tools": {"calculator", "time_now"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"time_now": {}})
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).post(
        "/agents/agent_1/authorize",
        json={"actor_id": "u_other", "channel": "web_console", "tool_id": "calculator"},
    )

    assert response.status_code == 403


def test_list_agents_returns_summaries(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "fetch_agents",
        lambda **_: [
            AgentSummary(
                agent_id="agent_1",
                name="calculator-agent",
                icon="calculator",
                owner_user_id="u_1",
                visibility="public",
                members=[],
                enabled=True,
                tools=["calculator"],
            )
        ],
    )

    response = TestClient(api.app).get("/agents")

    assert response.status_code == 200
    assert response.json()[0]["tools"] == ["calculator"]
    assert response.json()[0]["visibility"] == "public"


def test_admin_role_apis_reject_non_admin_session(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_member")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)

    client = TestClient(api.app)
    roles_response = client.get(
        "/admin/roles", headers={"Authorization": "Bearer member-token"}
    )
    assign_response = client.put(
        "/admin/users/u_2/roles",
        json={"actor_id": "u_member", "roles": ["office_tools"]},
        headers={"Authorization": "Bearer member-token"},
    )

    assert roles_response.status_code == 403
    assert assign_response.status_code == 403


def test_admin_role_apis_return_users_and_save_roles_for_admin_session(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "roles_exist", lambda roles: roles == {"office_tools"})
    monkeypatch.setattr(api.db, "upsert_user", lambda *_: None)
    monkeypatch.setattr(
        api.db,
        "fetch_roles",
        lambda: [
            RoleSummary(
                role_id="office_tools",
                name="办公工具角色",
                status="active",
                permissions=[],
            )
        ],
    )
    monkeypatch.setattr(
        api.db,
        "fetch_users_with_roles",
        lambda: [
            {"user_id": "u_member", "status": "active", "roles": ["office_tools"]}
        ],
    )
    monkeypatch.setattr(
        api.db,
        "replace_user_roles",
        lambda user_id, roles: saved.update({"user_id": user_id, "roles": roles}),
    )

    client = TestClient(api.app)
    headers = {"Authorization": "Bearer admin-token"}
    roles_response = client.get("/admin/roles", headers=headers)
    users_response = client.get("/admin/users", headers=headers)
    response = client.put(
        "/admin/users/u_2/roles",
        json={"actor_id": "u_admin", "roles": ["office_tools"]},
        headers=headers,
    )

    assert roles_response.status_code == 200
    assert roles_response.json()[0]["role_id"] == "office_tools"
    assert users_response.status_code == 200
    assert users_response.json()[0]["user_id"] == "u_member"
    assert response.status_code == 200
    assert response.json() == {"user_id": "u_2", "roles": ["office_tools"]}
    assert saved == {"user_id": "u_2", "roles": {"office_tools"}}


def test_admin_assign_roles_rejects_actor_id_spoofing(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)

    response = TestClient(api.app).put(
        "/admin/users/u_2/roles",
        json={"actor_id": "u_other", "roles": ["office_tools"]},
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 403


def test_admin_updates_tool_status(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(
        api.db,
        "update_tool_status",
        lambda tool_id, status: saved.update({"tool_id": tool_id, "status": status}) or True,
    )
    monkeypatch.setattr(
        api.db,
        "fetch_tools",
        lambda: [
            ToolDefinition(
                tool_id="calculator",
                name="Calculator",
                description="",
                status="disabled",
                scope_schema={"actions": ["calculate"]},
            )
        ],
    )

    response = TestClient(api.app).put(
        "/admin/tools/calculator/status",
        json={"actor_id": "u_admin", "status": "disabled"},
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert saved == {"tool_id": "calculator", "status": "disabled"}


def test_admin_creates_custom_role_with_scoped_tools(monkeypatch):
    saved = {}
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "role_exists", lambda _: False)
    monkeypatch.setattr(api.db, "tool_ids_exist", lambda tool_ids: tool_ids == {"calculator"})
    monkeypatch.setattr(
        api.db,
        "fetch_tool_scope_schemas",
        lambda tool_ids: {"calculator": {"actions": ["calculate"]}},
    )
    monkeypatch.setattr(
        api.db,
        "create_custom_role",
        lambda role_id, name, description, status: saved.update(
            {
                "role_id": role_id,
                "name": name,
                "description": description,
                "status": status,
            }
        ),
    )
    monkeypatch.setattr(
        api.db,
        "replace_role_tool_permissions",
        lambda role_id, permissions: saved.update({"permissions": permissions}),
    )
    monkeypatch.setattr(
        api.db,
        "record_role_change_event",
        lambda *event: events.append(event),
    )

    response = TestClient(api.app).post(
        "/admin/roles",
        json={
            "actor_id": "u_admin",
            "role_id": "finance_readonly",
            "name": "财务只读",
            "description": "用于报表查询",
            "permissions": [
                {"tool_id": "calculator", "scope": {"actions": ["calculate"]}}
            ],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 201
    assert response.json()["role_id"] == "finance_readonly"
    assert saved["permissions"] == {"calculator": {"actions": ["calculate"]}}
    assert events[-1][2] == "role_created"


def test_admin_creates_custom_role_with_tool_level_permission(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "role_exists", lambda _: False)
    monkeypatch.setattr(api.db, "tool_ids_exist", lambda tool_ids: tool_ids == {"mcp.search.web_search"})
    monkeypatch.setattr(
        api.db,
        "fetch_tool_scope_schemas",
        lambda tool_ids: {"mcp.search.web_search": {}},
    )
    monkeypatch.setattr(
        api.db,
        "create_custom_role",
        lambda role_id, name, description, status: None,
    )
    monkeypatch.setattr(
        api.db,
        "replace_role_tool_permissions",
        lambda role_id, permissions: saved.update({"permissions": permissions}),
    )
    monkeypatch.setattr(api.db, "record_role_change_event", lambda *_: None)

    response = TestClient(api.app).post(
        "/admin/roles",
        json={
            "actor_id": "u_admin",
            "role_id": "mcp_reader",
            "name": "MCP 检索",
            "description": "允许调用 MCP 检索工具",
            "permissions": [{"tool_id": "mcp.search.web_search", "scope": None}],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 201
    assert saved["permissions"] == {"mcp.search.web_search": {}}


def test_admin_creates_custom_role_with_chinese_role_id(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "role_exists", lambda _: False)
    monkeypatch.setattr(api.db, "tool_ids_exist", lambda tool_ids: tool_ids == {"mcp.gaode.maps_around_search"})
    monkeypatch.setattr(
        api.db,
        "fetch_tool_scope_schemas",
        lambda tool_ids: {"mcp.gaode.maps_around_search": {}},
    )
    monkeypatch.setattr(
        api.db,
        "create_custom_role",
        lambda role_id, name, description, status: saved.update({"role_id": role_id}),
    )
    monkeypatch.setattr(
        api.db,
        "replace_role_tool_permissions",
        lambda role_id, permissions: saved.update({"permissions": permissions}),
    )
    monkeypatch.setattr(api.db, "record_role_change_event", lambda *_: None)

    response = TestClient(api.app).post(
        "/admin/roles",
        json={
            "actor_id": "u_admin",
            "role_id": "高德mcp",
            "name": "高德mcp",
            "permissions": [{"tool_id": "mcp.gaode.maps_around_search", "scope": {}}],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 201
    assert saved["role_id"] == "高德mcp"
    assert saved["permissions"] == {"mcp.gaode.maps_around_search": {}}


def test_admin_rejects_custom_role_with_invalid_tool_scope(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "role_exists", lambda _: False)
    monkeypatch.setattr(api.db, "tool_ids_exist", lambda _: True)
    monkeypatch.setattr(
        api.db,
        "fetch_tool_scope_schemas",
        lambda tool_ids: {"calculator": {"actions": ["calculate"]}},
    )

    response = TestClient(api.app).post(
        "/admin/roles",
        json={
            "actor_id": "u_admin",
            "role_id": "bad_role",
            "name": "错误角色",
            "permissions": [
                {"tool_id": "calculator", "scope": {"actions": ["delete"]}}
            ],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 422
    assert "scope" in response.json()["detail"]


def test_admin_cannot_edit_system_role(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "role_is_system", lambda _: True)

    response = TestClient(api.app).put(
        "/admin/roles/office_tools",
        json={
            "actor_id": "u_admin",
            "role_id": "office_tools",
            "name": "办公工具角色",
            "permissions": [],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 403


def test_admin_updates_custom_role(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "role_is_system", lambda _: False)
    monkeypatch.setattr(api.db, "tool_ids_exist", lambda tool_ids: tool_ids == {"time_now"})
    monkeypatch.setattr(
        api.db,
        "fetch_tool_scope_schemas",
        lambda tool_ids: {"time_now": {"actions": ["read"]}},
    )
    monkeypatch.setattr(
        api.db,
        "update_custom_role",
        lambda role_id, name, description, status: saved.update(
            {"role_id": role_id, "name": name, "description": description, "status": status}
        )
        or True,
    )
    monkeypatch.setattr(
        api.db,
        "replace_role_tool_permissions",
        lambda role_id, permissions: saved.update({"permissions": permissions}),
    )
    monkeypatch.setattr(api.db, "record_role_change_event", lambda *_: None)

    response = TestClient(api.app).put(
        "/admin/roles/after_hours",
        json={
            "actor_id": "u_admin",
            "role_id": "after_hours",
            "name": "夜班查询",
            "description": "仅保留时间查询权限",
            "status": "disabled",
            "permissions": [{"tool_id": "time_now", "scope": {"actions": ["read"]}}],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert saved["permissions"] == {"time_now": {"actions": ["read"]}}


def test_publish_agent_writes_immutable_release_snapshot(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_1",
            "enabled": True,
            "name": "calculator-agent",
            "icon": "calculator",
            "system_prompt": "执行计算",
            "model": "deepseek-v4-pro",
            "tools": {"calculator"},
            "tool_scopes": {"calculator": {"actions": ["calculate"]}},
            "snapshot_tools": {"calculator"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "next_release_version", lambda _: 3)
    monkeypatch.setattr(
        api.db, "get_session_user", lambda token: "u_1" if token == "owner-token" else None
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(api.db, "get_user_roles", lambda _: {"office_tools"})
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate"]}},
    )
    monkeypatch.setattr(
        api.db,
        "fetch_tools_by_ids",
        lambda tool_ids: {
            "calculator": ToolDefinition(
                tool_id="calculator",
                name="Calculator",
                description="执行安全的基础算术表达式。",
                runner_tool_id="local.calculator",
                version="local-v1",
                read_only=True,
                idempotent=True,
                parallel_safe=True,
                requires_approval=False,
                side_effect_level="none",
                data_sensitivity="internal",
                network_access="none",
                layer="runtime",
                category="utility",
                namespace="platform",
                group_id="platform.local",
                group_name="平台本地工具",
                input_schema={"type": "object"},
                scope_schema={"actions": ["calculate"]},
                scope_descriptions={"actions": "允许的计算动作。"},
                manifest_hash="sha256:calculator",
            )
        },
    )
    monkeypatch.setattr(
        api.db,
        "save_agent_release",
        lambda release_id, agent_id, version, config_json, published_by: saved.update(
            {
                "agent_id": agent_id,
                "version": version,
                "config_json": config_json,
                "published_by": published_by,
            }
        ),
    )

    response = TestClient(api.app).post(
        "/agents/agent_1/releases",
        json={"actor_id": "u_1"},
        headers={"Authorization": "Bearer owner-token"},
    )

    assert response.status_code == 201
    assert response.json()["version"] == 3
    assert saved["config_json"]["agent"]["icon"] == "calculator"
    assert saved["config_json"]["tools"] == [
        {
            "tool_id": "calculator",
                "runner_tool_id": "local.calculator",
                "runner_name": None,
                "mcp_connection_id": None,
                "mcp_connection": None,
                "name": "Calculator",
            "description": "执行安全的基础算术表达式。",
            "source": "local",
            "version": "local-v1",
            "read_only": True,
            "idempotent": True,
            "parallel_safe": True,
            "requires_approval": False,
            "side_effect_level": "none",
            "data_sensitivity": "internal",
            "network_access": "none",
            "layer": "runtime",
            "category": "utility",
            "namespace": "platform",
            "group_id": "platform.local",
            "group_name": "平台本地工具",
            "timeout_ms": 8000,
            "input_schema": {"type": "object"},
            "scope_schema": {"actions": ["calculate"]},
            "scope_descriptions": {"actions": "允许的计算动作。"},
            "scope": {"actions": ["calculate"]},
            "manifest_hash": "sha256:calculator",
        }
    ]
    assert saved["config_json"]["permission_snapshot"]["published_by_roles"] == [
        "office_tools"
    ]


def test_public_agent_user_cannot_spoof_owner_to_publish(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_member")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)

    response = TestClient(api.app).post(
        "/agents/agent_1/releases",
        json={"actor_id": "u_owner"},
        headers={"Authorization": "Bearer member-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "请求用户与登录用户不一致"


def test_owner_updates_agent_configuration(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_owner")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_owner",
            "enabled": True,
            "name": "old",
            "icon": "robot",
            "visibility": "public",
            "members": set(),
            "system_prompt": "old",
            "model": None,
            "tools": {"calculator"},
            "tool_scopes": {"calculator": {}},
            "snapshot_tools": {"calculator"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "active_tool_ids", lambda ids: ids)
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate"]}},
    )
    monkeypatch.setattr(
        api.db,
        "update_agent",
        lambda agent_id, request, permissions, scopes, actor_id: saved.update(
            {
                "agent_id": agent_id,
                "name": request.name,
                "permissions": permissions,
                "scopes": scopes,
                "actor_id": actor_id,
            }
        ),
    )
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).put(
        "/agents/agent_1",
        json={
            "actor_id": "u_owner",
            "name": "new",
            "icon": "calculator",
            "visibility": "private",
            "members": ["u_member"],
            "system_prompt": "new prompt",
            "model": "test-model",
            "tools": ["calculator"],
            "channels": ["web_console"],
        },
        headers={"Authorization": "Bearer owner-token"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "new"
    assert response.json()["members"] == ["u_member"]
    assert saved == {
        "agent_id": "agent_1",
        "name": "new",
        "permissions": {"calculator"},
        "scopes": {"calculator": {}},
        "actor_id": "u_owner",
    }


def test_non_owner_cannot_update_public_agent(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_member")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {"owner_user_id": "u_owner", "visibility": "public"},
    )

    response = TestClient(api.app).put(
        "/agents/agent_1",
        json={
            "actor_id": "u_member",
            "name": "tampered",
            "tools": [],
            "channels": ["web_console"],
        },
        headers={"Authorization": "Bearer member-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "当前用户不能管理该 Agent"


def test_platform_admin_disables_agent(monkeypatch):
    saved = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    monkeypatch.setattr(api.db, "load_agent", lambda _: {"owner_user_id": "u_owner"})
    monkeypatch.setattr(
        api.db,
        "update_agent_status",
        lambda agent_id, enabled: saved.update(
            {"agent_id": agent_id, "enabled": enabled}
        )
        or True,
    )
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).put(
        "/admin/agents/agent_1/status",
        json={"actor_id": "u_admin", "enabled": False},
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"agent_id": "agent_1", "enabled": False}
    assert saved == {"agent_id": "agent_1", "enabled": False}


def release_fixture(status="published"):
    return {
        "release_id": "release_1",
        "agent_id": "agent_1",
        "version": 1,
        "status": status,
        "published_by": "u_1",
        "published_at": "2026-06-25 10:00:00+08",
        "revoked_at": None,
        "config_json": {
            "agent": {
                "id": "agent_1",
                "name": "calculator-agent",
                "icon": "calculator",
                "owner_user_id": "u_1",
                "system_prompt": "执行计算",
                "model": "deepseek-v4-pro",
                "channels": ["web_console"],
                "enabled": True,
            },
            "tools": [
                {
                    "tool_id": "calculator",
                    "runner_tool_id": "local.calculator",
                    "input_schema": {"type": "object"},
                    "scope": {"actions": ["calculate"]},
                    "manifest_hash": "sha256:calculator",
                }
            ],
            "runtime_policy": {"release_token_enabled": False},
        },
    }


def test_list_agent_releases(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "fetch_agent_releases", lambda _: [release_fixture()])

    response = TestClient(api.app).get("/agents/agent_1/releases")

    assert response.status_code == 200
    assert response.json()[0]["release_id"] == "release_1"
    assert response.json()[0]["config_json"]["tools"][0]["tool_id"] == "calculator"


def test_run_agent_release_uses_release_config_and_active_tools(monkeypatch):
    calls = []
    captured = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "fetch_agent_release", lambda *_: release_fixture())
    monkeypatch.setattr(api.db, "agent_is_enabled", lambda _: True)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"calculator": {}})
    monkeypatch.setattr(api.db, "create_run", lambda *args: calls.append(("create", args)))
    monkeypatch.setattr(api.db, "update_run", lambda *args, **kwargs: calls.append(("update", args, kwargs)))
    monkeypatch.setattr(api.db, "record_tool_call", lambda *args: calls.append(("tool", args)))
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: calls.append(("permission", args)))
    monkeypatch.setattr(runner, "execute_tool", lambda tool_id, message: f"{tool_id}: {message}")

    def llm(messages, model=None):
        captured["messages"] = messages
        captured["model"] = model
        return "发布版本运行完成"

    monkeypatch.setattr(runner, "call_llm_messages", llm)

    response = TestClient(api.app).post(
        "/agents/agent_1/releases/release_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "1 + 2 * 3",
            "tool_ids": ["calculator"],
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "发布版本运行完成"
    assert response.json()["tool_calls"][0]["tool_id"] == "calculator"
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["messages"][0]["content"] == "执行计算"


def test_run_agent_release_rejects_disabled_tool(monkeypatch):
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "fetch_agent_release", lambda *_: release_fixture())
    monkeypatch.setattr(api.db, "agent_is_enabled", lambda _: True)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda _: set())
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"calculator": {}})
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: events.append(args))

    response = TestClient(api.app).post(
        "/agents/agent_1/releases/release_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "1 + 2 * 3",
            "tool_ids": ["calculator"],
        },
    )

    assert response.status_code == 403
    assert events[-1][2] == "tool_denied"


def test_run_agent_release_rejects_current_agent_disabled(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "fetch_agent_release", lambda *_: release_fixture())
    monkeypatch.setattr(api.db, "agent_is_enabled", lambda _: False)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "get_user_role_tool_scopes", lambda _: {"calculator": {}})
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: None)

    response = TestClient(api.app).post(
        "/agents/agent_1/releases/release_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "不应执行",
            "tool_ids": ["calculator"],
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent 已被平台停用"


def test_list_runs_returns_recent_runs(monkeypatch):
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "fetch_runs",
        lambda: [
            RunSummary(
                run_id="run_1",
                agent_id="agent_1",
                actor_id="u_1",
                message="密码忘了",
                answer="点忘记密码",
                status="succeeded",
                created_at="2026-06-23 12:00:00+08",
            )
        ],
    )

    response = TestClient(api.app).get("/runs")

    assert response.status_code == 200
    assert response.json()[0]["status"] == "succeeded"
