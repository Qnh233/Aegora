import logging
import secrets
import re

import psycopg
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from uuid import uuid4

from . import (
    config,
    db,
    identity,
    mcp_discovery,
    runner,
    runtime_context,
    tools as local_tools,
    workflow_capabilities,
)
from .authz import (
    effective_tools,
    merge_tool_scopes,
    validate_configured_tools,
    validate_tool_scope_subset,
)
from .models import (
    AdminUserSummary,
    AgentCreateRequest,
    AgentCreateResponse,
    AgentDetail,
    AgentReleaseRequest,
    AgentReleaseResponse,
    AgentReleaseSummary,
    AgentRunRequest,
    AgentRunResponse,
    AgentStatusRequest,
    AgentSummary,
    AgentToolConfig,
    AgentUpdateRequest,
    AuthConfigResponse,
    ApprovalDecisionRequest,
    AuthorizeRequest,
    AuthorizeResponse,
    LoginRequest,
    LoginResponse,
    MCPConnectionDefinition,
    MCPDiscoveryResponse,
    MCPConnectionRequest,
    RunRequest,
    RunResponse,
    RunSummary,
    RoleAssignRequest,
    RoleUpsertRequest,
    RoleSummary,
    SeedRolesResponse,
    ToolDefinition,
    ToolRequest,
    ToolStatusRequest,
    UserProfile,
    UserRequest,
    WorkflowCapabilityRequest,
)

app = FastAPI(title="Agent Platform")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.get_cors_origins()),
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)

PLATFORM_ADMIN_ROLE = "platform_admin"
DEFAULT_ROLE_DEFINITIONS: dict[
    str, tuple[str, str, dict[str, dict[str, list[str]]]]
] = {
    PLATFORM_ADMIN_ROLE: ("平台管理员", "管理角色、用户授权和平台配置。", {}),
    "office_tools": (
        "办公工具角色",
        "可使用全部本地低风险办公工具。",
        {
            "calculator": {"actions": ["calculate"]},
            "text_stats": {"actions": ["read"]},
            "time_now": {"actions": ["read"]},
        },
    ),
    "analyst_tools": (
        "分析工具角色",
        "可执行基础计算和文本统计。",
        {
            "calculator": {"actions": ["calculate"]},
            "text_stats": {"actions": ["read"]},
        },
    ),
    "time_tools": (
        "时间查询角色",
        "仅可读取当前服务端时间。",
        {"time_now": {"actions": ["read"]}},
    ),
}
DEFAULT_DEMO_USERS: dict[str, set[str]] = {
    "u_console": {PLATFORM_ADMIN_ROLE, "office_tools"},
    "u_analyst": {"analyst_tools"},
    "u_timekeeper": {"time_tools"},
    "u_guest": set(),
}


def llm_error_detail(error: Exception) -> str:
    detail = str(error).strip()
    if len(detail) > 500:
        detail = f"{detail[:500]}..."
    return f"LLM 调用失败: {detail}" if detail else "LLM 调用失败"


def exception_leaf_details(error: BaseException) -> list[str]:
    if isinstance(error, BaseExceptionGroup):
        details: list[str] = []
        for child in error.exceptions:
            details.extend(exception_leaf_details(child))
        return details
    detail = str(error).strip()
    return [f"{type(error).__name__}: {detail}" if detail else type(error).__name__]


def compact_exception_detail(error: BaseException, limit: int = 500) -> str:
    detail = "；".join(dict.fromkeys(exception_leaf_details(error))) or type(error).__name__
    return f"{detail[:limit]}..." if len(detail) > limit else detail


def normalize_agent_tool_request(
    request: AgentCreateRequest | AgentUpdateRequest,
) -> dict[str, dict[str, list[str]]]:
    rows = []
    for item in request.tools:
        if isinstance(item, str):
            rows.append((item, {}))
        else:
            rows.append((item.tool_id, item.scope))
    return merge_tool_scopes(rows)


def normalize_role_permissions(request: RoleUpsertRequest) -> dict[str, dict[str, list[str]]]:
    permissions = merge_tool_scopes(
        [(permission.tool_id, permission.scope) for permission in request.permissions]
    )
    scope_schemas = db.fetch_tool_scope_schemas(set(permissions))
    for tool_id, scope in permissions.items():
        local_tools.validate_tool_scope(tool_id, scope, scope_schemas.get(tool_id))
    return permissions


def require_platform_admin(actor_id: str) -> None:
    if not db.user_is_active(actor_id) or not db.user_has_role(actor_id, PLATFORM_ADMIN_ROLE):
        raise HTTPException(status_code=403, detail="需要 platform_admin 角色")


def require_current_platform_admin(authorization: str | None) -> str:
    actor_id = current_user_id(authorization)
    require_platform_admin(actor_id)
    return actor_id


def require_current_actor(authorization: str | None, claimed_actor_id: str) -> str:
    actor_id = current_user_id(authorization)
    if actor_id != claimed_actor_id:
        raise HTTPException(status_code=403, detail="请求用户与登录用户不一致")
    if not db.user_is_active(actor_id):
        raise HTTPException(status_code=403, detail="当前用户不可用")
    return actor_id


def bearer_token(authorization: str | None) -> str:
    try:
        return identity.extract_bearer_token(authorization)
    except identity.IdentityAuthError:
        raise HTTPException(status_code=401, detail="缺少登录 token")


