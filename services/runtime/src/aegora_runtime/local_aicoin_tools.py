from __future__ import annotations

import json
from typing import Any, Callable

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.registry import LocalTool, registry as runner_registry
from aegora_runtime.sessions import derive_user_id_from_session
from aegora_runtime.skills import load_skill_by_name
from aegora_runtime.tools import RuntimeToolExecutor, ToolLifecycleHooks, ToolMetadata, ToolSpec


SearchFaqHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
AICOIN_LOCAL_TOOL_IDS = [
    "load_skill",
    "search_faq",
    "lookup_faq_detail",
    "save_user_memory",
    "record_handoff",
]


def build_aicoin_tool_executor(
    settings: Settings,
    search_faq_handler: SearchFaqHandler | None = None,
) -> RuntimeToolExecutor:
    hooks = ToolLifecycleHooks()
    hooks.after_call.append(lambda event: write_tool_log(settings, event))
    hooks.on_error.append(lambda event: write_tool_log(settings, event))
    executor = RuntimeToolExecutor(hooks)
    runtime = build_aicoin_tool_runtime(settings, search_faq_handler)
    for tool_id in AICOIN_LOCAL_TOOL_IDS:
        tool = runner_registry.get_tool(f"local.{tool_id}")
        executor.register(tool_spec_from_local_tool(tool, runtime))
    return executor


def build_aicoin_tool_runtime(
    settings: Settings,
    search_faq_handler: SearchFaqHandler | None = None,
) -> dict[str, Any]:
    return {
        "settings": settings,
        "search_faq_handler": search_faq_handler or search_faq_unavailable,
    }


def tool_spec_from_local_tool(tool: LocalTool, runtime: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=tool.tool_id,
        description=tool.description,
        input_schema=tool.input_schema,
        # 旧 AiCoin 本地工具也复用统一执行层，真实实现仍是 @registry.tool handler。
        handler=lambda args, state, local_tool=tool: local_tool.handler(
            args,
            {**state, "_tool_runtime": runtime},
        ),
        max_retries=tool.max_retries,
        retry_delay_seconds=tool.retry_delay_seconds,
        metadata=tool.metadata,
    )


def runtime_settings(state: dict[str, Any]) -> Settings:
    runtime = state.get("_tool_runtime") if isinstance(state.get("_tool_runtime"), dict) else {}
    settings = runtime.get("settings") if isinstance(runtime, dict) else None
    if not isinstance(settings, Settings):
        raise RuntimeError("工具运行时缺少 settings")
    return settings


def runtime_search_faq_handler(state: dict[str, Any]) -> SearchFaqHandler:
    runtime = state.get("_tool_runtime") if isinstance(state.get("_tool_runtime"), dict) else {}
    handler = runtime.get("search_faq_handler") if isinstance(runtime, dict) else None
    return handler if callable(handler) else search_faq_unavailable


