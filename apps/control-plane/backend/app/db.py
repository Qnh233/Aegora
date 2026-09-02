import hashlib
import json

import psycopg

from .authz import merge_tool_scopes, normalize_scope
from .config import get_config
from .models import (
    AdminUserSummary,
    AgentCreateRequest,
    AgentSummary,
    AgentUpdateRequest,
    MCPConnectionDefinition,
    MCPConnectionRequest,
    RolePermission,
    RoleSummary,
    RunSummary,
    ToolDefinition,
)


def database_url() -> str:
    return get_config().database_url


def normalize_agent_model(model: str | None) -> str | None:
    # 空模型不固化到 Agent，运行时回退全局 LLM_MODEL。
    stripped = model.strip() if model else ""
    return stripped or None


def ensure_schema() -> None:
    # ponytail: MVP 用幂等 DDL，Phase 5 引入正式 migration。
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id UUID PRIMARY KEY,
                    message TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    completed_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    revoked_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_connections (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    transport TEXT NOT NULL,
                    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    config_version INTEGER NOT NULL DEFAULT 1,
                    config_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tools (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'http',
                    runner_tool_id TEXT,
                    runner_name TEXT,
                    mcp_connection_id TEXT REFERENCES mcp_connections(id) ON DELETE SET NULL,
                    version TEXT NOT NULL DEFAULT 'dev',
                    read_only BOOLEAN NOT NULL DEFAULT TRUE,
                    idempotent BOOLEAN NOT NULL DEFAULT TRUE,
                    parallel_safe BOOLEAN NOT NULL DEFAULT TRUE,
                    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
                    side_effect_level TEXT NOT NULL DEFAULT 'none',
                    data_sensitivity TEXT NOT NULL DEFAULT 'internal',
                    network_access TEXT NOT NULL DEFAULT 'none',
                    layer TEXT NOT NULL DEFAULT 'support',
                    category TEXT NOT NULL DEFAULT 'general',
                    namespace TEXT NOT NULL DEFAULT 'default',
                    group_id TEXT NOT NULL DEFAULT 'default',
                    group_name TEXT NOT NULL DEFAULT 'Default',
                    timeout_ms INTEGER NOT NULL DEFAULT 8000,
                    input_schema_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    scope_schema_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    scope_descriptions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    manifest_hash TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS roles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    is_system BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id TEXT NOT NULL REFERENCES users(id),
                    role_id TEXT NOT NULL REFERENCES roles(id),
                    PRIMARY KEY (user_id, role_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS role_tool_permissions (
                    role_id TEXT NOT NULL REFERENCES roles(id),
                    tool_id TEXT NOT NULL REFERENCES tools(id),
                    scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    PRIMARY KEY (role_id, tool_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS role_change_events (
                    id BIGSERIAL PRIMARY KEY,
                    role_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id UUID PRIMARY KEY,
                    name TEXT NOT NULL,
                    icon TEXT NOT NULL DEFAULT 'robot',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    owner_user_id TEXT NOT NULL,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    model TEXT,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_members (
                    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    permission TEXT NOT NULL DEFAULT 'run',
                    PRIMARY KEY (agent_id, user_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_tools (
                    agent_id UUID NOT NULL REFERENCES agents(id),
                    tool_id TEXT NOT NULL REFERENCES tools(id),
                    scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    PRIMARY KEY (agent_id, tool_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_permission_snapshots (
                    agent_id UUID NOT NULL REFERENCES agents(id),
                    snapshot_version INTEGER NOT NULL,
                    permission_json JSONB NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reason TEXT NOT NULL,
                    PRIMARY KEY (agent_id, snapshot_version)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_permission_events (
                    id BIGSERIAL PRIMARY KEY,
                    agent_id UUID,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    agent_id UUID NOT NULL REFERENCES agents(id),
                    channel TEXT NOT NULL,
                    PRIMARY KEY (agent_id, channel)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_call_logs (
                    id BIGSERIAL PRIMARY KEY,
                    agent_id UUID NOT NULL REFERENCES agents(id),
                    tool_id TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_releases (
                    id UUID PRIMARY KEY,
                    agent_id UUID NOT NULL REFERENCES agents(id),
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    config_json JSONB NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    revoked_at TIMESTAMPTZ,
                    UNIQUE(agent_id, version)
                )
                """
            )
            cursor.execute(
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS system_prompt TEXT NOT NULL DEFAULT ''"
            )
            cursor.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS icon TEXT NOT NULL DEFAULT 'robot'")
            cursor.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS model TEXT")
            cursor.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private'")
            cursor.execute(
                """
                UPDATE agents
                SET visibility = 'private'
                WHERE visibility NOT IN ('private', 'public')
                """
            )
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'http'")
            cursor.execute("ALTER TABLE tools ALTER COLUMN source SET DEFAULT 'http'")
            cursor.execute("UPDATE tools SET source = 'http' WHERE source = 'manual'")
            cursor.execute("UPDATE tools SET source = 'workflow_agent' WHERE source = 'runner'")
            cursor.execute(
                """
                UPDATE tools
                SET source = 'http'
                WHERE source NOT IN ('mcp', 'http', 'workflow_agent', 'local')
                """
            )
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS runner_tool_id TEXT")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS runner_name TEXT")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS mcp_connection_id TEXT")
            cursor.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = 'tools_mcp_connection_id_fkey'"
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    ALTER TABLE tools
                    ADD CONSTRAINT tools_mcp_connection_id_fkey
                    FOREIGN KEY (mcp_connection_id)
                    REFERENCES mcp_connections(id)
                    ON DELETE SET NULL
                    """
                )
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT 'dev'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS read_only BOOLEAN NOT NULL DEFAULT TRUE")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS idempotent BOOLEAN NOT NULL DEFAULT TRUE")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS parallel_safe BOOLEAN NOT NULL DEFAULT TRUE")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS requires_approval BOOLEAN NOT NULL DEFAULT FALSE")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS side_effect_level TEXT NOT NULL DEFAULT 'none'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS data_sensitivity TEXT NOT NULL DEFAULT 'internal'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS network_access TEXT NOT NULL DEFAULT 'none'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS layer TEXT NOT NULL DEFAULT 'support'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'general'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS group_id TEXT NOT NULL DEFAULT 'default'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS group_name TEXT NOT NULL DEFAULT 'Default'")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS timeout_ms INTEGER NOT NULL DEFAULT 8000")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS input_schema_json JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS scope_schema_json JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS scope_descriptions_json JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("ALTER TABLE tools ADD COLUMN IF NOT EXISTS manifest_hash TEXT NOT NULL DEFAULT ''")
            cursor.execute(
                """
                UPDATE tools
                SET layer = 'runtime',
                    category = 'utility',
                    namespace = 'platform',
                    group_id = 'platform.local',
                    group_name = '平台本地工具'
                WHERE id IN ('calculator', 'time_now', 'text_stats')
                  AND source = 'local'
                  AND (layer, category, namespace, group_id, group_name)
                      IS DISTINCT FROM
                      ('runtime', 'utility', 'platform', 'platform.local', '平台本地工具')
                """
            )
            cursor.execute("ALTER TABLE roles ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE roles ADD COLUMN IF NOT EXISTS is_system BOOLEAN NOT NULL DEFAULT FALSE")
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS agent_id UUID")
            cursor.execute("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS actor_id TEXT")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id BIGSERIAL PRIMARY KEY,
                    run_id UUID NOT NULL REFERENCES agent_runs(run_id),
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )


def create_run(
    run_id: str,
    message: str,
    agent_id: str | None = None,
    actor_id: str | None = None,
) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_runs (run_id, message, status, agent_id, actor_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (run_id, message, "running", agent_id, actor_id),
            )
            cursor.execute(
                "INSERT INTO audit_logs (run_id, event_type, detail) VALUES (%s, %s, %s)",
                (run_id, "run_started", "run accepted"),
            )


def update_run(
    run_id: str, status: str, answer: str | None = None, error: str | None = None
) -> None:
    detail = error or "run completed"
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_runs
                SET status = %s, answer = %s, error = %s, completed_at = now()
                WHERE run_id = %s
                """,
                (status, answer, error, run_id),
            )
            cursor.execute(
                "INSERT INTO audit_logs (run_id, event_type, detail) VALUES (%s, %s, %s)",
                (run_id, f"run_{status}", detail),
            )


def upsert_user(user_id: str, status: str) -> None:
    if status not in {"active", "disabled"}:
        raise ValueError("status 只能是 active 或 disabled")
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (id, status) VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status
                """,
                (user_id, status),
            )


def ensure_user_exists(user_id: str, status: str = "active") -> None:
    if status not in {"active", "disabled"}:
        raise ValueError("status 只能是 active 或 disabled")
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (id, status) VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, status),
            )


def compute_tool_manifest_hash(tool: ToolDefinition) -> str:
    payload = tool.model_dump(exclude={"manifest_hash"})
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def compute_mcp_connection_hash(transport: str, config: dict[str, object]) -> str:
    payload = {"transport": transport, "config": config}
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def row_to_mcp_connection(row: tuple) -> MCPConnectionDefinition:
    return MCPConnectionDefinition(
        connection_id=row[0],
        name=row[1],
        status=row[2],
        transport=row[3],
        config=row[4],
        config_version=row[5],
        config_hash=row[6],
    )


def upsert_mcp_connection(
    connection_id: str,
    request: MCPConnectionRequest,
) -> MCPConnectionDefinition:
    runtime_config = request.runtime_config()
    config_hash = compute_mcp_connection_hash(request.transport, runtime_config)
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO mcp_connections (
                    id, name, status, transport, config_json, config_version, config_hash
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, 1, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    status = EXCLUDED.status,
                    transport = EXCLUDED.transport,
                    config_json = EXCLUDED.config_json,
                    config_version = CASE
                        WHEN mcp_connections.config_hash = EXCLUDED.config_hash
                        THEN mcp_connections.config_version
                        ELSE mcp_connections.config_version + 1
                    END,
                    config_hash = EXCLUDED.config_hash,
                    updated_at = now()
                RETURNING id, name, status, transport, config_json, config_version, config_hash
                """,
                (
                    connection_id,
                    request.name,
                    request.status,
                    request.transport,
                    json.dumps(runtime_config, ensure_ascii=False),
                    config_hash,
                ),
            )
            return row_to_mcp_connection(cursor.fetchone())


def fetch_mcp_connections() -> list[MCPConnectionDefinition]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, status, transport, config_json, config_version, config_hash
                FROM mcp_connections
                ORDER BY name, id
                """
            )
            return [row_to_mcp_connection(row) for row in cursor.fetchall()]


def fetch_mcp_connection(connection_id: str) -> MCPConnectionDefinition | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, status, transport, config_json, config_version, config_hash
                FROM mcp_connections
                WHERE id = %s
                """,
                (connection_id,),
            )
            row = cursor.fetchone()
            return row_to_mcp_connection(row) if row else None


def _upsert_tool(
    cursor: psycopg.Cursor,
    tool: ToolDefinition,
    *,
    preserve_status: bool = False,
) -> None:
    manifest_hash = tool.manifest_hash or compute_tool_manifest_hash(tool)
    status_update = "tools.status" if preserve_status else "EXCLUDED.status"
    cursor.execute(
        f"""
                INSERT INTO tools (
                    id, name, description, status, source, runner_tool_id, runner_name,
                    version, read_only, idempotent, parallel_safe, requires_approval,
                    side_effect_level, data_sensitivity, network_access, layer, category,
                    namespace, group_id, group_name, timeout_ms, input_schema_json,
                    scope_schema_json, scope_descriptions_json, manifest_hash, mcp_connection_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    status = {status_update},
                    source = EXCLUDED.source,
                    runner_tool_id = EXCLUDED.runner_tool_id,
                    runner_name = EXCLUDED.runner_name,
                    version = EXCLUDED.version,
                    read_only = EXCLUDED.read_only,
                    idempotent = EXCLUDED.idempotent,
                    parallel_safe = EXCLUDED.parallel_safe,
                    requires_approval = EXCLUDED.requires_approval,
                    side_effect_level = EXCLUDED.side_effect_level,
                    data_sensitivity = EXCLUDED.data_sensitivity,
                    network_access = EXCLUDED.network_access,
                    layer = EXCLUDED.layer,
                    category = EXCLUDED.category,
                    namespace = EXCLUDED.namespace,
                    group_id = EXCLUDED.group_id,
                    group_name = EXCLUDED.group_name,
                    timeout_ms = EXCLUDED.timeout_ms,
                    input_schema_json = EXCLUDED.input_schema_json,
                    scope_schema_json = EXCLUDED.scope_schema_json,
                    scope_descriptions_json = EXCLUDED.scope_descriptions_json,
                    manifest_hash = EXCLUDED.manifest_hash,
                    mcp_connection_id = EXCLUDED.mcp_connection_id
        """,
        (
            tool.tool_id,
            tool.name,
            tool.description,
            tool.status,
            tool.source,
            tool.runner_tool_id,
            tool.runner_name,
            tool.version,
            tool.read_only,
            tool.idempotent,
            tool.parallel_safe,
            tool.requires_approval,
            tool.side_effect_level,
            tool.data_sensitivity,
            tool.network_access,
            tool.layer,
            tool.category,
            tool.namespace,
            tool.group_id,
            tool.group_name,
            tool.timeout_ms,
            json.dumps(tool.input_schema, ensure_ascii=False),
            json.dumps(tool.scope_schema, ensure_ascii=False),
            json.dumps(tool.scope_descriptions, ensure_ascii=False),
            manifest_hash,
            tool.mcp_connection_id,
        ),
    )


def upsert_tool(tool: ToolDefinition) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            _upsert_tool(cursor, tool)


def replace_discovered_mcp_tools(
    connection_id: str,
    tools: list[ToolDefinition],
) -> list[str]:
    if any(
        tool.source != "mcp" or tool.mcp_connection_id != connection_id
        for tool in tools
    ):
        raise ValueError("发现工具与 MCP 连接不匹配")
    tool_ids = [tool.tool_id for tool in tools]
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            for tool in tools:
                # 重发现只更新 manifest，不覆盖管理员手动停用状态。
                _upsert_tool(cursor, tool, preserve_status=True)
            cursor.execute(
                """
                UPDATE tools
                SET status = 'disabled'
                WHERE source = 'mcp'
                  AND mcp_connection_id = %s
                  AND id <> ALL(%s)
                  AND status <> 'disabled'
                RETURNING id
                """,
                (connection_id, tool_ids),
            )
            return sorted(row[0] for row in cursor.fetchall())


def tool_ids_exist(tool_ids: set[str]) -> bool:
    if not tool_ids:
        return True
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM tools WHERE id = ANY(%s)", (list(tool_ids),))
            return {row[0] for row in cursor.fetchall()} == tool_ids


def row_to_tool_definition(row: tuple) -> ToolDefinition:
    return ToolDefinition(
        tool_id=row[0],
        name=row[1],
        description=row[2],
        status=row[3],
        source=row[4],
        runner_tool_id=row[5],
        runner_name=row[6],
        version=row[7],
        read_only=row[8],
        idempotent=row[9],
        parallel_safe=row[10],
        requires_approval=row[11],
        side_effect_level=row[12],
        data_sensitivity=row[13],
        network_access=row[14],
        layer=row[15],
        category=row[16],
        namespace=row[17],
        group_id=row[18],
        group_name=row[19],
        timeout_ms=row[20],
        input_schema=row[21],
        scope_schema=row[22],
        scope_descriptions=row[23],
        manifest_hash=row[24],
        mcp_connection_id=row[25],
    )


def hydrate_tool_connections(
    tools: list[ToolDefinition],
    *,
    require_active: bool = False,
) -> list[ToolDefinition]:
    connection_ids = {
        tool.mcp_connection_id for tool in tools if tool.mcp_connection_id
    }
    connections = {
        connection.connection_id: connection
        for connection in fetch_mcp_connections()
        if connection.connection_id in connection_ids
    }
    hydrated: list[ToolDefinition] = []
    for tool in tools:
        if not tool.mcp_connection_id:
            hydrated.append(tool)
            continue
        connection = connections.get(tool.mcp_connection_id)
        if connection is None or (require_active and connection.status != "active"):
            continue
        hydrated.append(
            tool.model_copy(
                update={
                    "runner_tool_id": connection.runner_tool_id,
                    "mcp_connection": connection,
                }
            )
        )
    return hydrated


def fetch_tools() -> list[ToolDefinition]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, description, status, source, runner_tool_id, runner_name,
                       version, read_only, idempotent, parallel_safe, requires_approval,
                       side_effect_level, data_sensitivity, network_access, layer, category,
                       namespace, group_id, group_name, timeout_ms, input_schema_json,
                       scope_schema_json, scope_descriptions_json, manifest_hash, mcp_connection_id
                FROM tools
                ORDER BY layer, group_id, category, id
                """
            )
            tools = [row_to_tool_definition(row) for row in cursor.fetchall()]
    return hydrate_tool_connections(tools)


def fetch_tools_by_ids(tool_ids: set[str]) -> dict[str, ToolDefinition]:
    if not tool_ids:
        return {}
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, description, status, source, runner_tool_id, runner_name,
                       version, read_only, idempotent, parallel_safe, requires_approval,
                       side_effect_level, data_sensitivity, network_access, layer, category,
                       namespace, group_id, group_name, timeout_ms, input_schema_json,
                       scope_schema_json, scope_descriptions_json, manifest_hash, mcp_connection_id
                FROM tools
                WHERE id = ANY(%s) AND status = 'active'
                """,
                (list(tool_ids),),
            )
            tools = [row_to_tool_definition(row) for row in cursor.fetchall()]
    return {
        tool.tool_id: tool
        for tool in hydrate_tool_connections(tools, require_active=True)
    }


def fetch_tool_scope_schemas(tool_ids: set[str]) -> dict[str, dict[str, list[str]]]:
    return {tool_id: tool.scope_schema for tool_id, tool in fetch_tools_by_ids(tool_ids).items()}


def active_tool_ids(tool_ids: set[str]) -> set[str]:
    if not tool_ids:
        return set()
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
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
            return {row[0] for row in cursor.fetchall()}


def update_tool_status(tool_id: str, status: str) -> bool:
    if status not in {"active", "disabled"}:
        raise ValueError("status 只能是 active 或 disabled")
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE tools SET status = %s WHERE id = %s", (status, tool_id))
            return cursor.rowcount == 1


def user_is_active(user_id: str) -> bool:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT status = 'active' FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return bool(row and row[0])


def user_status(user_id: str) -> str | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else None


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user_session(token: str, user_id: str) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_sessions (token_hash, user_id)
                VALUES (%s, %s)
                """,
                (hash_session_token(token), user_id),
            )


def get_session_user(token: str) -> str | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.user_id
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id AND u.status = 'active'
                WHERE s.token_hash = %s AND s.revoked_at IS NULL
                """,
                (hash_session_token(token),),
            )
            row = cursor.fetchone()
            return row[0] if row else None


def revoke_user_session(token: str) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE user_sessions
                SET revoked_at = now()
                WHERE token_hash = %s AND revoked_at IS NULL
                """,
                (hash_session_token(token),),
            )


def remove_tool_everywhere(tool_id: str) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            # 清理已下线工具，避免旧配置继续进入运行态。
            cursor.execute("DELETE FROM role_tool_permissions WHERE tool_id = %s", (tool_id,))
            cursor.execute("DELETE FROM agent_tools WHERE tool_id = %s", (tool_id,))
            cursor.execute("DELETE FROM tools WHERE id = %s", (tool_id,))


def remove_role_everywhere(role_id: str) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM role_tool_permissions WHERE role_id = %s", (role_id,))
            cursor.execute("DELETE FROM user_roles WHERE role_id = %s", (role_id,))
            cursor.execute("DELETE FROM roles WHERE id = %s", (role_id,))


def upsert_role(
    role_id: str,
    name: str,
    description: str = "",
    status: str = "active",
    is_system: bool = False,
) -> None:
    if status not in {"active", "disabled"}:
        raise ValueError("status 只能是 active 或 disabled")
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO roles (id, name, description, status, is_system)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    is_system = roles.is_system OR EXCLUDED.is_system
                """,
                (role_id, name, description, status, is_system),
            )


def role_exists(role_id: str) -> bool:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM roles WHERE id = %s", (role_id,))
            return cursor.fetchone() is not None


def role_is_system(role_id: str) -> bool | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT is_system FROM roles WHERE id = %s", (role_id,))
            row = cursor.fetchone()
            return bool(row[0]) if row else None


def create_custom_role(role_id: str, name: str, description: str, status: str) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO roles (id, name, description, status, is_system)
                VALUES (%s, %s, %s, %s, FALSE)
                """,
                (role_id, name, description, status),
            )


def update_custom_role(role_id: str, name: str, description: str, status: str) -> bool:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE roles
                SET name = %s, description = %s, status = %s
                WHERE id = %s AND is_system = FALSE
                """,
                (name, description, status, role_id),
            )
            return cursor.rowcount == 1


def record_role_change_event(
    role_id: str, actor_id: str, event_type: str, detail: dict[str, object]
) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO role_change_events (role_id, actor_id, event_type, detail_json)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (role_id, actor_id, event_type, json.dumps(detail, ensure_ascii=False)),
            )


def replace_role_tool_permissions(
    role_id: str, permissions: dict[str, dict[str, list[str]]]
) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM role_tool_permissions WHERE role_id = %s", (role_id,))
            for tool_id, scope in permissions.items():
                normalized = normalize_scope(scope)
                cursor.execute(
                    """
                    INSERT INTO role_tool_permissions (role_id, tool_id, scope_json)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (role_id, tool_id, json.dumps(normalized, ensure_ascii=False)),
                )


def roles_exist(role_ids: set[str]) -> bool:
    if not role_ids:
        return True
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM roles WHERE id = ANY(%s)", (list(role_ids),))
            return {row[0] for row in cursor.fetchall()} == role_ids


def replace_user_roles(user_id: str, role_ids: set[str]) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            for role_id in role_ids:
                cursor.execute(
                    "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                    (user_id, role_id),
                )


def user_has_role(user_id: str, role_id: str) -> bool:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM user_roles WHERE user_id = %s AND role_id = %s",
                (user_id, role_id),
            )
            return cursor.fetchone() is not None


