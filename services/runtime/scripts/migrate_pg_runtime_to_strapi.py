#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.db import connect
from aegora_runtime.strapi import StrapiClient, collection_endpoint


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy PG sessions/messages/feedback into Strapi.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    settings = load_settings(validate_secrets=True)
    client = StrapiClient(settings.strapi)
    rows = load_pg_runtime_rows(settings, args.limit)
    if args.dry_run:
        print_summary(rows, dry_run=True)
        return
    migrated = migrate_rows(client, settings, rows)
    print_summary(migrated, dry_run=False)


def load_pg_runtime_rows(settings, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            sessions = fetch_if_table_exists(
                cur,
                "chat_sessions",
                """
                SELECT session_id, user_id, title, status, metadata, created_at, updated_at
                FROM chat_sessions
                ORDER BY updated_at NULLS LAST, session_id
                """,
            )
            messages_sql = """
                SELECT id, session_id, user_id, trace_id, role, content, route, intent,
                       retrieved_faq_ids, tool_calls, observability, latency_ms, metadata, created_at
                FROM chat_messages
                WHERE role IN ('user', 'assistant')
                ORDER BY id
            """
            if limit:
                messages_sql += " LIMIT %s"
                messages = fetch_if_table_exists(cur, "chat_messages", messages_sql, (limit,))
            else:
                messages = fetch_if_table_exists(cur, "chat_messages", messages_sql)
            feedback = fetch_if_table_exists(
                cur,
                "message_feedback",
                """
                SELECT id, assistant_message_id, trace_id, session_id, user_id, rating,
                       reason, source, created_at, updated_at
                FROM message_feedback
                ORDER BY id
                """,
            )
    return {"sessions": sessions, "messages": messages, "feedback": feedback}


def fetch_if_table_exists(cur, table: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = %s
        ) AS exists
        """,
        (table,),
    )
    if not cur.fetchone()["exists"]:
        return []
    cur.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def migrate_rows(client: StrapiClient, settings, rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    session_endpoint = collection_endpoint(settings, "chat_sessions")
    message_endpoint = collection_endpoint(settings, "chat_messages")
    feedback_endpoint = collection_endpoint(settings, "message_feedback")

    session_ids = {str(row["session_id"]) for row in rows["sessions"]}
    for message in rows["messages"]:
        session_ids.add(str(message["session_id"]))
    for feedback in rows["feedback"]:
        session_ids.add(str(feedback["session_id"]))

    for session_id in sorted(session_ids):
        source = next((row for row in rows["sessions"] if str(row["session_id"]) == session_id), None)
        client.upsert(session_endpoint, "session_id", session_id, session_payload(session_id, source))

    for message in rows["messages"]:
        key = legacy_message_key(message["id"])
        client.upsert(message_endpoint, "message_key", key, message_payload(key, message))

    message_ids = {int(row["id"]) for row in rows["messages"]}
    for feedback in rows["feedback"]:
        if int(feedback["assistant_message_id"]) not in message_ids:
            continue
        key = legacy_message_key(feedback["assistant_message_id"])
        client.upsert(feedback_endpoint, "assistant_message_key", key, feedback_payload(key, feedback))
    return rows


def session_payload(session_id: str, row: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(row.get("metadata") or {}) if row else {}
    if row:
        metadata["legacy_pg"] = timestamps(row)
    return {
        "session_id": session_id,
        "user_id": str((row or {}).get("user_id") or f"session-owner:{session_id}"),
        "title": str((row or {}).get("title") or session_id)[:80],
        "source": "pg_migration",
        "metadata": metadata,
    }


def message_payload(key: str, row: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(row.get("metadata") or {})
    metadata["legacy_pg"] = {"id": int(row["id"]), **timestamps(row)}
    return {
        "message_key": key,
        "session_id": str(row["session_id"]),
        "user_id": str(row.get("user_id") or f"session-owner:{row['session_id']}"),
        "trace_id": str(row.get("trace_id") or row["session_id"]),
        "role": str(row["role"]),
        "content": str(row.get("content") or ""),
        "route": row.get("route"),
        "intent": row.get("intent") or {},
        "retrieved_faq_ids": row.get("retrieved_faq_ids") or [],
        "tool_calls": row.get("tool_calls") or [],
        "observability": row.get("observability") or [],
        "latency_ms": row.get("latency_ms"),
        "source": "pg_migration",
        "metadata": metadata,
    }


def feedback_payload(key: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "assistant_message_key": key,
        "trace_id": row.get("trace_id"),
        "session_id": str(row["session_id"]),
        "user_id": str(row["user_id"]),
        "rating": str(row["rating"]),
        "reason": row.get("reason"),
    }


def legacy_message_key(message_id: Any) -> str:
    return f"pg:{int(message_id)}"


def timestamps(row: dict[str, Any]) -> dict[str, str | None]:
    return {
        key: jsonable_datetime(row.get(key))
        for key in ["created_at", "updated_at"]
        if key in row
    }


def jsonable_datetime(value: Any) -> str | None:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value) if value is not None else None


def print_summary(rows: dict[str, list[dict[str, Any]]], *, dry_run: bool) -> None:
    print(
        "pg_runtime_to_strapi "
        f"dry_run={dry_run} "
        f"sessions={len(rows['sessions'])} "
        f"messages={len(rows['messages'])} "
        f"feedback={len(rows['feedback'])}"
    )


if __name__ == "__main__":
    main()
