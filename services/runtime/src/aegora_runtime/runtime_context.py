from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from aegora_runtime.runtime_cache import get_runtime_config_cache
from aegora_runtime.tool_adapters import local_mcp_stdio_runner_tool_id


Scope = dict[str, list[str]]
PLATFORM_ADMIN_ROLE = "platform_admin"


class RuntimeContextError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def agent_platform_database_url() -> str:
    url = os.environ.get("AGENT_PLATFORM_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeContextError(
            "AGENT_PLATFORM_DATABASE_URL 或 DATABASE_URL 未配置",
            status_code=503,
        )
    return url


@contextmanager
def connect_agent_platform() -> Iterator[Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 依赖缺失在部署检查中暴露。
        raise RuntimeContextError("缺少 psycopg 依赖", status_code=503) from exc

    with psycopg.connect(agent_platform_database_url(), row_factory=dict_row) as conn:
        yield conn


def normalize_scope(scope: dict[str, object] | None) -> Scope:
    normalized: Scope = {}
    if not scope:
        return normalized
    for key, values in scope.items():
        if not isinstance(key, str) or not isinstance(values, list):
            raise RuntimeContextError("scope 只支持 dict[str, list[str]]")
        clean_values = []
        for value in values:
            if not isinstance(value, str):
                raise RuntimeContextError("scope 只支持字符串列表")
            clean_values.append(value)
        normalized[key] = sorted(set(clean_values))
    return normalized


def release_tool_ids(config_json: dict[str, object]) -> set[str]:
    tools = config_json.get("tools") or []
    return {
        str(tool.get("tool_id"))
        for tool in tools
        if isinstance(tool, dict) and tool.get("tool_id")
    }


def active_tool_ids(tool_ids: set[str]) -> set[str]:
    if not tool_ids:
        return set()
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tools.id
                FROM tools
                LEFT JOIN mcp_connections ON mcp_connections.id = tools.mcp_connection_id
                WHERE tools.id = ANY(%s)
                  AND tools.status = 'active'
                  AND (
                      tools.mcp_connection_id IS NULL
                      OR mcp_connections.status = 'active'
                  )
                """,
                (list(tool_ids),),
            )
            return {str(row["id"]) for row in cur.fetchall()}


def fetch_active_mcp_connections(
    connection_ids: set[str],
) -> dict[str, dict[str, object]]:
    if not connection_ids:
        return {}
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, status, transport, config_json, config_version, config_hash
                FROM mcp_connections
                WHERE id = ANY(%s) AND status = 'active'
                """,
                (list(connection_ids),),
            )
            return {
                str(row["id"]): {
                    "connection_id": str(row["id"]),
                    "name": row["name"],
                    "status": row["status"],
                    "transport": row["transport"],
                    "config": row["config_json"],
                    "config_version": row["config_version"],
                    "config_hash": row["config_hash"],
                }
                for row in cur.fetchall()
            }