def get_user_roles(user_id: str) -> set[str]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT role_id FROM user_roles WHERE user_id = %s", (user_id,))
            return {row[0] for row in cursor.fetchall()}


def get_user_role_tool_scopes(user_id: str) -> dict[str, dict[str, list[str]]]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rtp.tool_id, rtp.scope_json
                FROM user_roles ur
                JOIN roles r ON r.id = ur.role_id AND r.status = 'active'
                JOIN role_tool_permissions rtp ON rtp.role_id = ur.role_id
                WHERE ur.user_id = %s
                """,
                (user_id,),
            )
            return merge_tool_scopes(cursor.fetchall())


def fetch_users_with_roles() -> list[AdminUserSummary]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.id, u.status,
                       COALESCE(array_agg(ur.role_id ORDER BY ur.role_id)
                                FILTER (WHERE ur.role_id IS NOT NULL), '{}')
                FROM users u
                LEFT JOIN user_roles ur ON ur.user_id = u.id
                GROUP BY u.id, u.status
                ORDER BY u.id
                """
            )
            return [
                AdminUserSummary(user_id=row[0], status=row[1], roles=list(row[2]))
                for row in cursor.fetchall()
            ]


def fetch_roles() -> list[RoleSummary]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.id, r.name, r.description, r.status, r.is_system,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object('tool_id', rtp.tool_id, 'scope', rtp.scope_json)
                           ) FILTER (WHERE rtp.tool_id IS NOT NULL),
                           '[]'::jsonb
                       )
                FROM roles r
                LEFT JOIN role_tool_permissions rtp ON rtp.role_id = r.id
                GROUP BY r.id, r.name, r.description, r.status, r.is_system
                ORDER BY r.id
                """
            )
            roles = []
            for row in cursor.fetchall():
                permissions = [
                    RolePermission(tool_id=item["tool_id"], scope=normalize_scope(item["scope"]))
                    for item in row[5]
                ]
                roles.append(
                    RoleSummary(
                        role_id=row[0],
                        name=row[1],
                        description=row[2],
                        status=row[3],
                        is_system=row[4],
                        permissions=permissions,
                    )
                )
            return roles


def save_agent(
    agent_id: str,
    request: AgentCreateRequest,
    creator_permissions: set[str],
    tool_scopes: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    scopes = tool_scopes or {str(tool_id): {} for tool_id in request.tools}
    model = normalize_agent_model(request.model)
    members = sorted(
        {
            member.strip()
            for member in request.members
            if member.strip() and member.strip() != request.owner_user_id
        }
    )
    if request.visibility == "public":
        members = []
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agents
                    (id, name, icon, visibility, owner_user_id, system_prompt, model)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    agent_id,
                    request.name,
                    request.icon,
                    request.visibility,
                    request.owner_user_id,
                    request.system_prompt,
                    model or None,
                ),
            )
            for member_id in members:
                # 成员账号可先占位，真正工具能力仍由角色 scope 决定。
                cursor.execute(
                    "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                    (member_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_members (agent_id, user_id, permission)
                    VALUES (%s, %s, 'run')
                    """,
                    (agent_id, member_id),
                )
            for tool_id, scope in scopes.items():
                cursor.execute(
                    """
                    INSERT INTO agent_tools (agent_id, tool_id, scope_json)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (agent_id, tool_id, json.dumps(scope, ensure_ascii=False)),
                )
            cursor.execute(
                """
                INSERT INTO agent_permission_snapshots
                (agent_id, snapshot_version, permission_json, created_by, reason)
                VALUES (%s, 1, %s::jsonb, %s, %s)
                """,
                (
                    agent_id,
                    json.dumps(sorted(creator_permissions)),
                    request.owner_user_id,
                    "agent_created",
                ),
            )
            for channel in set(request.channels):
                cursor.execute(
                    "INSERT INTO channel_bindings (agent_id, channel) VALUES (%s, %s)",
                    (agent_id, channel),
                )


def update_agent(
    agent_id: str,
    request: AgentUpdateRequest,
    owner_permissions: set[str],
    tool_scopes: dict[str, dict[str, list[str]]],
    actor_id: str,
) -> None:
    model = normalize_agent_model(request.model)
    members = sorted(
        {
            member.strip()
            for member in request.members
            if member.strip()
        }
    )
    if request.visibility == "public":
        members = []
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agents
                SET name = %s, icon = %s, visibility = %s,
                    system_prompt = %s, model = %s
                WHERE id = %s
                """,
                (
                    request.name,
                    request.icon,
                    request.visibility,
                    request.system_prompt,
                    model or None,
                    agent_id,
                ),
            )
            cursor.execute("DELETE FROM agent_members WHERE agent_id = %s", (agent_id,))
            for member_id in members:
                cursor.execute(
                    "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                    (member_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_members (agent_id, user_id, permission)
                    VALUES (%s, %s, 'run')
                    """,
                    (agent_id, member_id),
                )
            cursor.execute("DELETE FROM agent_tools WHERE agent_id = %s", (agent_id,))
            for tool_id, scope in tool_scopes.items():
                cursor.execute(
                    """
                    INSERT INTO agent_tools (agent_id, tool_id, scope_json)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (agent_id, tool_id, json.dumps(scope, ensure_ascii=False)),
                )
            cursor.execute("DELETE FROM channel_bindings WHERE agent_id = %s", (agent_id,))
            for channel in set(request.channels):
                cursor.execute(
                    "INSERT INTO channel_bindings (agent_id, channel) VALUES (%s, %s)",
                    (agent_id, channel),
                )
            cursor.execute(
                """
                INSERT INTO agent_permission_snapshots
                    (agent_id, snapshot_version, permission_json, created_by, reason)
                SELECT %s, COALESCE(MAX(snapshot_version), 0) + 1, %s::jsonb, %s, 'agent_updated'
                FROM agent_permission_snapshots
                WHERE agent_id = %s
                """,
                (
                    agent_id,
                    json.dumps(sorted(owner_permissions)),
                    actor_id,
                    agent_id,
                ),
            )


