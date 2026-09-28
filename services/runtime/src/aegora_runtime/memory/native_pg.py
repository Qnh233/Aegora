from __future__ import annotations

import json
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite


class NativePgMemoryProvider:
    """Versioned PostgreSQL memory provider backed by memory_items."""

    name = "native_pg"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def recall(
        self,
        *,
        scope: MemoryScope,
        query: str,
        limit: int,
    ) -> list[MemoryItem]:
        del query  # Provider-neutral relevance reranking is handled by MemoryService.
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        tenant_id,
                        subject_user_id,
                        scope,
                        namespace,
                        agent_id,
                        memory_type,
                        memory_key,
                        content,
                        source_agent_id,
                        source_session_id,
                        source_trace_id,
                        source_kind,
                        confidence,
                        importance,
                        status,
                        version,
                        supersedes_id,
                        valid_from,
                        valid_until,
                        created_at,
                        updated_at
                    FROM memory_items
                    WHERE tenant_id = %s
                      AND subject_user_id = %s
                      AND status = 'active'
                      AND (valid_until IS NULL OR valid_until > now())
                      AND (
                          scope IN ('user_global', 'tenant_user')
                          OR (scope = 'user_agent' AND agent_id = %s)
                      )
                    ORDER BY importance DESC, updated_at DESC, id DESC
                    LIMIT %s
                    """,
                    (
                        scope.tenant_id or "default",
                        scope.user_id,
                        scope.agent_id,
                        max(limit, 1),
                    ),
                )
                rows = cur.fetchall()

        return [_row_to_item(dict(row)) for row in rows]

    def get_current(
        self,
        *,
        scope: MemoryScope,
        key: str,
    ) -> MemoryItem | None:
        key = str(key or "").strip()
        if not key:
            return None
        agent_id = scope.agent_id if scope.memory_scope == "user_agent" else None
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM memory_items
                    WHERE tenant_id = %s
                      AND subject_user_id = %s
                      AND scope = %s
                      AND namespace = %s
                      AND COALESCE(agent_id, '') = COALESCE(%s, '')
                      AND memory_key = %s
                      AND status = 'active'
                      AND (valid_until IS NULL OR valid_until > now())
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        scope.tenant_id or "default",
                        scope.user_id,
                        scope.memory_scope,
                        scope.namespace,
                        agent_id,
                        key,
                    ),
                )
                row = cur.fetchone()
        return _row_to_item(dict(row)) if row else None

    def remember(
        self,
        *,
        scope: MemoryScope,
        memory: MemoryWrite,
    ) -> MemoryItem:
        agent_id = scope.agent_id if scope.memory_scope == "user_agent" else None
        source_agent_id = memory.source_agent_id or scope.agent_id
        source_session_id = memory.source_session_id or scope.session_id
        tenant_id = scope.tenant_id or "default"

        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM memory_items
                    WHERE tenant_id = %s
                      AND subject_user_id = %s
                      AND scope = %s
                      AND namespace = %s
                      AND COALESCE(agent_id, '') = COALESCE(%s, '')
                      AND memory_key = %s
                      AND status = 'active'
                    FOR UPDATE
                    """,
                    (
                        tenant_id,
                        scope.user_id,
                        scope.memory_scope,
                        scope.namespace,
                        agent_id,
                        memory.key,
                    ),
                )
                current = cur.fetchone()

                if (
                    current
                    and str(current["content"]) == memory.value
                    and current.get("valid_until") == memory.expires_at
                ):
                    return _row_to_item(dict(current))

                version = int(current["version"]) + 1 if current else 1
                supersedes_id = int(current["id"]) if current else None
                if current:
                    cur.execute(
                        """
                        UPDATE memory_items
                        SET status = 'superseded',
                            valid_until = COALESCE(valid_until, now()),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (current["id"],),
                    )
                    _record_event(
                        cur,
                        tenant_id=tenant_id,
                        user_id=scope.user_id,
                        namespace=scope.namespace,
                        key=memory.key,
                        memory_id=int(current["id"]),
                        event_type="superseded",
                        source_kind=memory.source_kind,
                        source_agent_id=source_agent_id,
                        source_session_id=source_session_id,
                        source_trace_id=memory.source_trace_id,
                        reason=memory.reason or "memory_update",
                        detail={"next_version": version},
                    )

                cur.execute(
                    """
                    INSERT INTO memory_items (
                        tenant_id,
                        subject_user_id,
                        scope,
                        namespace,
                        agent_id,
                        memory_type,
                        memory_key,
                        content,
                        source_agent_id,
                        source_session_id,
                        source_trace_id,
                        source_kind,
                        confidence,
                        importance,
                        status,
                        version,
                        supersedes_id,
                        valid_until
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s
                    )
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        scope.user_id,
                        scope.memory_scope,
                        scope.namespace,
                        agent_id,
                        memory.memory_type,
                        memory.key,
                        memory.value,
                        source_agent_id,
                        source_session_id,
                        memory.source_trace_id,
                        memory.source_kind,
                        memory.confidence,
                        memory.importance,
                        version,
                        supersedes_id,
                        memory.expires_at,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("memory insert did not return a row")
                row_dict = dict(row)
                _record_event(
                    cur,
                    tenant_id=tenant_id,
                    user_id=scope.user_id,
                    namespace=scope.namespace,
                    key=memory.key,
                    memory_id=int(row_dict["id"]),
                    event_type="created" if supersedes_id is None else "updated",
                    source_kind=memory.source_kind,
                    source_agent_id=source_agent_id,
                    source_session_id=source_session_id,
                    source_trace_id=memory.source_trace_id,
                    reason=memory.reason or ("memory_create" if supersedes_id is None else "memory_update"),
                    detail={
                        "version": version,
                        "supersedes_id": supersedes_id,
                        "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
                    },
                )

                # Keep the old aggregate table usable during the migration window.
                if scope.memory_scope == "user_global" and scope.namespace == "preferences":
                    cur.execute(
                        """
                        INSERT INTO user_memories (user_id, facts, updated_at)
                        VALUES (%s, jsonb_build_object(%s::text, %s::text), now())
                        ON CONFLICT (user_id)
                        DO UPDATE SET
                            facts = user_memories.facts || jsonb_build_object(%s::text, %s::text),
                            updated_at = now()
                        """,
                        (
                            scope.user_id,
                            memory.key,
                            memory.value,
                            memory.key,
                            memory.value,
                        ),
                    )
            conn.commit()

        return _row_to_item(row_dict)

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
        reason: str | None = None,
        source_trace_id: str | None = None,
        source_kind: str = "explicit",
    ) -> bool:
        key = str(key or "").strip()
        if not key:
            return False
        agent_id = scope.agent_id if scope.memory_scope == "user_agent" else None
        tenant_id = scope.tenant_id or "default"
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_items
                    SET status = 'deleted',
                        valid_until = now(),
                        updated_at = now()
                    WHERE tenant_id = %s
                      AND subject_user_id = %s
                      AND scope = %s
                      AND namespace = %s
                      AND COALESCE(agent_id, '') = COALESCE(%s, '')
                      AND memory_key = %s
                      AND status = 'active'
                    RETURNING id, source_agent_id, source_session_id
                    """,
                    (
                        tenant_id,
                        scope.user_id,
                        scope.memory_scope,
                        scope.namespace,
                        agent_id,
                        key,
                    ),
                )
                row = cur.fetchone()
                changed = row is not None
                if changed:
                    _record_event(
                        cur,
                        tenant_id=tenant_id,
                        user_id=scope.user_id,
                        namespace=scope.namespace,
                        key=key,
                        memory_id=int(row["id"]),
                        event_type="forgotten",
                        source_kind=source_kind,
                        source_agent_id=scope.agent_id,
                        source_session_id=scope.session_id,
                        source_trace_id=source_trace_id,
                        reason=reason or "explicit_forget",
                        detail={},
                    )
                if changed and scope.memory_scope == "user_global" and scope.namespace == "preferences":
                    cur.execute(
                        """
                        UPDATE user_memories
                        SET facts = facts - %s::text, updated_at = now()
                        WHERE user_id = %s
                        """,
                        (key, scope.user_id),
                    )
            conn.commit()
        return changed

    def expire_due(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 500,
    ) -> int:
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH due AS (
                        SELECT id
                        FROM memory_items
                        WHERE status = 'active'
                          AND valid_until IS NOT NULL
                          AND valid_until <= now()
                          AND (%s::text IS NULL OR tenant_id = %s)
                        ORDER BY valid_until ASC, id ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE memory_items AS item
                    SET status = 'expired',
                        updated_at = now()
                    FROM due
                    WHERE item.id = due.id
                    RETURNING
                        item.id,
                        item.tenant_id,
                        item.subject_user_id,
                        item.namespace,
                        item.memory_key,
                        item.source_kind,
                        item.source_agent_id,
                        item.source_session_id,
                        item.source_trace_id
                    """,
                    (tenant_id, tenant_id, max(1, limit)),
                )
                rows = cur.fetchall()
                for row in rows:
                    _record_event(
                        cur,
                        tenant_id=str(row["tenant_id"]),
                        user_id=str(row["subject_user_id"]),
                        namespace=str(row["namespace"]),
                        key=str(row["memory_key"]),
                        memory_id=int(row["id"]),
                        event_type="expired",
                        source_kind=str(row.get("source_kind") or "legacy"),
                        source_agent_id=row.get("source_agent_id"),
                        source_session_id=row.get("source_session_id"),
                        source_trace_id=row.get("source_trace_id"),
                        reason="valid_until_elapsed",
                        detail={},
                    )
            conn.commit()
        return len(rows)