def fetch_active_tool_runtime_policies(tool_ids: set[str]) -> dict[str, dict[str, object]]:
    if not tool_ids:
        return {}
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,
                       read_only,
                       idempotent,
                       parallel_safe,
                       requires_approval,
                       side_effect_level,
                       data_sensitivity,
                       network_access,
                       timeout_ms
                FROM tools
                WHERE id = ANY(%s) AND status = 'active'
                """,
                (list(tool_ids),),
            )
            return {
                str(row["id"]): {
                    "read_only": row["read_only"],
                    "idempotent": row["idempotent"],
                    "parallel_safe": row["parallel_safe"],
                    "requires_approval": row["requires_approval"],
                    "side_effect_level": row["side_effect_level"],
                    "data_sensitivity": row["data_sensitivity"],
                    "network_access": row["network_access"],
                    "timeout_ms": row["timeout_ms"],
                }
                for row in cur.fetchall()
            }


def refresh_active_tool_runtime_policies(
    tools: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    tool_ids = {str(tool["tool_id"]) for tool in tools if tool.get("tool_id")}
    if not tool_ids:
        return tools, []
    policies = fetch_active_tool_runtime_policies(tool_ids)
    refreshed = []
    changed = []
    for tool in tools:
        tool_id = str(tool.get("tool_id") or "")
        policy = policies.get(tool_id)
        if policy is None:
            refreshed.append(tool)
            continue
        merged = {**tool, **policy}
        if any(tool.get(key) != merged.get(key) for key in policy):
            changed.append(tool_id)
        refreshed.append(merged)
    return refreshed, sorted(set(changed))


def refresh_mcp_connection_configs(
    tools: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    connection_ids = {
        str(tool["mcp_connection_id"])
        for tool in tools
        if tool.get("mcp_connection_id")
    }
    if not connection_ids:
        return tools, []
    active_connections = fetch_active_mcp_connections(connection_ids)
    refreshed = []
    filtered = []
    for tool in tools:
        connection_id = tool.get("mcp_connection_id")
        if not connection_id:
            refreshed.append(tool)
            continue
        connection = active_connections.get(str(connection_id))
        if connection is None:
            filtered.append(str(tool.get("tool_id") or ""))
            continue
        refreshed.append(
            {
                **tool,
                "runner_tool_id": f"mcp-connection:{connection_id}",
                "mcp_connection": connection,
            }
        )
    return refreshed, sorted(set(filtered))


def merge_tool_scopes(rows: list[tuple[str, dict[str, object]]]) -> dict[str, Scope]:
    merged: dict[str, dict[str, set[str]]] = {}
    for tool_id, raw_scope in rows:
        scope = normalize_scope(raw_scope)
        tool_scope = merged.setdefault(tool_id, {})
        for scope_key, values in scope.items():
            tool_scope.setdefault(scope_key, set()).update(values)
    return {
        tool_id: {key: sorted(values) for key, values in sorted(scope.items())}
        for tool_id, scope in sorted(merged.items())
    }


def scope_is_subset(requested: dict[str, object] | None, allowed: dict[str, object] | None) -> bool:
    requested_scope = normalize_scope(requested)
    allowed_scope = normalize_scope(allowed)
    for key, requested_values in requested_scope.items():
        allowed_values = set(allowed_scope.get(key, []))
        if not set(requested_values).issubset(allowed_values):
            return False
    return True


def effective_tools(
    configured_tools: set[str],
    created_at_capabilities: set[str],
    current_capabilities: set[str],
) -> set[str]:
    return configured_tools & created_at_capabilities & current_capabilities


def user_is_active(user_id: str) -> bool:
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status = 'active' AS active FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return bool(row and row["active"])


def user_has_role(user_id: str, role_id: str) -> bool:
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM user_roles WHERE user_id = %s AND role_id = %s",
                (user_id, role_id),
            )
            return cur.fetchone() is not None


def get_user_role_tool_scopes(user_id: str) -> dict[str, Scope]:
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rtp.tool_id, rtp.scope_json
                FROM user_roles ur
                JOIN roles r ON r.id = ur.role_id AND r.status = 'active'
                JOIN role_tool_permissions rtp ON rtp.role_id = ur.role_id
                WHERE ur.user_id = %s
                """,
                (user_id,),
            )
            return merge_tool_scopes([(str(row["tool_id"]), row["scope_json"]) for row in cur.fetchall()])


def fetch_agent_release(agent_id: str, release_id: str) -> dict[str, object] | None:
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS release_id,
                       agent_id::text AS agent_id,
                       version,
                       status,
                       to_jsonb(agent_releases)->>'visibility' AS visibility
                FROM agent_releases
                WHERE agent_id = %s AND id = %s
                """,
                (agent_id, release_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            release = dict(row)
            config_json = _release_config_json(
                cur,
                release,
                agent_id=agent_id,
                release_id=release_id,
            )
            release["config_json"] = config_json
            return release


def fetch_agent_release_by_version(agent_id: str, version: int) -> dict[str, object] | None:
    with connect_agent_platform() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text AS release_id,
                       agent_id::text AS agent_id,
                       version,
                       status,
                       to_jsonb(agent_releases)->>'visibility' AS visibility
                FROM agent_releases
                WHERE agent_id = %s AND version = %s
                ORDER BY published_at DESC NULLS LAST, id DESC
                LIMIT 1
                """,
                (agent_id, version),
            )
            row = cur.fetchone()
            if not row:
                return None
            release = dict(row)
            release_id = str(release["release_id"])
            release["config_json"] = _release_config_json(
                cur,
                release,
                agent_id=agent_id,
                release_id=release_id,
            )
            return release


def _release_config_json(
    cur: Any,
    release: dict[str, object],
    *,
    agent_id: str,
    release_id: str,
) -> dict[str, object]:
    version = int(release["version"])
    cache = get_runtime_config_cache()
    cached = cache.get(agent_id=agent_id, release_id=release_id, version=version)
    if cached is not None:
        return cached

    cur.execute(
        "SELECT config_json FROM agent_releases WHERE agent_id = %s AND id = %s",
        (agent_id, release_id),
    )
    config_row = cur.fetchone()
    if not config_row or not isinstance(config_row.get("config_json"), dict):
        raise RuntimeContextError("release config_json 不合法")
    config_json = dict(config_row["config_json"])
    cache.set(
        config_json,
        agent_id=agent_id,
        release_id=release_id,
        version=version,
    )
    return config_json