def current_user_id(authorization: str | None) -> str:
    try:
        return identity.resolve_current_user_id(authorization)
    except identity.IdentityAuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except identity.IdentityForbiddenError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except identity.IdentityUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def optional_current_user_id(authorization: str | None) -> str | None:
    if not authorization:
        return None
    return current_user_id(authorization)


def user_profile(user_id: str) -> UserProfile:
    status = db.user_status(user_id)
    if status is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserProfile(
        user_id=user_id,
        status=status,
        roles=sorted(db.get_user_roles(user_id)),
        role_tool_scopes=db.get_user_role_tool_scopes(user_id),
        is_platform_admin=db.user_has_role(user_id, PLATFORM_ADMIN_ROLE),
    )


def can_manage_agent(actor_id: str, agent: dict[str, object]) -> bool:
    return actor_id == agent["owner_user_id"] or db.user_has_role(actor_id, PLATFORM_ADMIN_ROLE)


def require_agent_manager(actor_id: str, agent: dict[str, object]) -> None:
    if not can_manage_agent(actor_id, agent):
        raise HTTPException(status_code=403, detail="当前用户不能管理该 Agent")


def agent_detail(
    agent_id: str,
    agent: dict[str, object],
) -> AgentDetail:
    tool_scopes = agent.get("tool_scopes") or {
        tool_id: {} for tool_id in sorted(agent.get("tools") or [])
    }
    return AgentDetail(
        agent_id=agent_id,
        name=str(agent["name"]),
        icon=str(agent["icon"]),
        owner_user_id=str(agent["owner_user_id"]),
        visibility=str(agent.get("visibility") or "private"),
        members=sorted(str(member) for member in agent.get("members") or []),
        enabled=bool(agent.get("enabled", True)),
        tools=sorted(str(tool_id) for tool_id in tool_scopes),
        system_prompt=str(agent.get("system_prompt") or ""),
        model=str(agent["model"]) if agent.get("model") else None,
        tool_configs=[
            AgentToolConfig(tool_id=tool_id, scope=scope)
            for tool_id, scope in sorted(tool_scopes.items())
        ],
        channels=sorted(str(channel) for channel in agent.get("channels") or []),
    )


def can_run_agent(actor_id: str, agent: dict[str, object]) -> bool:
    if agent.get("visibility", "private") == "public":
        return True
    members = {str(member) for member in agent.get("members", set()) or set()}
    return (
        actor_id == agent["owner_user_id"]
        or actor_id in members
        or db.user_has_role(actor_id, PLATFORM_ADMIN_ROLE)
    )


def release_config_snapshot(
    agent_id: str, agent: dict[str, object], actor_id: str
) -> dict[str, object]:
    tool_scopes = agent.get("tool_scopes") or {
        tool_id: {} for tool_id in sorted(agent["tools"])
    }
    tool_manifests = db.fetch_tools_by_ids(set(tool_scopes))
    if set(tool_manifests) != set(tool_scopes):
        missing = ", ".join(sorted(set(tool_scopes) - set(tool_manifests)))
        raise HTTPException(status_code=422, detail=f"发布工具不存在或已停用: {missing}")
    return {
        "agent": {
            "id": agent_id,
            "name": agent["name"],
            "icon": agent["icon"],
            "visibility": agent.get("visibility", "private"),
            "members": sorted(agent.get("members", [])),
            "owner_user_id": agent["owner_user_id"],
            "system_prompt": agent["system_prompt"],
            "model": agent["model"],
            "channels": sorted(agent["channels"]),
            "enabled": agent["enabled"],
        },
        "tools": [
            runtime_tool_manifest(manifest, scope)
            for tool_id, scope in sorted(tool_scopes.items())
            for manifest in [tool_manifests[tool_id]]
        ],
        "runtime_policy": {"release_token_enabled": False},
        "permission_snapshot": {
            "published_by": actor_id,
            "published_by_roles": sorted(db.get_user_roles(actor_id)),
            "role_tool_scopes": db.get_user_role_tool_scopes(actor_id),
        },
    }


def resolve_agent_access(
    agent_id: str, actor_id: str, channel: str
) -> tuple[dict[str, object], set[str]]:
    agent = db.load_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if not agent["enabled"] or not db.user_is_active(actor_id):
        db.record_permission_event(agent_id, actor_id, "tool_denied", "agent or owner inactive")
        raise HTTPException(status_code=403, detail="Agent 或创建者不可用")
    if not can_run_agent(actor_id, agent):
        db.record_permission_event(agent_id, actor_id, "tool_denied", "actor cannot run agent")
        raise HTTPException(status_code=403, detail="当前用户不能执行该 Agent")
    if channel not in agent["channels"]:
        db.record_permission_event(agent_id, actor_id, "tool_denied", "channel not allowed")
        raise HTTPException(status_code=403, detail="渠道未授权")
    role_tool_ids = set(db.get_user_role_tool_scopes(actor_id))
    allowed_tools = effective_tools(set(agent["tools"]), set(agent["snapshot_tools"]), role_tool_ids)
    allowed_tools &= db.active_tool_ids(allowed_tools)
    return agent, allowed_tools