@runner_registry.tool(
    tool_id="load_skill",
    runner_tool_id="local.load_skill",
    description="按候选 Skill 索引加载完整经验正文；仅在当前问题确实需要该经验时调用。",
    input_schema={"name": str, "reason": str},
    metadata=ToolMetadata(read_only=True, parallel_safe=True, idempotent=True, side_effects="none"),
)
def load_skill_runner_tool(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return load_skill(runtime_settings(state), state, args["name"], args["reason"])


@runner_registry.tool(
    tool_id="search_faq",
    runner_tool_id="local.search_faq",
    description="使用 FAQ RAG 检索产品知识库，返回候选 FAQ 证据。",
    input_schema={"query": str},
    metadata=ToolMetadata(read_only=True, parallel_safe=True, idempotent=True, side_effects="none"),
)
def search_faq_runner_tool(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return runtime_search_faq_handler(state)(args, state)


@runner_registry.tool(
    tool_id="lookup_faq_detail",
    runner_tool_id="local.lookup_faq_detail",
    description="按 FAQ ID 查询完整 FAQ 详情。",
    input_schema={"faq_id": int},
    metadata=ToolMetadata(read_only=True, parallel_safe=True, idempotent=True, side_effects="none"),
)
def lookup_faq_detail_runner_tool(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return lookup_faq_detail(runtime_settings(state), int(args["faq_id"]))


@runner_registry.tool(
    tool_id="save_user_memory",
    runner_tool_id="local.save_user_memory",
    description="保存本会话偏好或稳定事实，只保存用户明确表达的信息。",
    input_schema={"key": str, "value": str},
    max_retries=2,
    retry_delay_seconds=0.1,
    metadata=ToolMetadata(side_effects="memory_write"),
)
def save_user_memory_runner_tool(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return save_user_memory(runtime_settings(state), state, args["key"], args["value"])


@runner_registry.tool(
    tool_id="record_handoff",
    runner_tool_id="local.record_handoff",
    description="记录需要人工处理的请求摘要，不承诺已经处理完成。",
    input_schema={"reason": str, "summary": str},
    max_retries=2,
    retry_delay_seconds=0.1,
    metadata=ToolMetadata(side_effects="audit_write"),
)
def record_handoff_runner_tool(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return record_handoff(runtime_settings(state), state, args["reason"], args["summary"])


def load_skill(settings: Settings, state: dict[str, Any], name: str, reason: str) -> dict[str, Any]:
    request = state.get("request")
    index = (state.get("context") or {}).get("skill_index") or []
    skills = state.get("skills") or []
    scope = current_tool_scope(state)
    product_id = first_scope_value(scope, "product_ids") or str(getattr(request, "product_id", settings.app.product_id))
    domain_hint = first_scope_value(scope, "domains") or getattr(request, "domain_hint", None)
    result = load_skill_by_name(
        name.strip(),
        query=str(getattr(request, "query", "") or ""),
        product_id=product_id,
        domain_hint=domain_hint,
        allowed_names={str(item.get("name")) for item in index if item.get("name")} | {str(item.get("name")) for item in skills if item.get("name")},
        already_loaded_ids={int(item["id"]) for item in skills if item.get("id") is not None},
        settings=settings,
        allowed_scope=scope,
    )
    return {**result, "requested_reason": reason.strip()}


def current_tool_scope(state: dict[str, Any]) -> dict[str, object]:
    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    tool_scopes = context.get("tool_scopes") if isinstance(context.get("tool_scopes"), dict) else {}
    current_tool = state.get("_current_tool") or state.get("current_tool")
    tool_id = current_tool.get("tool_id") if isinstance(current_tool, dict) else None
    candidates = [tool_id, "runtime.load_skill", "load_skill"]
    candidates.extend(key for key in tool_scopes if isinstance(key, str) and key.endswith(".load_skill"))
    for candidate in candidates:
        if isinstance(candidate, str) and isinstance(tool_scopes.get(candidate), dict):
            return dict(tool_scopes[candidate])
    return {}


def first_scope_value(scope: dict[str, object], key: str) -> str | None:
    values = scope.get(key)
    if isinstance(values, list):
        return next((item for item in values if isinstance(item, str) and item.strip()), None)
    return None


def search_faq_unavailable(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {"results": [], "reason": "search_faq_handler_not_configured"}


def lookup_faq_detail(settings: Settings, faq_id: int) -> dict[str, Any]:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    f.strapi_id AS faq_id,
                    f.title,
                    f.faq,
                    f.response,
                    f.response_pic_app_url,
                    f.response_pic_pc_url,
                    f.keywords,
                    c.name AS raw_category,
                    c.project_category
                FROM faqs f
                JOIN faq_categories c ON c.id = f.category_id
                WHERE f.deleted_at IS NULL AND f.strapi_id = %s
                """,
                (faq_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"found": False, "faq_id": faq_id}
    return {"found": True, **dict(row)}


def save_user_memory(settings: Settings, state: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    session_id = request_value(state, "session_id")
    if not session_id:
        return {"saved": False, "reason": "missing_session_id"}
    user_id = derive_user_id_from_session(session_id)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_memories (user_id, facts, updated_at)
                VALUES (%s, jsonb_build_object(%s::text, %s::text), now())
                ON CONFLICT (user_id)
                DO UPDATE SET
                    facts = user_memories.facts || jsonb_build_object(%s::text, %s::text),
                    updated_at = now()
                """,
                (user_id, key, value, key, value),
            )
        conn.commit()
    return {"saved": True, "key": key, "scope": "session", "session_id": session_id}


def record_handoff(settings: Settings, state: dict[str, Any], reason: str, summary: str) -> dict[str, Any]:
    trace_id = trace_id_from_state(state)
    request = state.get("request")
    payload = {
        "reason": reason,
        "summary": summary,
        "query": getattr(request, "query", None),
        "retrieved_faq_ids": [item.get("faq_id") for item in state.get("retrieved_faqs") or []],
    }
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs (trace_id, session_id, user_id, event_type, node_name, payload)
                VALUES (%s, %s, %s, 'handoff_requested', 'record_handoff', %s::jsonb)
                """,
                (
                    trace_id,
                    request_value(state, "session_id"),
                    request_value(state, "user_id"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        conn.commit()
    return {"recorded": True, "trace_id": trace_id}


def write_tool_log(settings: Settings, event: dict[str, Any]) -> None:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tool_logs (
                    trace_id,
                    session_id,
                    user_id,
                    tool_name,
                    input,
                    output,
                    status,
                    error_code,
                    error_message,
                    latency_ms
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    event.get("trace_id") or "unknown",
                    event.get("session_id"),
                    event.get("user_id"),
                    event["tool_name"],
                    json.dumps(
                        {
                            "call_id": event.get("call_id"),
                            "args": event.get("args") or {},
                            "tool_metadata": event.get("tool_metadata") or {},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(event.get("output"), ensure_ascii=False) if event.get("output") is not None else None,
                    event.get("status") or "unknown",
                    event.get("error_type"),
                    event.get("error"),
                    int(event.get("latency_ms") or 0),
                ),
            )
        conn.commit()


def request_value(state: dict[str, Any], name: str) -> str | None:
    request = state.get("request")
    value = getattr(request, name, None)
    return str(value) if value else None


def trace_id_from_state(state: dict[str, Any]) -> str:
    return request_value(state, "trace_id") or request_value(state, "session_id") or request_value(state, "user_id") or "unknown"