def build_runtime_context(
    release: dict[str, object],
    actor_id: str | None = None,
    channel: str | None = None,
) -> dict[str, object]:
    config_json = release["config_json"]
    if not isinstance(config_json, dict):
        raise RuntimeContextError("release config_json 不合法")
    agent_config = config_json.get("agent")
    if not isinstance(agent_config, dict):
        raise RuntimeContextError("release agent 配置缺失")

    raw_tools = config_json.get("tools") or []
    if not isinstance(raw_tools, list):
        raise RuntimeContextError("release tools 配置不合法")

    released_tool_ids = release_tool_ids(config_json)
    active_ids = active_tool_ids(released_tool_ids)
    active_tool_configs = [
        normalize_tool_config(tool)
        for tool in raw_tools
        if isinstance(tool, dict) and tool.get("tool_id") in active_ids
    ]
    active_tool_configs, policy_refreshed = refresh_active_tool_runtime_policies(
        active_tool_configs
    )
    active_tool_configs, connection_filtered = refresh_mcp_connection_configs(
        active_tool_configs
    )
    permission_snapshot = (
        config_json.get("permission_snapshot")
        if isinstance(config_json.get("permission_snapshot"), dict)
        else {}
    )
    authz_result = apply_runtime_tool_authz(
        active_tool_configs,
        actor_id=actor_id,
        permission_snapshot=permission_snapshot,
    )
    active_tool_configs = authz_result["tools"]
    tool_scopes = {
        str(tool["tool_id"]): normalize_scope(tool.get("scope") if isinstance(tool.get("scope"), dict) else None)
        for tool in active_tool_configs
    }

    return {
        "release": {
            "release_id": release["release_id"],
            "agent_id": release["agent_id"],
            "version": release["version"],
            "status": release["status"],
            "visibility": release.get("visibility") or agent_config.get("visibility") or "private",
        },
        "agent": {
            "id": agent_config.get("id"),
            "name": agent_config.get("name") or "released-agent",
            "icon": agent_config.get("icon") or "robot",
            "owner_user_id": agent_config.get("owner_user_id"),
            "system_prompt": agent_config.get("system_prompt") or "",
            "model": agent_config.get("model"),
            "enabled": bool(agent_config.get("enabled", True)),
            "channels": list(agent_config.get("channels") or []),
        },
        "actor": {"actor_id": actor_id},
        "channel": channel,
        "tools": active_tool_configs,
        "tool_ids": sorted(tool_scopes),
        "tool_scopes": tool_scopes,
        "permission_snapshot": permission_snapshot,
        "policy": {
            "source": "agent_release",
            "disabled_tools_filtered": sorted(released_tool_ids - active_ids),
            "runtime_tool_policy_refreshed": policy_refreshed,
            "mcp_connection_filtered_tools": connection_filtered,
            **authz_result["policy"],
        },
    }


def apply_runtime_tool_authz(
    tools: list[dict[str, object]],
    *,
    actor_id: str | None,
    permission_snapshot: dict[str, object],
) -> dict[str, object]:
    if not actor_id:
        return {
            "tools": [],
            "policy": {
                "runtime_authz_filtered": sorted(str(tool.get("tool_id")) for tool in tools),
                "scope_denied_tools": [],
                "runtime_authz_reason": "missing_actor_id",
            },
        }

    current_scopes = get_user_role_tool_scopes(actor_id)
    configured_tool_ids = {str(tool["tool_id"]) for tool in tools}
    created_scopes = snapshot_role_tool_scopes(permission_snapshot)
    created_capabilities = set(created_scopes) if created_scopes else set(configured_tool_ids)
    effective = effective_tools(configured_tool_ids, created_capabilities, set(current_scopes))

    allowed_tools = []
    scope_denied = []
    for tool in tools:
        tool_id = str(tool["tool_id"])
        requested_scope = tool.get("scope") if isinstance(tool.get("scope"), dict) else {}
        if tool_id not in effective:
            continue
        if created_scopes and not scope_is_subset(requested_scope, created_scopes.get(tool_id)):
            scope_denied.append(tool_id)
            continue
        if not scope_is_subset(requested_scope, current_scopes.get(tool_id)):
            scope_denied.append(tool_id)
            continue
        allowed_tools.append(tool)

    allowed_ids = {str(tool["tool_id"]) for tool in allowed_tools}
    return {
        "tools": allowed_tools,
        "policy": {
            "runtime_authz_filtered": sorted(configured_tool_ids - allowed_ids),
            "scope_denied_tools": sorted(set(scope_denied)),
            "runtime_authz_source": "role_tool_permissions",
        },
    }