def update_agent_status(agent_id: str, enabled: bool) -> bool:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agents SET enabled = %s WHERE id = %s",
                (enabled, agent_id),
            )
            return cursor.rowcount == 1


def agent_is_enabled(agent_id: str) -> bool | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT enabled FROM agents WHERE id = %s", (agent_id,))
            row = cursor.fetchone()
            return bool(row[0]) if row else None


def load_agent(agent_id: str) -> dict[str, object] | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT owner_user_id, enabled, name, icon, system_prompt, model, visibility
                FROM agents WHERE id = %s
                """,
                (agent_id,),
            )
            agent = cursor.fetchone()
            if agent is None:
                return None
            cursor.execute(
                "SELECT tool_id, scope_json FROM agent_tools WHERE agent_id = %s AND enabled",
                (agent_id,),
            )
            tool_rows = cursor.fetchall()
            tool_scopes = {row[0]: normalize_scope(row[1]) for row in tool_rows}
            tools = set(tool_scopes)
            cursor.execute(
                """
                SELECT permission_json FROM agent_permission_snapshots
                WHERE agent_id = %s ORDER BY snapshot_version DESC LIMIT 1
                """,
                (agent_id,),
            )
            snapshot_row = cursor.fetchone()
            snapshot = snapshot_row[0] if snapshot_row else sorted(tools)
            cursor.execute(
                "SELECT channel FROM channel_bindings WHERE agent_id = %s", (agent_id,)
            )
            channels = {row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT user_id FROM agent_members WHERE agent_id = %s ORDER BY user_id",
                (agent_id,),
            )
            return {
                "owner_user_id": agent[0],
                "enabled": agent[1],
                "name": agent[2],
                "icon": agent[3],
                "system_prompt": agent[4],
                "model": agent[5],
                "visibility": agent[6],
                "members": {row[0] for row in cursor.fetchall()},
                "tools": tools,
                "tool_scopes": tool_scopes,
                "snapshot_tools": set(snapshot),
                "channels": channels,
            }


def fetch_agents(actor_id: str | None = None, is_admin: bool = False) -> list[AgentSummary]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            params: list[object] = []
            where_clause = ""
            if not is_admin:
                if actor_id:
                    where_clause = """
                    WHERE a.visibility = 'public'
                       OR a.owner_user_id = %s
                       OR EXISTS (
                           SELECT 1 FROM agent_members am
                           WHERE am.agent_id = a.id AND am.user_id = %s
                       )
                    """
                    params.extend([actor_id, actor_id])
                else:
                    where_clause = "WHERE a.visibility = 'public'"
            cursor.execute(
                f"""
                SELECT a.id::text, a.name, a.icon, a.owner_user_id, a.visibility, a.enabled,
                       COALESCE(
                           (
                               SELECT array_agg(at.tool_id ORDER BY at.tool_id)
                               FROM agent_tools at
                               WHERE at.agent_id = a.id AND at.enabled
                           ),
                           '{{}}'
                       ),
                       COALESCE(
                           (
                               SELECT array_agg(am.user_id ORDER BY am.user_id)
                               FROM agent_members am
                               WHERE am.agent_id = a.id
                           ),
                           '{{}}'
                       )
                FROM agents a
                {where_clause}
                ORDER BY a.created_at DESC
                LIMIT 100
                """,
                params,
            )
            return [
                AgentSummary(
                    agent_id=row[0],
                    name=row[1],
                    icon=row[2],
                    owner_user_id=row[3],
                    visibility=row[4],
                    enabled=row[5],
                    tools=sorted(row[6]),
                    members=sorted(row[7]),
                )
                for row in cursor.fetchall()
            ]


def next_release_version(agent_id: str) -> int:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_releases WHERE agent_id = %s",
                (agent_id,),
            )
            return cursor.fetchone()[0]


def save_agent_release(
    release_id: str,
    agent_id: str,
    version: int,
    config_json: dict[str, object],
    published_by: str,
) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_releases
                (id, agent_id, version, status, config_json, published_by)
                VALUES (%s, %s, %s, 'published', %s::jsonb, %s)
                """,
                (
                    release_id,
                    agent_id,
                    version,
                    json.dumps(config_json, ensure_ascii=False),
                    published_by,
                ),
            )