def resolve_release_access(
    agent_id: str, release_id: str, actor_id: str, channel: str
) -> tuple[dict[str, object], set[str]]:
    context = runtime_context.fetch_runtime_context(
        agent_id, release_id, actor_id=actor_id, channel=channel
    )
    if context is None:
        raise HTTPException(status_code=404, detail="发布版本不存在")
    release = context["release"]
    if not isinstance(release, dict):
        raise HTTPException(status_code=422, detail="发布版本配置不合法")
    agent = runtime_context.runner_agent_from_context(context)
    released_tools = set(context["tool_ids"])
    role_tool_ids = set(db.get_user_role_tool_scopes(actor_id))
    allowed_tools = effective_tools(released_tools, released_tools, role_tool_ids)
    if release["status"] != "published":
        raise HTTPException(status_code=409, detail="发布版本不可运行")
    if db.agent_is_enabled(agent_id) is not True:
        db.record_permission_event(agent_id, actor_id, "tool_denied", "agent disabled")
        raise HTTPException(status_code=403, detail="Agent 已被平台停用")
    if not can_run_agent(actor_id, agent):
        db.record_permission_event(agent_id, actor_id, "tool_denied", "actor cannot run release")
        raise HTTPException(status_code=403, detail="当前用户不能执行该发布版本")
    if not agent["enabled"] or not db.user_is_active(actor_id):
        db.record_permission_event(agent_id, actor_id, "tool_denied", "release agent or actor inactive")
        raise HTTPException(status_code=403, detail="Agent 或执行用户不可用")
    if channel not in agent["channels"]:
        db.record_permission_event(agent_id, actor_id, "tool_denied", "channel not allowed")
        raise HTTPException(status_code=403, detail="渠道未授权")
    return agent, allowed_tools


def selected_runner_backend(request: AgentRunRequest) -> str:
    return request.runner_backend or config.normalize_runner_backend(config.env_value("RUNNER_BACKEND"))


def draft_runtime_context(
    agent_id: str,
    agent: dict[str, object],
    allowed_tools: set[str],
    actor_id: str,
    channel: str,
) -> dict[str, object]:
    tool_scopes = agent.get("tool_scopes")
    if not isinstance(tool_scopes, dict):
        tool_scopes = {tool_id: {} for tool_id in allowed_tools}
    active_tool_scopes = {
        tool_id: tool_scopes.get(tool_id, {}) for tool_id in sorted(allowed_tools)
    }
    tool_manifests = db.fetch_tools_by_ids(set(active_tool_scopes))
    return {
        "release": None,
        "agent": {
            "id": agent_id,
            "name": agent.get("name"),
            "icon": agent.get("icon"),
            "visibility": agent.get("visibility") or "private",
            "members": sorted(agent.get("members") or []),
            "owner_user_id": agent.get("owner_user_id"),
            "system_prompt": agent.get("system_prompt") or "",
            "model": agent.get("model"),
            "enabled": bool(agent.get("enabled", True)),
            "channels": sorted(agent.get("channels") or []),
        },
        "actor": {"actor_id": actor_id},
        "channel": channel,
        "tools": [
            runtime_tool_manifest(manifest, active_tool_scopes[tool_id])
            for tool_id, manifest in sorted(tool_manifests.items())
        ],
        "tool_ids": sorted(active_tool_scopes),
        "tool_scopes": active_tool_scopes,
        "policy": {"source": "agent_draft", "disabled_tools_filtered": []},
    }


def gateway_run_payload(
    run_id: str,
    agent_id: str,
    message: str,
    request: AgentRunRequest,
    runtime_ctx: dict[str, object],
    requested_tools: list[str],
    allowed_tools: set[str],
) -> dict[str, object]:
    filtered_context = filter_runtime_context_tools(runtime_ctx, allowed_tools)
    release = filtered_context.get("release")
    if not isinstance(release, dict) or not release.get("release_id"):
        raise RuntimeError("真实 Gateway Runner 需要选择发布版本运行")
    return {
        "agent_id": agent_id,
        "release_id": str(release["release_id"]),
        "actor_id": request.actor_id,
        "channel": request.channel,
        "message": message,
        "session_id": request.session_id,
        "stream": False,
        "metadata": {
            "platform_run_id": run_id,
            "requested_tool_ids": requested_tools,
            "allowed_tool_ids": sorted(allowed_tools),
            "runtime_context": filtered_context,
        },
    }


def filter_runtime_context_tools(
    runtime_ctx: dict[str, object], allowed_tools: set[str]
) -> dict[str, object]:
    filtered = dict(runtime_ctx)
    raw_tools = runtime_ctx.get("tools") or []
    if isinstance(raw_tools, list):
        filtered["tools"] = [
            tool
            for tool in raw_tools
            if isinstance(tool, dict) and tool.get("tool_id") in allowed_tools
        ]
    raw_scopes = runtime_ctx.get("tool_scopes") or {}
    if isinstance(raw_scopes, dict):
        filtered["tool_scopes"] = {
            tool_id: scope
            for tool_id, scope in raw_scopes.items()
            if tool_id in allowed_tools
        }
    filtered["tool_ids"] = sorted(allowed_tools)
    return filtered


def execute_agent_run(
    run_id: str,
    agent_id: str,
    message: str,
    request: AgentRunRequest,
    agent: dict[str, object],
    allowed_tools: set[str],
    requested_tools: list[str],
    runtime_ctx: dict[str, object] | None = None,
) -> AgentRunResponse:
    backend = selected_runner_backend(request)
    if backend == "debug":
        answer, tool_calls = runner.run_agent_once(agent, message, allowed_tools, requested_tools)
        return AgentRunResponse(run_id=run_id, answer=answer, tool_calls=tool_calls)

    context = runtime_ctx or draft_runtime_context(
        agent_id, agent, allowed_tools, request.actor_id, request.channel
    )
    return runner.run_gateway_once(
        gateway_run_payload(
            run_id, agent_id, message, request, context, requested_tools, allowed_tools
        ),
        fallback_run_id=run_id,
    )


