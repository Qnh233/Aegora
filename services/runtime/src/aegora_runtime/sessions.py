from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.strapi import client_from_settings, collection_endpoint


DEFAULT_SESSION_ID = "local-session"
SESSION_USER_PREFIX = "session-owner:"


def normalize_session_id(session_id: str | None) -> str:
    value = str(session_id or "").strip()
    return value or DEFAULT_SESSION_ID


def derive_user_id_from_session(session_id: str | None) -> str:
    return f"{SESSION_USER_PREFIX}{normalize_session_id(session_id)}"


def session_user_id(session_id: str | None, user_id: str | None) -> str:
    value = str(user_id or "").strip()
    return value or derive_user_id_from_session(session_id)


@contextmanager
def session_execution_lock(settings: Settings, session_id: str) -> Iterator[None]:
    """PG only coordinates concurrent turns; conversation content lives in Strapi."""
    session_id = normalize_session_id(session_id)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (session_id,))
        conn.commit()
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (session_id,))
            conn.commit()


def save_chat_turn(
    settings: Settings,
    *,
    session_id: str,
    user_id: str | None,
    user_message: str,
    assistant_message: str,
    result: dict[str, Any],
    source: str = "gradio",
    request_metadata: dict[str, Any] | None = None,
) -> str:
    session_id = normalize_session_id(session_id)
    user_id = session_user_id(session_id, user_id)
    current_trace_id = trace_id(result, session_id)
    client = client_from_settings(settings)
    sessions_endpoint = collection_endpoint(settings, "chat_sessions")
    messages_endpoint = collection_endpoint(settings, "chat_messages")
    client.upsert(
        sessions_endpoint,
        "session_id",
        session_id,
        {
            "session_id": session_id,
            "user_id": user_id,
            "title": user_message.strip()[:80] or "未命名会话",
            "source": source,
            "metadata": request_metadata or {},
        },
    )
    client.upsert(
        messages_endpoint,
        "message_key",
        f"{current_trace_id}:user",
        {
            "message_key": f"{current_trace_id}:user",
            "session_id": session_id,
            "user_id": user_id,
            "trace_id": current_trace_id,
            "role": "user",
            "content": user_message,
            "source": source,
            "metadata": {"request_metadata": request_metadata or {}},
        },
    )
    assistant_key = f"{current_trace_id}:assistant"
    client.upsert(
        messages_endpoint,
        "message_key",
        assistant_key,
        {
            "message_key": assistant_key,
            "session_id": session_id,
            "user_id": user_id,
            "trace_id": current_trace_id,
            "role": "assistant",
            "content": assistant_message,
            "route": result.get("route"),
            "intent": result.get("intent") or {},
            "retrieved_faq_ids": retrieved_faq_ids(result),
            "tool_calls": result.get("tool_observations") or [],
            "observability": result.get("observability") or [],
            "latency_ms": latency_ms(result),
            "source": source,
            "metadata": {
                "status": result.get("status"),
                "loop_mode": result.get("loop_mode"),
                "loop_count": result.get("loop_count"),
                "self_check": result.get("self_check") or {},
                "decision": result.get("decision") or {},
                "injected_skill_ids": [item.get("id") for item in result.get("skills") or []],
                "model_usage": result.get("model_usage") or {},
                "instance_id": settings.observability.instance_id,
                "request_metadata": request_metadata or {},
            },
        },
    )
    return assistant_key


