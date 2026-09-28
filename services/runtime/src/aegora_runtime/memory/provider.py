from __future__ import annotations

from typing import Protocol

from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite


class MemoryProvider(Protocol):
    def recall(
        self,
        *,
        scope: MemoryScope,
        query: str,
        limit: int,
    ) -> list[MemoryItem]: ...

    def get_current(
        self,
        *,
        scope: MemoryScope,
        key: str,
    ) -> MemoryItem | None: ...

    def remember(
        self,
        *,
        scope: MemoryScope,
        memory: MemoryWrite,
    ) -> MemoryItem: ...

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
        reason: str | None = None,
        source_trace_id: str | None = None,
        source_kind: str = "explicit",
    ) -> bool: ...

    def expire_due(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 500,
    ) -> int: ...