def persist_agent_run_result(
    run_id: str, agent_id: str, result: AgentRunResponse
) -> None:
    for trace in result.tool_calls:
        db.record_tool_call(agent_id, trace.tool_id, f"{trace.status}: {trace.result}")
    db.update_run(run_id, result.status, answer=result.answer)


@app.post("/auth/login", response_model=LoginResponse)
def login(request: LoginRequest) -> LoginResponse:
    try:
        db.ensure_schema()
        if config.get_config().auth_mode != "local":
            raise HTTPException(status_code=403, detail="当前环境使用 OA 登录")
        if not db.user_is_active(request.user_id):
            raise HTTPException(status_code=403, detail="用户不存在或不可用")
        token = secrets.token_urlsafe(32)
        db.create_user_session(token, request.user_id)
        return LoginResponse(access_token=token, user=user_profile(request.user_id))
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.get("/auth/config", response_model=AuthConfigResponse)
def auth_config() -> AuthConfigResponse:
    app_config = config.get_config()
    return AuthConfigResponse(
        auth_mode=app_config.auth_mode,
        oa_login_url=app_config.oa_login_url,
    )


@app.get("/auth/me", response_model=UserProfile)
def me(authorization: str | None = Header(default=None)) -> UserProfile:
    try:
        db.ensure_schema()
        return user_profile(current_user_id(authorization))
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post("/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    try:
        db.ensure_schema()
        token = bearer_token(authorization)
        if config.get_config().auth_mode == "local":
            db.revoke_user_session(token)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return {"status": "ok"}


@app.put("/users/{user_id}")
def set_user(user_id: str, request: UserRequest) -> dict[str, str]:
    try:
        db.ensure_schema()
        db.upsert_user(user_id, request.status)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return {"user_id": user_id, "status": request.status}


def tool_definition_from_request(tool_id: str, request: ToolRequest) -> ToolDefinition:
    connection = None
    runner_tool_id = request.runner_tool_id
    if request.mcp_connection_id:
        if request.source not in {"mcp", "workflow"}:
            raise HTTPException(status_code=422, detail="只有 MCP/Workflow 能力可以绑定 MCP 连接")
        connection = db.fetch_mcp_connection(request.mcp_connection_id)
        if connection is None:
            raise HTTPException(status_code=422, detail="MCP 连接不存在")
        runner_tool_id = connection.runner_tool_id
    return ToolDefinition(
        tool_id=tool_id,
        name=request.name,
        description=request.description,
        status=request.status,
        source=request.source,
        runner_tool_id=runner_tool_id,
        runner_name=request.runner_name,
        mcp_connection_id=request.mcp_connection_id,
        mcp_connection=connection,
        version=request.version,
        read_only=request.read_only,
        idempotent=request.idempotent,
        parallel_safe=request.parallel_safe,
        requires_approval=request.requires_approval,
        side_effect_level=request.side_effect_level,
        data_sensitivity=request.data_sensitivity,
        network_access=request.network_access,
        layer=request.layer,
        category=request.category,
        namespace=request.namespace,
        group_id=request.group_id,
        group_name=request.group_name,
        timeout_ms=request.timeout_ms,
        input_schema=request.input_schema,
        scope_schema=request.scope_schema,
        scope_descriptions=request.scope_descriptions,
        manifest_hash=request.manifest_hash or "",
    )


def workflow_definition_from_request(
    workflow_id: str,
    request: WorkflowCapabilityRequest,
) -> ToolDefinition:
    connection = db.fetch_mcp_connection(request.mcp_connection_id)
    if connection is None:
        raise HTTPException(status_code=422, detail="MCP 连接不存在")
    return workflow_capabilities.build_workflow_capability(
        workflow_id,
        request,
        connection,
    )


def runtime_tool_manifest(
    manifest: ToolDefinition,
    scope: dict[str, list[str]],
) -> dict[str, object]:
    return {
        "tool_id": manifest.tool_id,
        "runner_tool_id": manifest.runner_tool_id,
        "runner_name": manifest.runner_name,
        "mcp_connection_id": manifest.mcp_connection_id,
        "mcp_connection": (
            manifest.mcp_connection.runtime_snapshot()
            if manifest.mcp_connection
            else None
        ),
        "name": manifest.name,
        "description": manifest.description,
        "source": manifest.source,
        "version": manifest.version,
        "read_only": manifest.read_only,
        "idempotent": manifest.idempotent,
        "parallel_safe": manifest.parallel_safe,
        "requires_approval": manifest.requires_approval,
        "side_effect_level": manifest.side_effect_level,
        "data_sensitivity": manifest.data_sensitivity,
        "network_access": manifest.network_access,
        "layer": manifest.layer,
        "category": manifest.category,
        "namespace": manifest.namespace,
        "group_id": manifest.group_id,
        "group_name": manifest.group_name,
        "timeout_ms": manifest.timeout_ms,
        "input_schema": manifest.input_schema,
        "scope_schema": manifest.scope_schema,
        "scope_descriptions": manifest.scope_descriptions,
        "scope": scope,
        "manifest_hash": manifest.manifest_hash,
    }


@app.get("/admin/mcp-connections", response_model=list[MCPConnectionDefinition])
def list_mcp_connections(
    authorization: str | None = Header(default=None),
) -> list[MCPConnectionDefinition]:
    try:
        db.ensure_schema()
        require_current_platform_admin(authorization)
        return db.fetch_mcp_connections()
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.put(
    "/admin/mcp-connections/{connection_id}",
    response_model=MCPConnectionDefinition,
)
def set_mcp_connection(
    connection_id: str,
    request: MCPConnectionRequest,
    authorization: str | None = Header(default=None),
) -> MCPConnectionDefinition:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", connection_id):
        raise HTTPException(status_code=422, detail="MCP connection_id 格式不合法")
    try:
        db.ensure_schema()
        require_current_platform_admin(authorization)
        return db.upsert_mcp_connection(connection_id, request)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post(
    "/admin/mcp-connections/{connection_id}/discover",
    response_model=MCPDiscoveryResponse,
)
async def discover_mcp_connection_tools(
    connection_id: str,
    authorization: str | None = Header(default=None),
) -> MCPDiscoveryResponse:
    try:
        db.ensure_schema()
        require_current_platform_admin(authorization)
        connection = db.fetch_mcp_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="MCP 连接不存在")
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    try:
        tools = await mcp_discovery.discover_tools(connection)
    except Exception as error:
        detail = compact_exception_detail(error)
        logger.exception("MCP tool discovery failed: connection_id=%s", connection_id)
        raise HTTPException(
            status_code=502,
            detail=f"MCP 工具发现失败: {detail}",
        ) from error
    try:
        disabled_tool_ids = db.replace_discovered_mcp_tools(connection_id, tools)
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="MCP 工具写入失败") from error
    tool_ids = sorted(tool.tool_id for tool in tools)
    return MCPDiscoveryResponse(
        connection_id=connection_id,
        discovered_count=len(tool_ids),
        tool_ids=tool_ids,
        disabled_tool_ids=disabled_tool_ids,
    )