def load_recent_conversation(
    settings: Settings,
    session_id: str,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = load_session_messages(settings, session_id, limit=limit)
    history = [{"role": row["role"], "content": row["content"]} for row in rows]
    assistant_ids = [str(row["message_key"]) for row in rows if row["role"] == "assistant"]
    return history, assistant_ids


def load_session_messages(settings: Settings, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    session_id = normalize_session_id(session_id)
    client = client_from_settings(settings)
    rows = client.list(
        collection_endpoint(settings, "chat_messages"),
        filters={"filters[session_id][$eq]": session_id},
        sort="createdAt:desc",
        page_size=max(1, min(limit, 100)),
        max_items=limit,
    )
    visible = [
        row
        for row in rows
        if row.get("role") in {"user", "assistant"} and row.get("message_key") and row.get("content") is not None
    ][:limit]
    return list(reversed(visible))


def load_session_user_messages(
    settings: Settings,
    session_id: str,
    user_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session_id = normalize_session_id(session_id)
    user_id = session_user_id(session_id, user_id)
    client = client_from_settings(settings)
    rows = client.list(
        collection_endpoint(settings, "chat_messages"),
        filters={
            "filters[session_id][$eq]": session_id,
            "filters[user_id][$eq]": user_id,
        },
        sort="createdAt:desc",
        page_size=max(1, min(limit, 100)),
        max_items=limit,
    )
    visible = [
        row
        for row in rows
        if row.get("role") in {"user", "assistant"} and row.get("message_key") and row.get("content") is not None
    ][:limit]
    return list(reversed(visible))


def load_session_context_views(
    settings: Settings,
    session_id: str,
    user_id: str | None,
    *,
    session_limit: int = 8,
    user_limit: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    session_rows = load_session_messages(settings, session_id, limit=session_limit)
    user_rows = load_session_user_messages(
        settings,
        session_id,
        session_user_id(session_id, user_id),
        limit=user_limit + session_limit,
    )
    seen = {str(row.get("message_key")) for row in session_rows if row.get("message_key")}
    session_context = [context_item_from_message(row) for row in session_rows]
    session_user_context = [
        context_item_from_message(row)
        for row in user_rows
        if row.get("message_key") and str(row["message_key"]) not in seen
    ]
    return {
        "session_context": session_context,
        "session_user_context": session_user_context[-user_limit:],
    }


def load_assistant_metadata(settings: Settings, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return [
        row.get("metadata") or {}
        for row in load_session_messages(settings, session_id, limit)
        if row.get("role") == "assistant" and isinstance(row.get("metadata"), dict)
    ]


def load_recent_messages(settings: Settings, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return load_recent_conversation(settings, session_id, limit)[0]


def load_recent_assistant_message_ids(settings: Settings, session_id: str, limit: int = 20) -> list[str]:
    return load_recent_conversation(settings, session_id, limit)[1]


def find_assistant_message_by_request_metadata(
    settings: Settings,
    *,
    session_id: str,
    key: str,
    value: str,
) -> str | None:
    if not key or not value:
        return None
    for row in reversed(load_session_messages(settings, session_id, limit=100)):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        request_metadata = metadata.get("request_metadata") if isinstance(metadata.get("request_metadata"), dict) else {}
        if row.get("role") == "assistant" and request_metadata.get(key) == value:
            return str(row["message_key"])
    return None


def save_message_feedback(
    settings: Settings,
    *,
    assistant_message_id: str | int,
    session_id: str,
    user_id: str,
    rating: str,
    reason: str | None = None,
) -> dict[str, Any]:
    if rating not in {"positive", "negative"}:
        raise ValueError("rating must be positive or negative")
    session_id = normalize_session_id(session_id)
    user_id = session_user_id(session_id, user_id)
    assistant_key = str(assistant_message_id)
    client = client_from_settings(settings)
    message = client.find_one(collection_endpoint(settings, "chat_messages"), "message_key", assistant_key)
    if not message or message.get("session_id") != session_id or message.get("role") != "assistant":
        raise ValueError("assistant message does not belong to this session")
    return client.upsert(
        collection_endpoint(settings, "message_feedback"),
        "assistant_message_key",
        assistant_key,
        {
            "assistant_message_key": assistant_key,
            "trace_id": message.get("trace_id"),
            "session_id": session_id,
            "user_id": user_id,
            "rating": rating,
            "reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
        },
    )


def retrieved_faq_ids(result: dict[str, Any]) -> list[int]:
    return [int(item["faq_id"]) for item in result.get("retrieved_faqs") or [] if item.get("faq_id") is not None]


def latency_ms(result: dict[str, Any]) -> int | None:
    value = result.get("total_latency_ms")
    return int(float(value)) if value is not None else None


def trace_id(result: dict[str, Any], session_id: str) -> str:
    return str(result.get("trace_id") or session_id)


def context_item_from_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_key": str(row.get("message_key") or ""),
        "role": row.get("role"),
        "content": row.get("content"),
        "user_id": row.get("user_id"),
        "trace_id": row.get("trace_id"),
    }
