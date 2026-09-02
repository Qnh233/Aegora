from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from aegora_runtime.config import AEGORA_ROOT


AGENT_ID = str(uuid5(NAMESPACE_URL, "agent-platform:aicoin-customer-support-agent"))
ROLE_ID = "aicoin_customer_tools"
DEFAULT_OWNER_USER_ID = "u_console"
DEFAULT_DATABASE_URL = "postgresql://aegora:aegora@127.0.0.1:55432/aegora"
DEFAULT_CHANNELS = ["debug", "http", "im", "oa", "web_console", "wecom"]
PLATFORM_BACKEND_PATH = AEGORA_ROOT / "apps" / "control-plane" / "backend"


@dataclass(frozen=True)
class SeedResult:
    agent_id: str
    release_id: str
    release_version: int
    owner_user_id: str
    role_id: str
    tool_ids: list[str]
    reused_release: bool


def database_url() -> str:
    return os.environ.get("AGENT_PLATFORM_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def owner_user_id() -> str:
    return os.environ.get("AICOIN_AGENT_OWNER_USER_ID") or DEFAULT_OWNER_USER_ID


def agent_model() -> str | None:
    model = os.environ.get("AICOIN_AGENT_MODEL") or os.environ.get("LLM_GATEWAY_CHAT_MODEL")
    return model.strip() if model and model.strip() else None


def aicoin_system_prompt() -> str:
    return """你是 AiCoin 客服 Planner Agent。只输出 JSON 对象。
允许 route: tool_call, faq_answer, clarify, handoff, chat。
可用工具以 available_tools 为准，常见能力：
- support.search_faq: 检索 AiCoin FAQ，参数 {"query": string, "top_k": number}。
- support.lookup_faq_detail: 按 FAQ ID 查询完整详情，参数 {"faq_id": number}。
- runtime.load_skill: 从授权经验库加载客服经验，参数 {"name": string, "reason": string}。
- support.record_handoff: 记录人工处理摘要，参数 {"reason": string, "summary": string}。
- runtime.save_session_memory: 保存本会话内用户明确表达的偏好或稳定事实，参数 {"key": string, "value": string}。

决策规则：
1. 产品事实、功能入口、价格、权益、链接和操作步骤若没有证据，应 route=tool_call 调用 support.search_faq。
2. skill_index 只是候选经验索引。需要经验正文时调用 runtime.load_skill；不得猜测未加载 Skill 的内容。
3. 当前适用 Skill 中“先、首先、必须、不要”等指令是硬约束。若要求先确认需求，而用户尚未提供，必须立即 route=clarify。
4. 已有 FAQ 结果后，只使用与当前问题直接相关的证据，不要机械汇总全部召回结果。
5. 生成 faq_answer 时，FAQ/工具结果决定事实边界，当前适用 Skill 决定回答结构、澄清顺序和下一步引导。
6. 证据不足或问题仍不明确，route=clarify。
7. 账号、订单、充值、投诉、风控、禁言、解封等需要人工核实的问题，route=tool_call 调用 support.record_handoff 或 route=handoff；不要承诺已经处理完成。
8. 身份、寒暄、感谢、当前对话历史、非产品普通交流，route=chat，不要调用 FAQ 检索。
9. 用户同时提出多个独立产品问题时，拆成多个 support.search_faq 调用并放入同一个 tool_calls 数组。
10. 只有 available_tools 中 read_only=true 且 parallel_safe=true 的工具才允许放入同一批并行调用；写工具单独调用。
11. route 为 faq_answer、clarify、handoff 或 chat 时，answer 必须是非空字符串；只有 tool_call 可以不返回 answer。
12. 不要假设存在未提供的经验或外部信息。"""


def tool_manifests() -> list[dict[str, Any]]:
    return [
        {
            "tool_id": "runtime.load_skill",
            "name": "加载客服经验",
            "description": "通用 Runtime 能力：在授权经验库内加载完整经验正文；只读，不直接提供事实证据。",
            "runner_tool_id": "local.load_skill",
            "runner_name": "load_skill",
            "layer": "runtime",
            "category": "knowledge",
            "namespace": "aicoin.customer_support",
            "group_id": "runtime.knowledge",
            "group_name": "Runtime 知识能力",
            "read_only": True,
            "idempotent": True,
            "parallel_safe": True,
            "requires_approval": False,
            "side_effect_level": "none",
            "data_sensitivity": "internal",
            "network_access": "none",
            "timeout_ms": 8000,
            "input_schema": object_schema(
                {
                    "name": {"type": "string", "description": "候选 Skill 名称"},
                    "reason": {"type": "string", "description": "加载该经验的原因"},
                },
                required=["name", "reason"],
            ),
            "scope_schema": {
                "actions": ["read"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "skill_library_ids": ["aicoin_customer_support"],
            },
            "scope_descriptions": {
                "actions": "允许的操作类型；read 表示只能读取经验内容。",
                "product_ids": "允许访问的产品范围。",
                "domains": "允许访问的业务领域范围。",
                "skill_library_ids": "允许访问的经验库。",
            },
            "scope": {
                "actions": ["read"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "skill_library_ids": ["aicoin_customer_support"],
            },
        },
        {
            "tool_id": "support.search_faq",
            "name": "FAQ 检索",
            "description": "客服领域能力：在授权 FAQ 集合中检索 AiCoin 产品知识，返回可用于回答的事实证据。",
            "runner_tool_id": "local.search_faq",
            "runner_name": "search_faq",
            "layer": "support",
            "category": "support_knowledge",
            "namespace": "aicoin.customer_support",
            "group_id": "aicoin.support_knowledge",
            "group_name": "AiCoin 客服知识",
            "read_only": True,
            "idempotent": True,
            "parallel_safe": True,
            "requires_approval": False,
            "side_effect_level": "none",
            "data_sensitivity": "internal",
            "network_access": "internal_only",
            "timeout_ms": 15000,
            "input_schema": object_schema(
                {
                    "query": {"type": "string", "description": "用户问题或独立检索问题"},
                    "top_k": {"type": "integer", "description": "返回候选数量", "minimum": 1, "maximum": 10},
                },
                required=["query"],
            ),
            "scope_schema": {
                "actions": ["search", "read"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "faq_collections": ["aicoin_faq"],
            },
            "scope_descriptions": {
                "actions": "search 表示允许向量检索，read 表示允许读取检索结果。",
                "product_ids": "允许检索的产品范围。",
                "domains": "允许检索的业务领域范围。",
                "faq_collections": "允许访问的 FAQ 知识集合。",
            },
            "scope": {
                "actions": ["search", "read"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "faq_collections": ["aicoin_faq"],
            },
        },
        {
            "tool_id": "support.lookup_faq_detail",
            "name": "FAQ 详情查询",
            "description": "客服领域能力：在授权 FAQ 集合中按 FAQ ID 读取完整详情。",
            "runner_tool_id": "local.lookup_faq_detail",
            "runner_name": "lookup_faq_detail",
            "layer": "support",
            "category": "support_knowledge",
            "namespace": "aicoin.customer_support",
            "group_id": "aicoin.support_knowledge",
            "group_name": "AiCoin 客服知识",
            "read_only": True,
            "idempotent": True,
            "parallel_safe": True,
            "requires_approval": False,
            "side_effect_level": "none",
            "data_sensitivity": "internal",
            "network_access": "internal_only",
            "timeout_ms": 8000,
            "input_schema": object_schema(
                {"faq_id": {"type": "integer", "description": "FAQ strapi_id"}},
                required=["faq_id"],
            ),
            "scope_schema": {
                "actions": ["read"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "faq_collections": ["aicoin_faq"],
            },
            "scope_descriptions": {
                "actions": "read 表示允许读取 FAQ 详情。",
                "product_ids": "允许读取的产品范围。",
                "domains": "允许读取的业务领域范围。",
                "faq_collections": "允许访问的 FAQ 知识集合。",
            },
            "scope": {
                "actions": ["read"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "faq_collections": ["aicoin_faq"],
            },
        },
        {
            "tool_id": "runtime.save_session_memory",
            "name": "保存会话记忆",
            "description": "通用 Runtime 能力：在授权 namespace 内保存本会话记忆；只保存用户明确表达的信息。",
            "runner_tool_id": "local.save_user_memory",
            "runner_name": "save_user_memory",
            "layer": "runtime",
            "category": "memory",
            "namespace": "aicoin.customer_support",
            "group_id": "runtime.memory",
            "group_name": "Runtime 记忆能力",
            "read_only": False,
            "idempotent": False,
            "parallel_safe": False,
            "requires_approval": False,
            "side_effect_level": "internal_write",
            "data_sensitivity": "internal",
            "network_access": "internal_only",
            "timeout_ms": 8000,
            "input_schema": object_schema(
                {
                    "key": {"type": "string", "description": "记忆键"},
                    "value": {"type": "string", "description": "记忆值"},
                },
                required=["key", "value"],
            ),
            "scope_schema": {
                "actions": ["write"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "memory_namespaces": ["session.aicoin.customer_support"],
            },
            "scope_descriptions": {
                "actions": "write 表示允许写入会话记忆。",
                "product_ids": "允许写入记忆关联的产品范围。",
                "domains": "允许写入记忆关联的业务领域范围。",
                "memory_namespaces": "允许写入的记忆命名空间。",
            },
            "scope": {
                "actions": ["write"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "memory_namespaces": ["session.aicoin.customer_support"],
            },
        },
        {
            "tool_id": "support.record_handoff",
            "name": "记录转人工",
            "description": "客服领域能力：记录需要人工处理的请求摘要；只写审计，不承诺已经处理完成。",
            "runner_tool_id": "local.record_handoff",
            "runner_name": "record_handoff",
            "layer": "support",
            "category": "support_escalation",
            "namespace": "aicoin.customer_support",
            "group_id": "aicoin.support_operations",
            "group_name": "AiCoin 客服作业",
            "read_only": False,
            "idempotent": False,
            "parallel_safe": False,
            "requires_approval": False,
            "side_effect_level": "internal_write",
            "data_sensitivity": "confidential",
            "network_access": "internal_only",
            "timeout_ms": 8000,
            "input_schema": object_schema(
                {
                    "reason": {"type": "string", "description": "转人工原因"},
                    "summary": {"type": "string", "description": "给人工客服的摘要"},
                },
                required=["reason", "summary"],
            ),
            "scope_schema": {
                "actions": ["write"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "handoff_queues": ["aicoin_customer_support"],
            },
            "scope_descriptions": {
                "actions": "write 表示允许写入转人工审计记录。",
                "product_ids": "允许转人工关联的产品范围。",
                "domains": "允许转人工关联的业务领域范围。",
                "handoff_queues": "允许写入的转人工队列。",
            },
            "scope": {
                "actions": ["write"],
                "product_ids": ["aicoin"],
                "domains": ["customer_support"],
                "handoff_queues": ["aicoin_customer_support"],
            },
        },
    ]


def object_schema(properties: dict[str, Any], *, required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def tool_definition(tool: dict[str, Any]) -> dict[str, Any]:
    definition = {
        "tool_id": tool["tool_id"],
        "name": tool["name"],
        "description": tool["description"],
        "status": "active",
        "source": "local",
        "runner_tool_id": tool["runner_tool_id"],
        "runner_name": tool["runner_name"],
        "version": "aicoin-local-v1",
        "read_only": tool["read_only"],
        "idempotent": tool["idempotent"],
        "parallel_safe": tool["parallel_safe"],
        "requires_approval": tool["requires_approval"],
        "side_effect_level": tool["side_effect_level"],
        "data_sensitivity": tool["data_sensitivity"],
        "network_access": tool["network_access"],
        "layer": tool["layer"],
        "category": tool["category"],
        "namespace": tool["namespace"],
        "group_id": tool["group_id"],
        "group_name": tool["group_name"],
        "timeout_ms": tool["timeout_ms"],
        "input_schema": tool["input_schema"],
        "scope_schema": tool["scope_schema"],
        "scope_descriptions": tool["scope_descriptions"],
        "manifest_hash": "",
    }
    definition["manifest_hash"] = manifest_hash(definition)
    return definition


def manifest_hash(definition: dict[str, Any]) -> str:
    payload = {key: value for key, value in definition.items() if key != "manifest_hash"}
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def release_config(agent_id: str, user_id: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    tool_defs = [tool_definition(tool) for tool in tools]
    role_tool_scopes = {tool["tool_id"]: tool["scope"] for tool in tools}
    return {
        "agent": {
            "id": agent_id,
            "name": "AiCoin 客服助手",
            "icon": "customer-service",
            "visibility": "private",
            "members": [],
            "owner_user_id": user_id,
            "system_prompt": aicoin_system_prompt(),
            "model": agent_model(),
            "channels": DEFAULT_CHANNELS,
            "enabled": True,
        },
        "tools": [
            {
                "tool_id": item["tool_id"],
                "runner_tool_id": item["runner_tool_id"],
                "runner_name": item["runner_name"],
                "name": item["name"],
                "description": item["description"],
                "source": item["source"],
                "version": item["version"],
                "read_only": item["read_only"],
                "idempotent": item["idempotent"],
                "parallel_safe": item["parallel_safe"],
                "requires_approval": item["requires_approval"],
                "side_effect_level": item["side_effect_level"],
                "data_sensitivity": item["data_sensitivity"],
                "network_access": item["network_access"],
                "layer": item["layer"],
                "category": item["category"],
                "namespace": item["namespace"],
                "group_id": item["group_id"],
                "group_name": item["group_name"],
                "timeout_ms": item["timeout_ms"],
                "input_schema": item["input_schema"],
                "scope_schema": item["scope_schema"],
                "scope_descriptions": item["scope_descriptions"],
                "scope": role_tool_scopes[item["tool_id"]],
                "manifest_hash": item["manifest_hash"],
            }
            for item in sorted(tool_defs, key=lambda row: row["tool_id"])
        ],
        "runtime_policy": {"release_token_enabled": False},
        "permission_snapshot": {
            "published_by": user_id,
            "published_by_roles": [ROLE_ID],
            "role_tool_scopes": role_tool_scopes,
        },
    }


def register_aicoin_platform_config() -> SeedResult:
    ensure_platform_schema()
    import psycopg

    user_id = owner_user_id()
    tools = tool_manifests()
    config = release_config(AGENT_ID, user_id, tools)
    tool_ids = sorted(tool["tool_id"] for tool in tools)
    with psycopg.connect(database_url()) as conn:
        with conn.cursor() as cur:
            ensure_agent_release_visibility_column(cur)
            disable_legacy_flat_tools(cur)
            backfill_aicoin_skill_library(cur)
            upsert_tools(cur, tools)
            upsert_role_and_owner(cur, user_id, tools)
            upsert_agent(cur, AGENT_ID, user_id, tools)
            release_id, version, reused = ensure_release(cur, AGENT_ID, user_id, config)
        conn.commit()
    return SeedResult(
        agent_id=AGENT_ID,
        release_id=release_id,
        release_version=version,
        owner_user_id=user_id,
        role_id=ROLE_ID,
        tool_ids=tool_ids,
        reused_release=reused,
    )


def ensure_platform_schema() -> None:
    backend_path = Path(os.environ.get("AGENT_PLATFORM_BACKEND_PATH", PLATFORM_BACKEND_PATH))
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    os.environ.setdefault("DATABASE_URL", database_url())
    from app import db as platform_db

    platform_db.ensure_schema()


def ensure_agent_release_visibility_column(cur: Any) -> None:
    cur.execute("ALTER TABLE agent_releases ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'private'")


def upsert_tools(cur: Any, tools: list[dict[str, Any]]) -> None:
    for tool in tools:
        item = tool_definition(tool)
        cur.execute(
            """
            INSERT INTO tools (
                id, name, description, status, source, runner_tool_id, runner_name,
                version, read_only, idempotent, parallel_safe, requires_approval,
                side_effect_level, data_sensitivity, network_access, layer, category,
                namespace, group_id, group_name, timeout_ms, input_schema_json,
                scope_schema_json, scope_descriptions_json, manifest_hash
            )
            VALUES (
                %(tool_id)s, %(name)s, %(description)s, %(status)s, %(source)s,
                %(runner_tool_id)s, %(runner_name)s, %(version)s, %(read_only)s,
                %(idempotent)s, %(parallel_safe)s, %(requires_approval)s,
                %(side_effect_level)s, %(data_sensitivity)s, %(network_access)s,
                %(layer)s, %(category)s, %(namespace)s, %(group_id)s, %(group_name)s,
                %(timeout_ms)s, %(input_schema)s::jsonb, %(scope_schema)s::jsonb,
                %(scope_descriptions)s::jsonb,
                %(manifest_hash)s
            )
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                status = EXCLUDED.status,
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
                manifest_hash = EXCLUDED.manifest_hash
            """,
            {
                **item,
                "input_schema": json.dumps(item["input_schema"], ensure_ascii=False),
                "scope_schema": json.dumps(item["scope_schema"], ensure_ascii=False),
                "scope_descriptions": json.dumps(item["scope_descriptions"], ensure_ascii=False),
            },
        )


def disable_legacy_flat_tools(cur: Any) -> None:
    cur.execute(
        """
        UPDATE tools
        SET status = 'disabled',
            layer = 'support',
            category = 'legacy',
            namespace = 'aicoin.customer_support',
            group_id = 'aicoin.legacy_flat',
            group_name = 'AiCoin 旧扁平工具'
        WHERE id = ANY(%s)
        """,
        (["load_skill", "search_faq", "lookup_faq_detail", "save_user_memory", "record_handoff"],),
    )


def backfill_aicoin_skill_library(cur: Any) -> None:
    cur.execute("SELECT to_regclass('public.skills')")
    if cur.fetchone()[0] is None:
        return
    cur.execute(
        """
        UPDATE skills
        SET metadata = metadata
            || jsonb_build_object(
                'skill_library_id', COALESCE(metadata->>'skill_library_id', metadata->>'library_id', 'aicoin_customer_support'),
                'skill_library_name', COALESCE(metadata->>'skill_library_name', 'AiCoin 客服经验库')
            )
        WHERE product_id = 'aicoin'
          AND status IN ('draft', 'active')
        """
    )


def upsert_role_and_owner(cur: Any, user_id: str, tools: list[dict[str, Any]]) -> None:
    cur.execute("INSERT INTO users (id, status) VALUES (%s, 'active') ON CONFLICT (id) DO UPDATE SET status = 'active'", (user_id,))
    cur.execute(
        """
        INSERT INTO roles (id, name, description, status, is_system)
        VALUES (%s, %s, %s, 'active', FALSE)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            status = 'active'
        """,
        (ROLE_ID, "AiCoin 客服工具权限", "允许运行 AiCoin 客服助手发布快照中的本地工具。"),
    )
    cur.execute("DELETE FROM role_tool_permissions WHERE role_id = %s", (ROLE_ID,))
    for tool in tools:
        cur.execute(
            """
            INSERT INTO role_tool_permissions (role_id, tool_id, scope_json)
            VALUES (%s, %s, %s::jsonb)
            """,
            (ROLE_ID, tool["tool_id"], json.dumps(tool["scope"], ensure_ascii=False)),
        )
    cur.execute(
        """
        INSERT INTO user_roles (user_id, role_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id, role_id) DO NOTHING
        """,
        (user_id, ROLE_ID),
    )


def upsert_agent(cur: Any, agent_id: str, user_id: str, tools: list[dict[str, Any]]) -> None:
    cur.execute(
        """
        INSERT INTO agents (id, name, icon, visibility, owner_user_id, system_prompt, model, enabled)
        VALUES (%s, %s, %s, 'private', %s, %s, %s, TRUE)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            icon = EXCLUDED.icon,
            visibility = EXCLUDED.visibility,
            owner_user_id = EXCLUDED.owner_user_id,
            system_prompt = EXCLUDED.system_prompt,
            model = EXCLUDED.model,
            enabled = TRUE
        """,
        (agent_id, "AiCoin 客服助手", "customer-service", user_id, aicoin_system_prompt(), agent_model()),
    )
    cur.execute("DELETE FROM agent_members WHERE agent_id = %s", (agent_id,))
    cur.execute("DELETE FROM agent_tools WHERE agent_id = %s", (agent_id,))
    for tool in tools:
        cur.execute(
            """
            INSERT INTO agent_tools (agent_id, tool_id, scope_json, enabled)
            VALUES (%s, %s, %s::jsonb, TRUE)
            """,
            (agent_id, tool["tool_id"], json.dumps(tool["scope"], ensure_ascii=False)),
        )
    cur.execute("DELETE FROM channel_bindings WHERE agent_id = %s", (agent_id,))
    for channel in DEFAULT_CHANNELS:
        cur.execute(
            "INSERT INTO channel_bindings (agent_id, channel) VALUES (%s, %s)",
            (agent_id, channel),
        )
    cur.execute(
        """
        INSERT INTO agent_permission_snapshots
            (agent_id, snapshot_version, permission_json, created_by, reason)
        VALUES (%s, 1, %s::jsonb, %s, 'aicoin_seed')
        ON CONFLICT (agent_id, snapshot_version) DO UPDATE SET
            permission_json = EXCLUDED.permission_json,
            created_by = EXCLUDED.created_by,
            reason = EXCLUDED.reason
        """,
        (agent_id, json.dumps([tool["tool_id"] for tool in tools], ensure_ascii=False), user_id),
    )


def ensure_release(cur: Any, agent_id: str, user_id: str, config: dict[str, Any]) -> tuple[str, int, bool]:
    config_text = stable_json(config)
    cur.execute(
        """
        SELECT id::text, version, config_json
        FROM agent_releases
        WHERE agent_id = %s AND status = 'published'
        ORDER BY version DESC
        """,
        (agent_id,),
    )
    releases = cur.fetchall()
    for release_id, version, existing_config in releases:
        if stable_json(existing_config) == config_text:
            return str(release_id), int(version), True
    next_version = (int(releases[0][1]) + 1) if releases else 1
    release_id = str(uuid5(NAMESPACE_URL, f"agent-platform:aicoin:{agent_id}:{next_version}:{sha256_text(config_text)}"))
    cur.execute(
        """
        INSERT INTO agent_releases (id, agent_id, version, status, visibility, config_json, published_by)
        VALUES (%s, %s, %s, 'published', %s, %s::jsonb, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            release_id,
            agent_id,
            next_version,
            str((config.get("agent") or {}).get("visibility") or "private"),
            config_text,
            user_id,
        ),
    )
    return release_id, next_version, False


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def result_payload(result: SeedResult) -> dict[str, Any]:
    return {
        "agent_id": result.agent_id,
        "release_id": result.release_id,
        "release_version": result.release_version,
        "owner_user_id": result.owner_user_id,
        "role_id": result.role_id,
        "tool_ids": result.tool_ids,
        "reused_release": result.reused_release,
    }