@app.get("/tools", response_model=list[ToolDefinition])
def list_tools() -> list[ToolDefinition]:
    try:
        db.ensure_schema()
        return db.fetch_tools()
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.put("/tools/{tool_id}", response_model=ToolDefinition)
def set_tool(tool_id: str, request: ToolRequest) -> ToolDefinition:
    try:
        db.ensure_schema()
        tool = tool_definition_from_request(tool_id, request)
        db.upsert_tool(tool)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return tool.model_copy(
        update={"manifest_hash": tool.manifest_hash or db.compute_tool_manifest_hash(tool)}
    )


@app.put("/admin/workflows/{workflow_id}", response_model=ToolDefinition)
def set_workflow_capability(
    workflow_id: str,
    request: WorkflowCapabilityRequest,
    authorization: str | None = Header(default=None),
) -> ToolDefinition:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", workflow_id):
        raise HTTPException(status_code=422, detail="workflow_id 格式不合法")
    try:
        db.ensure_schema()
        require_current_platform_admin(authorization)
        workflow = workflow_definition_from_request(workflow_id, request)
        db.upsert_tool(workflow)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return workflow.model_copy(
        update={
            "manifest_hash": workflow.manifest_hash
            or db.compute_tool_manifest_hash(workflow)
        }
    )


@app.put("/admin/tools/{tool_id}/status", response_model=ToolDefinition)
def set_tool_status(
    tool_id: str,
    request: ToolStatusRequest,
    authorization: str | None = Header(default=None),
) -> ToolDefinition:
    try:
        db.ensure_schema()
        actor_id = require_current_platform_admin(authorization)
        if request.actor_id != actor_id:
            raise HTTPException(status_code=403, detail="请求用户与登录用户不一致")
        if not db.update_tool_status(tool_id, request.status):
            raise HTTPException(status_code=404, detail="工具不存在")
        tools = [tool for tool in db.fetch_tools() if tool.tool_id == tool_id]
        if not tools:
            raise HTTPException(status_code=404, detail="工具不存在")
        return tools[0]
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post("/admin/seed-roles", response_model=SeedRolesResponse)
def seed_roles() -> SeedRolesResponse:
    try:
        db.ensure_schema()
        db.remove_tool_everywhere("search_faq")
        db.remove_role_everywhere("faq_reader")
        for tool in local_tools.LOCAL_TOOLS:
            db.upsert_tool(tool)
        for role_id, (name, description, permissions) in DEFAULT_ROLE_DEFINITIONS.items():
            db.upsert_role(role_id, name, description, is_system=True)
            db.replace_role_tool_permissions(role_id, permissions)
        configured_admin_users = set(config.get_config().bootstrap_admin_users)
        demo_users = {**DEFAULT_DEMO_USERS}
        for user_id in configured_admin_users:
            demo_users[user_id] = demo_users.get(user_id, set()) | {PLATFORM_ADMIN_ROLE}
        for user_id, default_roles in demo_users.items():
            db.upsert_user(user_id, "active")
            existing_roles = db.get_user_roles(user_id)
            if not existing_roles:
                db.replace_user_roles(user_id, default_roles)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return SeedRolesResponse(
        roles=sorted(DEFAULT_ROLE_DEFINITIONS),
        admin_users=sorted(configured_admin_users | {"u_console"}),
    )


