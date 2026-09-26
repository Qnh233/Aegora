from __future__ import annotations

import json
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite


class NativePgMemoryProvider:
    """Compatibility provider over the existing user_memories aggregate table."""

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
        del query  # P0/P1 performs structured user-scoped recall, not semantic ranking yet.
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT profile, facts, timeline, summary
                    FROM user_memories
                    WHERE user_id = %s
                    """,
                    (scope.user_id,),
                )
                row = cur.fetchone()

        if not row:
            return []

        items: list[MemoryItem] = []
        facts = _mapping(row.get("facts"))
        profile = _mapping(row.get("profile"))
        timeline = row.get("timeline") if isinstance(row.get("timeline"), list) else []
        summary = row.get("summary")

        for key, value in facts.items():
            items.append(_item(scope, "semantic", str(key), value))
        for key, value in profile.items():
            items.append(_item(scope, "profile", str(key), value))
        if summary not in (None, ""):
            items.append(_item(scope, "summary", "summary", summary))
        for index, value in enumerate(reversed(timeline)):
            items.append(_item(scope, "episodic", f"timeline:{index}", value))

        return items[: max(0, limit)]

    def remember(
        self,
        *,
        scope: MemoryScope,
        memory: MemoryWrite,
    ) -> MemoryItem:
        if memory.memory_type != "semantic":
            raise ValueError("native_pg compatibility provider currently writes semantic memories only")

        with connect(self.settings) as conn:
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
                    (scope.user_id, memory.key, memory.value, memory.key, memory.value),
                )
            conn.commit()

        return MemoryItem(
            memory_id=f"semantic:{scope.user_id}:{memory.key}",
            memory_type="semantic",
            key=memory.key,
            content=memory.value,
            metadata=_scope_metadata(scope),
        )

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
    ) -> bool:
        key = str(key or "").strip()
        if not key:
            return False
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_memories
                    SET facts = facts - %s::text, updated_at = now()
                    WHERE user_id = %s AND facts ? %s::text
                    """,
                    (key, scope.user_id, key),
                )
                changed = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return changed


def _item(scope: MemoryScope, memory_type: str, key: str, value: Any) -> MemoryItem:
    if isinstance(value, str):
        content = value
    else:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return MemoryItem(
        memory_id=f"{memory_type}:{scope.user_id}:{key}",
        memory_type=memory_type,
        key=key,
        content=content,
        metadata=_scope_metadata(scope),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _scope_metadata(scope: MemoryScope) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "tenant_id": scope.tenant_id,
            "user_id": scope.user_id,
            "agent_id": scope.agent_id,
            "session_id": scope.session_id,
            "namespace": scope.namespace,
        }.items()
        if value not in (None, "")
    }
