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
    ) -> bool: ...
