from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from aegora_runtime.config import load_settings
from aegora_runtime.memory.models import MemoryScope, MemoryWrite
from aegora_runtime.memory.native_pg import NativePgMemoryProvider


pytestmark = pytest.mark.skipif(
    os.getenv("AEGORA_TEST_PG_INTEGRATION") != "1",
    reason="set AEGORA_TEST_PG_INTEGRATION=1 to run against an isolated PostgreSQL",
)


def test_native_pg_real_lifecycle_and_audit_events() -> None:
    settings = load_settings(env_path=None)
    provider = NativePgMemoryProvider(settings)
    scope = MemoryScope(
        user_id="pg-it-user",
        session_id="pg-it-s1",
        agent_id="agent-a",
        tenant_id="default",
        namespace="preferences",
        memory_scope="user_global",
    )
    _clean(settings, scope.user_id)

    v1 = provider.remember(
        scope=scope,
        memory=MemoryWrite(
            key="preferred_language",
            value="Python",
            source_agent_id="agent-a",
            source_session_id="pg-it-s1",
            source_trace_id="pg-it-create",
            source_kind="explicit",
            reason="integration_create",
        ),
    )
    v2 = provider.remember(
        scope=scope,
        memory=MemoryWrite(
            key="preferred_language",
            value="Go",
            source_agent_id="agent-a",
            source_session_id="pg-it-s2",
            source_trace_id="pg-it-update",
            source_kind="automatic",
            confidence=0.99,
            reason="integration_update",
        ),
    )

    recalled = provider.recall(scope=scope, query="language", limit=8)
    forgotten = provider.forget(
        scope=scope,
        key="preferred_language",
        reason="integration_forget",
        source_trace_id="pg-it-forget",
        source_kind="explicit",
    )
    provider.remember(
        scope=scope,
        memory=MemoryWrite(
            key="temporary_context",
            value="ephemeral",
            source_trace_id="pg-it-expire",
            source_kind="automatic",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            reason="integration_expire_seed",
        ),
    )
    expired_count = provider.expire_due(tenant_id="default", limit=100)

    assert (v1.version, v2.version) == (1, 2)
    assert v2.metadata["supersedes_id"] == int(v1.memory_id)
    assert [(item.key, item.content, item.version) for item in recalled] == [
        ("preferred_language", "Go", 2)
    ]
    assert forgotten is True
    assert expired_count >= 1

    with psycopg.connect(settings.postgres.dsn, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_key, status, version, source_kind
                FROM memory_items
                WHERE subject_user_id = %s
                ORDER BY id
                """,
                (scope.user_id,),
            )
            items = [
                (
                    row["memory_key"],
                    row["status"],
                    row["version"],
                    row["source_kind"],
                )
                for row in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT event_type, memory_key, reason
                FROM memory_events
                WHERE subject_user_id = %s
                ORDER BY id
                """,
                (scope.user_id,),
            )
            events = [
                (row["event_type"], row["memory_key"], row["reason"])
                for row in cur.fetchall()
            ]

    assert items == [
        ("preferred_language", "superseded", 1, "explicit"),
        ("preferred_language", "deleted", 2, "automatic"),
        ("temporary_context", "expired", 1, "automatic"),
    ]
    assert events == [
        ("created", "preferred_language", "integration_create"),
        ("superseded", "preferred_language", "integration_update"),
        ("updated", "preferred_language", "integration_update"),
        ("forgotten", "preferred_language", "integration_forget"),
        ("created", "temporary_context", "integration_expire_seed"),
        ("expired", "temporary_context", "valid_until_elapsed"),
    ]


def _clean(settings, user_id: str) -> None:
    with psycopg.connect(settings.postgres.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_events WHERE subject_user_id = %s", (user_id,))
            cur.execute("DELETE FROM memory_candidates WHERE subject_user_id = %s", (user_id,))
            cur.execute("DELETE FROM memory_items WHERE subject_user_id = %s", (user_id,))
            cur.execute("DELETE FROM user_memories WHERE user_id = %s", (user_id,))
        conn.commit()