@app.get("/admin/roles", response_model=list[RoleSummary])
def list_roles(authorization: str | None = Header(default=None)) -> list[RoleSummary]:
    try:
        db.ensure_schema()
        require_current_platform_admin(authorization)
        return db.fetch_roles()
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.get("/admin/users", response_model=list[AdminUserSummary])
def list_admin_users(authorization: str | None = Header(default=None)) -> list[AdminUserSummary]:
    try:
        db.ensure_schema()
        require_current_platform_admin(authorization)
        return db.fetch_users_with_roles()
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post("/admin/roles", response_model=RoleSummary, status_code=201)
def create_custom_role(
    request: RoleUpsertRequest,
    authorization: str | None = Header(default=None),
) -> RoleSummary:
    try:
        db.ensure_schema()
        actor_id = require_current_platform_admin(authorization)
        if request.actor_id != actor_id:
            raise HTTPException(status_code=403, detail="请求用户与登录用户不一致")
        if db.role_exists(request.role_id):
            raise HTTPException(status_code=409, detail="角色 ID 已存在")
        permissions = normalize_role_permissions(request)
        if not db.tool_ids_exist(set(permissions)):
            raise HTTPException(status_code=422, detail="包含未注册工具")
        db.create_custom_role(request.role_id, request.name, request.description, request.status)
        db.replace_role_tool_permissions(request.role_id, permissions)
        db.record_role_change_event(
            request.role_id,
            actor_id,
            "role_created",
            {"status": request.status, "permissions": permissions},
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return RoleSummary(
        role_id=request.role_id,
        name=request.name,
        description=request.description,
        status=request.status,
        is_system=False,
        permissions=[
            {"tool_id": tool_id, "scope": scope}
            for tool_id, scope in sorted(permissions.items())
        ],
    )


@app.put("/admin/roles/{role_id}", response_model=RoleSummary)
def update_custom_role(
    role_id: str,
    request: RoleUpsertRequest,
    authorization: str | None = Header(default=None),
) -> RoleSummary:
    try:
        db.ensure_schema()
        actor_id = require_current_platform_admin(authorization)
        if request.actor_id != actor_id:
            raise HTTPException(status_code=403, detail="请求用户与登录用户不一致")
        if request.role_id != role_id:
            raise HTTPException(status_code=422, detail="角色 ID 不允许修改")
        is_system = db.role_is_system(role_id)
        if is_system is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        if is_system:
            raise HTTPException(status_code=403, detail="系统角色不允许编辑")
        permissions = normalize_role_permissions(request)
        if not db.tool_ids_exist(set(permissions)):
            raise HTTPException(status_code=422, detail="包含未注册工具")
        if not db.update_custom_role(role_id, request.name, request.description, request.status):
            raise HTTPException(status_code=404, detail="角色不存在")
        db.replace_role_tool_permissions(role_id, permissions)
        db.record_role_change_event(
            role_id,
            actor_id,
            "role_updated",
            {"status": request.status, "permissions": permissions},
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return RoleSummary(
        role_id=role_id,
        name=request.name,
        description=request.description,
        status=request.status,
        is_system=False,
        permissions=[
            {"tool_id": tool_id, "scope": scope}
            for tool_id, scope in sorted(permissions.items())
        ],
    )


@app.put("/admin/users/{user_id}/roles")
def set_user_roles(
    user_id: str,
    request: RoleAssignRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    role_ids = set(request.roles)
    try:
        db.ensure_schema()
        actor_id = require_current_platform_admin(authorization)
        if request.actor_id != actor_id:
            raise HTTPException(status_code=403, detail="请求用户与登录用户不一致")
        if not db.roles_exist(role_ids):
            raise HTTPException(status_code=422, detail="包含未注册角色")
        if not db.user_is_active(user_id):
            db.upsert_user(user_id, "active")
        db.replace_user_roles(user_id, role_ids)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return {"user_id": user_id, "roles": sorted(role_ids)}


@app.post("/agents", response_model=AgentCreateResponse, status_code=201)
def create_agent(
    request: AgentCreateRequest,
    authorization: str | None = Header(default=None),
) -> AgentCreateResponse:
    tool_scopes = normalize_agent_tool_request(request)
    configured_tools = set(tool_scopes)
    try:
        db.ensure_schema()
        actor_id = current_user_id(authorization)
        if not db.user_is_active(actor_id):
            raise HTTPException(status_code=403, detail="当前用户不可用")
        if (
            actor_id != request.owner_user_id
            and not db.user_has_role(actor_id, PLATFORM_ADMIN_ROLE)
        ):
            raise HTTPException(status_code=403, detail="当前用户不能代替他人创建 Agent")
        if not db.user_is_active(request.owner_user_id):
            db.record_permission_event(None, request.owner_user_id, "tool_denied", "owner inactive")
            raise HTTPException(status_code=403, detail="创建者不可用")
        if db.active_tool_ids(configured_tools) != configured_tools:
            raise HTTPException(status_code=422, detail="包含未注册或已关闭工具")
        role_scopes = db.get_user_role_tool_scopes(request.owner_user_id)
        if not role_scopes:
            raise HTTPException(status_code=403, detail="创建者缺少角色工具权限")
        creator_permissions = set(role_scopes)
        try:
            validate_configured_tools(configured_tools, creator_permissions)
            for tool_id, scope in tool_scopes.items():
                validate_tool_scope_subset(tool_id, scope, role_scopes.get(tool_id))
        except PermissionError as error:
            db.record_permission_event(None, request.owner_user_id, "tool_denied", str(error))
            raise HTTPException(status_code=403, detail=str(error)) from error
        agent_id = str(uuid4())
        db.save_agent(agent_id, request, creator_permissions, tool_scopes)
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return AgentCreateResponse(agent_id=agent_id, tools=sorted(configured_tools))


@app.get("/agents", response_model=list[AgentSummary])
def list_agents(authorization: str | None = Header(default=None)) -> list[AgentSummary]:
    try:
        db.ensure_schema()
        actor_id = optional_current_user_id(authorization)
        is_admin = bool(actor_id and db.user_has_role(actor_id, PLATFORM_ADMIN_ROLE))
        return db.fetch_agents(actor_id=actor_id, is_admin=is_admin)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.get("/agents/{agent_id}", response_model=AgentDetail)
def get_agent(
    agent_id: str,
    authorization: str | None = Header(default=None),
) -> AgentDetail:
    try:
        db.ensure_schema()
        actor_id = current_user_id(authorization)
        agent = db.load_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        require_agent_manager(actor_id, agent)
        return agent_detail(agent_id, agent)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.put("/agents/{agent_id}", response_model=AgentDetail)
def update_agent(
    agent_id: str,
    request: AgentUpdateRequest,
    authorization: str | None = Header(default=None),
) -> AgentDetail:
    tool_scopes = normalize_agent_tool_request(request)
    configured_tools = set(tool_scopes)
    try:
        db.ensure_schema()
        actor_id = require_current_actor(authorization, request.actor_id)
        agent = db.load_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        require_agent_manager(actor_id, agent)
        if db.active_tool_ids(configured_tools) != configured_tools:
            raise HTTPException(status_code=422, detail="包含未注册或已关闭工具")
        owner_id = str(agent["owner_user_id"])
        owner_scopes = db.get_user_role_tool_scopes(owner_id)
        try:
            validate_configured_tools(configured_tools, set(owner_scopes))
            for tool_id, scope in tool_scopes.items():
                validate_tool_scope_subset(tool_id, scope, owner_scopes.get(tool_id))
        except PermissionError as error:
            db.record_permission_event(agent_id, actor_id, "tool_denied", str(error))
            raise HTTPException(status_code=403, detail=str(error)) from error
        members = [] if request.visibility == "public" else [
            member for member in request.members if member != owner_id
        ]
        normalized_request = request.model_copy(update={"members": members})
        db.update_agent(
            agent_id,
            normalized_request,
            set(owner_scopes),
            tool_scopes,
            actor_id,
        )
        updated = {
            **agent,
            "name": normalized_request.name,
            "icon": normalized_request.icon,
            "visibility": normalized_request.visibility,
            "members": set(members),
            "system_prompt": normalized_request.system_prompt,
            "model": normalized_request.model,
            "tools": configured_tools,
            "tool_scopes": tool_scopes,
            "snapshot_tools": set(owner_scopes),
            "channels": set(normalized_request.channels),
        }
        db.record_permission_event(agent_id, actor_id, "agent_updated", "draft configuration")
        return agent_detail(agent_id, updated)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.put("/admin/agents/{agent_id}/status")
def set_agent_status(
    agent_id: str,
    request: AgentStatusRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    try:
        db.ensure_schema()
        actor_id = require_current_platform_admin(authorization)
        if request.actor_id != actor_id:
            raise HTTPException(status_code=403, detail="请求用户与登录用户不一致")
        if db.load_agent(agent_id) is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        if not db.update_agent_status(agent_id, request.enabled):
            raise HTTPException(status_code=404, detail="Agent 不存在")
        detail = "enabled" if request.enabled else "disabled"
        db.record_permission_event(agent_id, actor_id, "agent_status_updated", detail)
        return {"agent_id": agent_id, "enabled": request.enabled}
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post("/agents/{agent_id}/releases", response_model=AgentReleaseResponse, status_code=201)
def publish_agent(
    agent_id: str,
    request: AgentReleaseRequest,
    authorization: str | None = Header(default=None),
) -> AgentReleaseResponse:
    try:
        db.ensure_schema()
        actor_id = require_current_actor(authorization, request.actor_id)
        agent = db.load_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        require_agent_manager(actor_id, agent)
        release_id = str(uuid4())
        version = db.next_release_version(agent_id)
        db.save_agent_release(
            release_id,
            agent_id,
            version,
            release_config_snapshot(agent_id, agent, actor_id),
            actor_id,
        )
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return AgentReleaseResponse(
        release_id=release_id,
        agent_id=agent_id,
        version=version,
        status="published",
    )


@app.get("/agents/{agent_id}/releases", response_model=list[AgentReleaseSummary])
def list_agent_releases(agent_id: str) -> list[AgentReleaseSummary]:
    try:
        db.ensure_schema()
        return [AgentReleaseSummary(**release) for release in db.fetch_agent_releases(agent_id)]
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post(
    "/agents/{agent_id}/releases/{release_id}/revoke",
    response_model=AgentReleaseResponse,
)
def revoke_agent_release(
    agent_id: str,
    release_id: str,
    request: AgentReleaseRequest,
    authorization: str | None = Header(default=None),
) -> AgentReleaseResponse:
    try:
        db.ensure_schema()
        actor_id = require_current_actor(authorization, request.actor_id)
        agent = db.load_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        require_agent_manager(actor_id, agent)
        release = db.fetch_agent_release(agent_id, release_id)
        if release is None:
            raise HTTPException(status_code=404, detail="发布版本不存在")
        if not db.revoke_agent_release(agent_id, release_id):
            raise HTTPException(status_code=409, detail="发布版本已撤销")
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return AgentReleaseResponse(
        release_id=release_id,
        agent_id=agent_id,
        version=release["version"],
        status="revoked",
    )


@app.get("/agents/{agent_id}/releases/{release_id}")
def get_agent_release(agent_id: str, release_id: str) -> dict[str, object]:
    try:
        db.ensure_schema()
        release = db.fetch_agent_release(agent_id, release_id)
        if release is None:
            raise HTTPException(status_code=404, detail="发布版本不存在")
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return release


@app.post(
    "/agents/{agent_id}/releases/{release_id}/runs",
    response_model=AgentRunResponse,
)
def run_agent_release(
    agent_id: str, release_id: str, request: AgentRunRequest
) -> AgentRunResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")

    requested_tools = list(dict.fromkeys(request.tool_ids))
    run_id = str(uuid4())
    try:
        db.ensure_schema()
        runtime_ctx = runtime_context.fetch_runtime_context(
            agent_id, release_id, actor_id=request.actor_id, channel=request.channel
        )
        agent, allowed_tools = resolve_release_access(
            agent_id, release_id, request.actor_id, request.channel
        )
        if not set(requested_tools).issubset(allowed_tools):
            db.record_permission_event(
                agent_id,
                request.actor_id,
                "tool_denied",
                f"release={release_id};requested={','.join(sorted(requested_tools))}",
            )
            raise HTTPException(status_code=403, detail="工具未获授权")
        db.create_run(run_id, message, agent_id, request.actor_id)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error

    try:
        result = execute_agent_run(
            run_id,
            agent_id,
            message,
            request,
            agent,
            allowed_tools,
            requested_tools,
            runtime_ctx=runtime_ctx,
        )
    except Exception as error:
        try:
            db.update_run(run_id, "failed", error=str(error))
        except (psycopg.Error, RuntimeError):
            pass
        raise HTTPException(status_code=502, detail=llm_error_detail(error)) from error

    try:
        persist_agent_run_result(run_id, agent_id, result)
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="审计日志写入失败") from error
    return result


@app.post("/agents/{agent_id}/authorize", response_model=AuthorizeResponse)
def authorize_tool(agent_id: str, request: AuthorizeRequest) -> AuthorizeResponse:
    try:
        db.ensure_schema()
        _, allowed_tools = resolve_agent_access(agent_id, request.actor_id, request.channel)
        requested_tools = {request.tool_id, *request.override_tools}
        if not requested_tools.issubset(allowed_tools):
            db.record_permission_event(
                agent_id,
                request.actor_id,
                "tool_denied",
                f"requested={','.join(sorted(requested_tools))}",
            )
            raise HTTPException(status_code=403, detail="工具未获授权")
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error
    return AuthorizeResponse(effective_tools=sorted(allowed_tools))


@app.post("/agents/{agent_id}/runs", response_model=AgentRunResponse)
def run_agent(agent_id: str, request: AgentRunRequest) -> AgentRunResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")

    requested_tools = list(dict.fromkeys(request.tool_ids))
    run_id = str(uuid4())
    try:
        db.ensure_schema()
        agent, allowed_tools = resolve_agent_access(agent_id, request.actor_id, request.channel)
        if not set(requested_tools).issubset(allowed_tools):
            db.record_permission_event(
                agent_id,
                request.actor_id,
                "tool_denied",
                f"requested={','.join(sorted(requested_tools))}",
            )
            raise HTTPException(status_code=403, detail="工具未获授权")
        db.create_run(run_id, message, agent_id, request.actor_id)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error

    try:
        result = execute_agent_run(
            run_id,
            agent_id,
            message,
            request,
            agent,
            allowed_tools,
            requested_tools,
        )
    except Exception as error:
        try:
            db.update_run(run_id, "failed", error=str(error))
        except (psycopg.Error, RuntimeError):
            pass
        raise HTTPException(status_code=502, detail=llm_error_detail(error)) from error

    try:
        persist_agent_run_result(run_id, agent_id, result)
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="审计日志写入失败") from error
    return result


@app.post(
    "/gateway/approvals/{approval_id}/decide",
    response_model=AgentRunResponse,
)
def decide_gateway_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    authorization: str | None = Header(default=None),
) -> AgentRunResponse:
    try:
        db.ensure_schema()
        require_current_actor(authorization, request.actor_id)
        payload = request.model_dump()
        payload["decided_by"] = request.actor_id
        result = runner.decide_gateway_approval(approval_id, payload)
        if result.run_id:
            db.update_run(result.run_id, result.status, answer=result.answer)
        return result
    except HTTPException:
        raise
    except runner.GatewayRunnerError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=llm_error_detail(error)) from error


@app.get("/runs", response_model=list[RunSummary])
def list_runs() -> list[RunSummary]:
    try:
        db.ensure_schema()
        return db.fetch_runs()
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error


@app.post("/runs", response_model=RunResponse)
def run(request: RunRequest) -> RunResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")

    run_id = str(uuid4())
    try:
        db.ensure_schema()
        db.create_run(run_id, message)
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="数据库不可用或未配置") from error

    try:
        answer = runner.call_llm(message)
    except Exception as error:
        try:
            db.update_run(run_id, "failed", error=str(error))
        except (psycopg.Error, RuntimeError):
            pass
        raise HTTPException(status_code=502, detail=llm_error_detail(error)) from error

    try:
        db.update_run(run_id, "succeeded", answer=answer)
    except (psycopg.Error, RuntimeError) as error:
        raise HTTPException(status_code=503, detail="审计日志写入失败") from error
    return RunResponse(run_id=run_id, answer=answer)
