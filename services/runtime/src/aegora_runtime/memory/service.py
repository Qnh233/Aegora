from __future__ import annotations

import logging

from aegora_runtime.config import Settings
from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite, MemoryWriteResult
from aegora_runtime.memory.native_pg import NativePgMemoryProvider
from aegora_runtime.memory.provider import MemoryProvider


LOGGER = logging.getLogger(__name__)


class MemoryService:
    """Runtime-facing memory facade. Provider failures must not break an agent turn."""

    def __init__(
        self,
        provider: MemoryProvider | None,
        *,
        recall_enabled: bool = True,
        write_enabled: bool = True,
        recall_limit: int = 8,
        max_item_chars: int = 400,
    ) -> None:
        self.provider = provider
        self.recall_enabled = recall_enabled
        self.write_enabled = write_enabled
        self.recall_limit = recall_limit
        self.max_item_chars = max_item_chars

    def recall(self, *, scope: MemoryScope, query: str) -> list[MemoryItem]:
        if not self.recall_enabled or self.provider is None:
            return []
        try:
            items = self.provider.recall(scope=scope, query=query, limit=self.recall_limit)
        except Exception:
            LOGGER.exception("memory recall failed; continuing without long-term memory")
            return []

        sanitized: list[MemoryItem] = []
        for item in items[: self.recall_limit]:
            content = item.content.strip()
            if not content:
                continue
            sanitized.append(
                MemoryItem(
                    memory_id=item.memory_id,
                    memory_type=item.memory_type,
                    key=item.key,
                    content=content[: self.max_item_chars],
                    metadata=dict(item.metadata),
                )
            )
        return sanitized

    def remember(self, *, scope: MemoryScope, memory: MemoryWrite) -> MemoryWriteResult:
        if not self.write_enabled or self.provider is None:
            return MemoryWriteResult(saved=False, reason="memory_write_disabled")
        try:
            item = self.provider.remember(scope=scope, memory=memory)
        except Exception:
            LOGGER.exception("memory write failed")
            return MemoryWriteResult(saved=False, reason="provider_error")
        return MemoryWriteResult(saved=True, item=item)

    def forget(self, *, scope: MemoryScope, key: str) -> bool:
        if not self.write_enabled or self.provider is None:
            return False
        try:
            return self.provider.forget(scope=scope, key=key)
        except Exception:
            LOGGER.exception("memory forget failed")
            return False


def build_memory_service(settings: Settings) -> MemoryService:
    provider: MemoryProvider | None = None
    if settings.memory.provider == "native_pg":
        provider = NativePgMemoryProvider(settings)
    else:  # Guarded by config validation; keep Runtime defensive.
        raise ValueError(f"unsupported memory provider: {settings.memory.provider}")
    return MemoryService(
        provider,
        recall_enabled=settings.memory.recall_enabled,
        write_enabled=True,
        recall_limit=settings.memory.recall_limit,
        max_item_chars=settings.memory.max_item_chars,
    )