def snapshot_role_tool_scopes(permission_snapshot: dict[str, object]) -> dict[str, Scope]:
    role_scopes = permission_snapshot.get("role_tool_scopes")
    if not isinstance(role_scopes, dict):
        return {}
    result: dict[str, Scope] = {}
    for tool_id, scope in role_scopes.items():
        if isinstance(tool_id, str):
            result[tool_id] = normalize_scope(scope if isinstance(scope, dict) else None)
    return result


def normalize_tool_config(tool: dict[str, object]) -> dict[str, object]:
    tool_id = str(tool["tool_id"])
    runner_tool_id = str(tool.get("runner_tool_id") or f"local.{tool_id}")
    runner_name = tool.get("runner_name")
    source = str(tool.get("source") or "")
    if source == "local" or runner_tool_id.startswith("local."):
        runner_name = runner_name or runner_tool_id.removeprefix("local.") or tool_id
        runner_tool_id = local_mcp_stdio_runner_tool_id()
        source = "mcp"
    return {
        **tool,
        "tool_id": tool_id,
        "runner_tool_id": runner_tool_id,
        "runner_name": runner_name,
        "name": tool.get("name") or tool_id,
        "description": tool.get("description") or "",
        "source": source or tool.get("source") or "mcp",
        "input_schema": tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {},
        "scope": normalize_scope(tool.get("scope") if isinstance(tool.get("scope"), dict) else None),
        "read_only": bool(tool.get("read_only", True)),
        "idempotent": bool(tool.get("idempotent", True)),
        "parallel_safe": bool(tool.get("parallel_safe", True)),
        "requires_approval": bool(tool.get("requires_approval", False)),
        "side_effect_level": tool.get("side_effect_level") or "none",
        "data_sensitivity": tool.get("data_sensitivity") or "internal",
        "network_access": tool.get("network_access") or "none",
        "timeout_ms": int(tool.get("timeout_ms") or 8000),
    }


def validate_runtime_context(context: dict[str, object]) -> None:
    release = _dict_value(context, "release")
    agent = _dict_value(context, "agent")
    actor = _dict_value(context, "actor")
    actor_id = actor.get("actor_id")
    channel = context.get("channel")

    if release.get("status") != "published":
        raise RuntimeContextError("发布版本不可运行", status_code=409)
    if not agent.get("enabled", True):
        raise RuntimeContextError("Agent 不可用", status_code=403)
    if not isinstance(actor_id, str) or not actor_id:
        raise RuntimeContextError("actor_id 缺失", status_code=401)
    if not user_is_active(actor_id):
        raise RuntimeContextError("执行用户不可用", status_code=403)
    owner_user_id = agent.get("owner_user_id")
    visibility = str(release.get("visibility") or "private")
    if visibility != "public" and actor_id != owner_user_id and not user_has_role(actor_id, PLATFORM_ADMIN_ROLE):
        raise RuntimeContextError("当前用户不能执行该发布版本", status_code=403)
    channels = {str(item) for item in agent.get("channels") or []}
    if channel not in channels:
        raise RuntimeContextError("渠道未授权", status_code=403)


def resolve_runtime_context(
    agent_id: str,
    release_id: str,
    actor_id: str,
    channel: str,
) -> dict[str, object]:
    release = fetch_agent_release(agent_id, release_id)
    if release is None:
        raise RuntimeContextError("发布版本不存在", status_code=404)
    context = build_runtime_context(release, actor_id=actor_id, channel=channel)
    validate_runtime_context(context)
    return context


def resolve_runtime_context_by_version(
    agent_id: str,
    version: int,
    actor_id: str,
    channel: str,
) -> dict[str, object]:
    release = fetch_agent_release_by_version(agent_id, version)
    if release is None:
        raise RuntimeContextError("发布版本不存在", status_code=404)
    context = build_runtime_context(release, actor_id=actor_id, channel=channel)
    validate_runtime_context(context)
    return context


def _dict_value(context: dict[str, object], key: str) -> dict[str, object]:
    value = context.get(key)
    if not isinstance(value, dict):
        raise RuntimeContextError(f"runtime context {key} 不合法")
    return value
