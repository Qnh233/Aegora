from __future__ import annotations

import logging
import re

from aegora_runtime.config import Settings
from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite, MemoryWriteResult
from aegora_runtime.memory.native_pg import NativePgMemoryProvider
from aegora_runtime.memory.openviking import OpenVikingMemoryProvider
from aegora_runtime.memory.policy import MemoryGovernance, filter_recalled_items
from aegora_runtime.memory.provider import MemoryProvider


LOGGER = logging.getLogger(__name__)
_CJK_RUN = re.compile(r"[㐀-鿿]+")
_WORD = re.compile(r"[a-z0-9_]{2,}")


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

    def recall(
        self,
        *,
        scope: MemoryScope,
        query: str,
        governance: MemoryGovernance | None = None,
    ) -> list[MemoryItem]:
        if not self.recall_enabled or self.provider is None:
            return []
        try:
            items = self.provider.recall(
                scope=scope,
                query=query,
                limit=max(self.recall_limit * 4, self.recall_limit),
            )
            if governance is not None:
                items = filter_recalled_items(items, governance)
        except Exception:
            LOGGER.exception("memory recall failed; continuing without long-term memory")
            return []

        ranked = rank_memory_items(query, items)
        sanitized: list[MemoryItem] = []
        for item in ranked[: self.recall_limit]:
            content = item.content.strip()
            if not content:
                continue
            sanitized.append(
                MemoryItem(
                    memory_id=item.memory_id,
                    memory_type=item.memory_type,
                    key=item.key,
                    content=content[: self.max_item_chars],
                    scope=item.scope,
                    namespace=item.namespace,
                    version=item.version,
                    source_agent_id=item.source_agent_id,
                    source_kind=item.source_kind,
                    metadata=dict(item.metadata),
                )
            )
        return sanitized

    def get_current(self, *, scope: MemoryScope, key: str) -> MemoryItem | None:
        if self.provider is None:
            return None
        try:
            return self.provider.get_current(scope=scope, key=key)
        except Exception:
            LOGGER.exception("memory current lookup failed")
            return None

    def remember(self, *, scope: MemoryScope, memory: MemoryWrite) -> MemoryWriteResult:
        if not self.write_enabled or self.provider is None:
            return MemoryWriteResult(saved=False, reason="memory_write_disabled")
        try:
            item = self.provider.remember(scope=scope, memory=memory)
        except Exception:
            LOGGER.exception("memory write failed")
            return MemoryWriteResult(saved=False, reason="provider_error")
        return MemoryWriteResult(saved=True, item=item)

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
        reason: str | None = None,
        source_trace_id: str | None = None,
        source_kind: str = "explicit",
    ) -> bool:
        if not self.write_enabled or self.provider is None:
            return False
        try:
            return self.provider.forget(
                scope=scope,
                key=key,
                reason=reason,
                source_trace_id=source_trace_id,
                source_kind=source_kind,
            )
        except Exception:
            LOGGER.exception("memory forget failed")
            return False

    def expire_due(self, *, tenant_id: str | None = None, limit: int = 500) -> int:
        if not self.write_enabled or self.provider is None:
            return 0
        try:
            return self.provider.expire_due(tenant_id=tenant_id, limit=limit)
        except Exception:
            LOGGER.exception("memory expiry sweep failed")
            return 0


def rank_memory_items(query: str, items: list[MemoryItem]) -> list[MemoryItem]:
    terms = memory_terms(query)
    if not terms:
        return list(items)

    scored: list[tuple[int, float, float, float, int, MemoryItem]] = []
    for index, item in enumerate(items):
        item_terms = memory_terms(f"{item.key} {item.content}")
        overlap = len(terms & item_terms) / max(1, len(terms))
        importance = _float_metadata(item, "importance", 0.5)
        confidence = _float_metadata(item, "confidence", 1.0)
        # A direct lexical hit must beat an unrelated high-importance memory.
        # Importance/confidence only break ties after relevance is established.
        scored.append((int(overlap > 0), overlap, importance, confidence, -index, item))
    scored.sort(key=lambda row: row[:5], reverse=True)
    return [row[5] for row in scored]


def memory_terms(text: str) -> set[str]:
    normalized = str(text or "").lower()
    terms = set(_WORD.findall(normalized))
    for run in _CJK_RUN.findall(normalized):
        terms.add(run)
        terms.update(run)
        if len(run) > 1:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _float_metadata(item: MemoryItem, key: str, default: float) -> float:
    try:
        return float(item.metadata.get(key, default))
    except (TypeError, ValueError):
        return default


def build_memory_service(settings: Settings) -> MemoryService:
    provider: MemoryProvider | None = None
    if settings.memory.provider == "native_pg":
        provider = NativePgMemoryProvider(settings)
    elif settings.memory.provider == "openviking":
        provider = OpenVikingMemoryProvider(settings)
    else:  # Guarded by config validation; keep Runtime defensive.
        raise ValueError(f"unsupported memory provider: {settings.memory.provider}")
    return MemoryService(
        provider,
        recall_enabled=settings.memory.recall_enabled,
        write_enabled=True,
        recall_limit=settings.memory.recall_limit,
        max_item_chars=settings.memory.max_item_chars,
    )
