import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class RunRequest(BaseModel):
    message: str = Field(max_length=10_000)


class RunResponse(BaseModel):
    run_id: str
    answer: str


class UserRequest(BaseModel):
    status: str = "active"


class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)


class UserProfile(BaseModel):
    user_id: str
    status: str
    roles: list[str]
    role_tool_scopes: dict[str, dict[str, list[str]]]
    is_platform_admin: bool


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserProfile


class AuthConfigResponse(BaseModel):
    auth_mode: str
    oa_login_url: str | None = None


class MCPConnectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    transport: str = Field(pattern="^(streamable_http|stdio)$")
    url: str | None = Field(default=None, max_length=2_000)
    command: str | None = Field(default=None, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=100)
    cwd: str | None = Field(default=None, max_length=1_000)
    env_vars: list[str] = Field(default_factory=list, max_length=100)
    bearer_env: str | None = Field(default=None, max_length=200)
    headers: dict[str, str] = Field(default_factory=dict)
    connect_timeout_ms: int = Field(default=10_000, ge=100, le=300_000)
    call_timeout_ms: int = Field(default=30_000, ge=100, le=300_000)
    idle_ttl_seconds: int = Field(default=900, ge=1, le=86_400)
    discovery_ttl_seconds: int = Field(default=300, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_transport_config(self) -> "MCPConnectionRequest":
        if self.transport == "streamable_http":
            parsed = urlsplit(self.url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("streamable_http 必须配置有效的 http/https URL")
        elif not self.command:
            raise ValueError("stdio 必须配置 command")
        return self

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for raw_key, raw_value in headers.items():
            key = str(raw_key).strip()
            value = str(raw_value)
            if not key:
                continue
            if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", key):
                raise ValueError(f"Header 名称不合法: {key}")
            if "\r" in value or "\n" in value:
                raise ValueError(f"Header 值不能包含换行: {key}")
            if len(value) > 2_000:
                raise ValueError(f"Header 值过长: {key}")
            clean[key] = value
        return clean

    def runtime_config(self) -> dict[str, Any]:
        common = {
            "connect_timeout_ms": self.connect_timeout_ms,
            "call_timeout_ms": self.call_timeout_ms,
            "idle_ttl_seconds": self.idle_ttl_seconds,
            "discovery_ttl_seconds": self.discovery_ttl_seconds,
        }
        if self.transport == "streamable_http":
            return {
                **common,
                "url": self.url,
                "bearer_env": self.bearer_env,
                "headers": self.headers,
            }
        return {
            **common,
            "command": self.command,
            "args": self.args,
            "cwd": self.cwd,
            "env_vars": self.env_vars,
        }


class MCPConnectionDefinition(BaseModel):
    connection_id: str
    name: str
    status: str
    transport: str
    config: dict[str, Any]
    config_version: int
    config_hash: str

    @property
    def runner_tool_id(self) -> str:
        if self.transport == "streamable_http":
            url = str(self.config.get("url") or "")
            bearer_env = self.config.get("bearer_env")
            if bearer_env:
                parsed = urlsplit(url)
                query = [*parse_qsl(parsed.query), ("bearer_env", str(bearer_env))]
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
            return f"mcp+{url}"
        command = quote(str(self.config.get("command") or ""), safe="/")
        query: list[tuple[str, str]] = [
            ("arg", str(value)) for value in self.config.get("args") or []
        ]
        if self.config.get("cwd"):
            query.append(("cwd", str(self.config["cwd"])))
        env_vars = [str(value) for value in self.config.get("env_vars") or []]
        if env_vars:
            query.append(("env", ",".join(env_vars)))
        return f"mcp+stdio://{command}?{urlencode(query)}"

    def runtime_snapshot(self) -> dict[str, Any]:
        return self.model_dump()


class MCPDiscoveryResponse(BaseModel):
    connection_id: str
    discovered_count: int
    tool_ids: list[str]
    disabled_tool_ids: list[str] = Field(default_factory=list)


class ToolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    source: str = Field(default="http", pattern="^(mcp|http|workflow|workflow_agent|local)$")
    runner_tool_id: str | None = Field(default=None, max_length=200)
    runner_name: str | None = Field(default=None, max_length=100)
    mcp_connection_id: str | None = Field(default=None, max_length=100)
    version: str = Field(default="dev", max_length=100)
    read_only: bool = True
    idempotent: bool = True
    parallel_safe: bool = True
    requires_approval: bool = False
    side_effect_level: str = Field(
        default="none",
        pattern="^(none|external_read|internal_write|external_write|destructive)$",
    )
    data_sensitivity: str = Field(
        default="internal", pattern="^(public|internal|confidential|secret)$"
    )
    network_access: str = Field(
        default="none", pattern="^(none|internal_only|external)$"
    )
    layer: str = Field(default="support", pattern="^(runtime|support|business|integration|workflow)$")
    category: str = Field(default="general", max_length=100)
    namespace: str = Field(default="default", max_length=100)
    group_id: str = Field(default="default", max_length=100)
    group_name: str = Field(default="Default", max_length=100)
    timeout_ms: int = Field(default=8_000, ge=100, le=300_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    scope_schema: dict[str, list[str]] = Field(default_factory=dict)
    scope_descriptions: dict[str, str] = Field(default_factory=dict)
    manifest_hash: str | None = Field(default=None, max_length=200)


class ToolStatusRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    status: str = Field(pattern="^(active|disabled)$")


class ToolDefinition(BaseModel):
    tool_id: str
    name: str
    description: str
    status: str = "active"
    source: str = Field(default="local", pattern="^(mcp|http|workflow|workflow_agent|local)$")
    runner_tool_id: str | None = None
    runner_name: str | None = None
    mcp_connection_id: str | None = None
    mcp_connection: MCPConnectionDefinition | None = None
    version: str = "dev"
    read_only: bool = True
    idempotent: bool = True
    parallel_safe: bool = True
    requires_approval: bool = False
    side_effect_level: str = "none"
    data_sensitivity: str = "internal"
    network_access: str = "none"
    layer: str = "support"
    category: str = "general"
    namespace: str = "default"
    group_id: str = "default"
    group_name: str = "Default"
    timeout_ms: int = 8_000
    input_schema: dict[str, Any] = Field(default_factory=dict)
    scope_schema: dict[str, list[str]] = Field(default_factory=dict)
    scope_descriptions: dict[str, str] = Field(default_factory=dict)
    manifest_hash: str = ""


class WorkflowCapabilityRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    mcp_connection_id: str = Field(min_length=1, max_length=100)
    runner_name: str = Field(min_length=1, max_length=100)
    version: str = Field(default="v1", min_length=1, max_length=100)
    requires_approval: bool = False
    side_effect_level: str = Field(
        default="internal_write",
        pattern="^(none|external_read|internal_write|external_write|destructive)$",
    )
    data_sensitivity: str = Field(
        default="internal", pattern="^(public|internal|confidential|secret)$"
    )
    timeout_ms: int = Field(default=30_000, ge=100, le=300_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    scope_schema: dict[str, list[str]] = Field(default_factory=dict)
    scope_descriptions: dict[str, str] = Field(default_factory=dict)


class WorkflowVersionSummary(BaseModel):
    workflow_id: str
    version: str
    lifecycle_status: str = Field(pattern="^(active|retired)$")
    manifest_hash: str
    created_by: str
    created_at: datetime
    activated_by: str
    activated_at: datetime
    retired_by: str | None = None
    retired_at: datetime | None = None


class AgentToolConfig(BaseModel):
    tool_id: str = Field(min_length=1, max_length=100)
    scope: dict[str, list[str]] = Field(default_factory=dict)


class AgentCreateRequest(BaseModel):
    owner_user_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    icon: str = Field(default="robot", max_length=50)
    visibility: str = Field(default="private", pattern="^(private|public)$")
    members: list[str] = Field(default_factory=list)
    system_prompt: str = Field(default="", max_length=20_000)
    model: str | None = Field(default=None, max_length=100)
    tools: list[str | AgentToolConfig] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=lambda: ["web_console"])


class AgentCreateResponse(BaseModel):
    agent_id: str
    tools: list[str]


class AgentUpdateRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    icon: str = Field(default="robot", max_length=50)
    visibility: str = Field(default="private", pattern="^(private|public)$")
    members: list[str] = Field(default_factory=list)
    system_prompt: str = Field(default="", max_length=20_000)
    model: str | None = Field(default=None, max_length=100)
    tools: list[str | AgentToolConfig] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=lambda: ["web_console"])


class AgentStatusRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    enabled: bool


class AgentSummary(BaseModel):
    agent_id: str
    name: str
    icon: str
    owner_user_id: str
    visibility: str = "private"
    members: list[str] = Field(default_factory=list)
    enabled: bool
    tools: list[str]


class AgentDetail(AgentSummary):
    system_prompt: str
    model: str | None = None
    tool_configs: list[AgentToolConfig] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class AuthorizeRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    channel: str = Field(min_length=1, max_length=100)
    tool_id: str = Field(min_length=1, max_length=100)
    override_tools: list[str] = Field(default_factory=list)


class AuthorizeResponse(BaseModel):
    effective_tools: list[str]


class AgentRunRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    channel: str = Field(default="web_console", min_length=1, max_length=100)
    message: str = Field(max_length=10_000)
    session_id: str | None = Field(default=None, max_length=100)
    tool_ids: list[str] = Field(default_factory=list)
    runner_backend: str | None = Field(default=None, pattern="^(debug|gateway)$")


class ToolCallTrace(BaseModel):
    tool_id: str
    status: str
    result: str


class ApprovalDecisionRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = Field(default="", max_length=1_000)


class AgentRunResponse(BaseModel):
    run_id: str
    answer: str = ""
    tool_calls: list[ToolCallTrace]
    status: str = "succeeded"
    approval_requests: list[dict[str, Any]] = Field(default_factory=list)


class RunSummary(BaseModel):
    run_id: str
    agent_id: str | None
    actor_id: str | None
    message: str
    answer: str | None
    status: str
    created_at: str


class RolePermission(BaseModel):
    tool_id: str
    scope: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("scope", mode="before")
    @classmethod
    def default_empty_scope(cls, value: Any) -> Any:
        # 外部工具可以只做工具级授权；空 scope 不应被表单空值挡住。
        return {} if value is None else value


class RoleSummary(BaseModel):
    role_id: str
    name: str
    description: str = ""
    status: str
    is_system: bool = False
    permissions: list[RolePermission]


class RoleUpsertRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    role_id: str = Field(min_length=2, max_length=100, pattern=r"^[\w][\w.-]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    permissions: list[RolePermission] = Field(default_factory=list)


class RoleAssignRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    roles: list[str] = Field(default_factory=list)


class AdminUserSummary(BaseModel):
    user_id: str
    status: str
    roles: list[str]


class SeedRolesResponse(BaseModel):
    roles: list[str]
    admin_users: list[str]


class AgentReleaseRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)


class AgentReleaseResponse(BaseModel):
    release_id: str
    agent_id: str
    version: int
    status: str


class AgentReleaseSummary(BaseModel):
    release_id: str
    agent_id: str
    version: int
    status: str
    config_json: dict[str, Any]
    published_by: str
    published_at: str
    revoked_at: str | None = None