def _record_event(
    cur: Any,
    *,
    tenant_id: str,
    user_id: str,
    namespace: str,
    key: str,
    memory_id: int | None,
    event_type: str,
    source_kind: str | None,
    source_agent_id: str | None,
    source_session_id: str | None,
    source_trace_id: str | None,
    reason: str | None,
    detail: dict[str, Any],
    candidate_id: int | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO memory_events (
            tenant_id,
            subject_user_id,
            namespace,
            memory_key,
            memory_id,
            candidate_id,
            event_type,
            source_kind,
            source_agent_id,
            source_session_id,
            source_trace_id,
            reason,
            detail
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            tenant_id,
            user_id,
            namespace,
            key,
            memory_id,
            candidate_id,
            event_type,
            source_kind,
            source_agent_id,
            source_session_id,
            source_trace_id,
            reason,
            json.dumps(detail, ensure_ascii=False, default=str),
        ),
    )


def _row_to_item(row: dict[str, Any]) -> MemoryItem:
    return MemoryItem(
        memory_id=str(row["id"]),
        memory_type=str(row.get("memory_type") or "semantic"),
        key=str(row.get("memory_key") or ""),
        content=str(row.get("content") or ""),
        scope=str(row.get("scope") or "user_global"),
        namespace=str(row.get("namespace") or "preferences"),
        version=int(row.get("version") or 1),
        source_agent_id=(
            str(row["source_agent_id"]) if row.get("source_agent_id") not in (None, "") else None
        ),
        source_kind=str(row.get("source_kind") or "legacy"),
        metadata={
            key: value
            for key, value in {
                "tenant_id": row.get("tenant_id"),
                "user_id": row.get("subject_user_id"),
                "agent_id": row.get("agent_id"),
                "source_session_id": row.get("source_session_id"),
                "source_trace_id": row.get("source_trace_id"),
                "confidence": row.get("confidence"),
                "importance": row.get("importance"),
                "supersedes_id": row.get("supersedes_id"),
                "valid_from": row.get("valid_from"),
                "valid_until": row.get("valid_until"),
            }.items()
            if value is not None
        },
    )