def revoke_agent_release(agent_id: str, release_id: str) -> bool:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_releases
                SET status = 'revoked', revoked_at = now()
                WHERE id = %s AND agent_id = %s AND status = 'published'
                """,
                (release_id, agent_id),
            )
            return cursor.rowcount == 1


def fetch_agent_release(agent_id: str, release_id: str) -> dict[str, object] | None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text, agent_id::text, version, status, config_json,
                       published_by, published_at::text, revoked_at::text
                FROM agent_releases
                WHERE id = %s AND agent_id = %s
                """,
                (release_id, agent_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "release_id": row[0],
                "agent_id": row[1],
                "version": row[2],
                "status": row[3],
                "config_json": row[4],
                "published_by": row[5],
                "published_at": row[6],
                "revoked_at": row[7],
            }


def fetch_agent_releases(agent_id: str) -> list[dict[str, object]]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text, agent_id::text, version, status, config_json,
                       published_by, published_at::text, revoked_at::text
                FROM agent_releases
                WHERE agent_id = %s
                ORDER BY version DESC
                LIMIT 50
                """,
                (agent_id,),
            )
            return [
                {
                    "release_id": row[0],
                    "agent_id": row[1],
                    "version": row[2],
                    "status": row[3],
                    "config_json": row[4],
                    "published_by": row[5],
                    "published_at": row[6],
                    "revoked_at": row[7],
                }
                for row in cursor.fetchall()
            ]


def fetch_runs() -> list[RunSummary]:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id::text, agent_id::text, actor_id, message, answer, status,
                       created_at::text
                FROM agent_runs
                ORDER BY created_at DESC
                LIMIT 100
                """
            )
            return [
                RunSummary(
                    run_id=row[0],
                    agent_id=row[1],
                    actor_id=row[2],
                    message=row[3],
                    answer=row[4],
                    status=row[5],
                    created_at=row[6],
                )
                for row in cursor.fetchall()
            ]


def record_permission_event(
    agent_id: str | None, user_id: str, event_type: str, detail: str
) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_permission_events (agent_id, user_id, event_type, detail)
                VALUES (%s, %s, %s, %s)
                """,
                (agent_id, user_id, event_type, detail),
            )


def record_tool_call(agent_id: str, tool_id: str, result: str) -> None:
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tool_call_logs (agent_id, tool_id, result)
                VALUES (%s, %s, %s)
                """,
                (agent_id, tool_id, result),
            )
