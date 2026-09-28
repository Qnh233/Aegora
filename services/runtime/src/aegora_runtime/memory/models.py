from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    session_id: str | None = None
    agent_id: str | None = None
    tenant_id: str | None = None
    namespace: str = "preferences"
    memory_scope: str = "user_global"

    def __post_init__(self) -> None:
        user_id = str(self.user_id or "").strip()
        if not user_id:
            raise ValueError("memory scope requires user_id")
        object.__setattr__(self, "user_id", user_id)
        if self.memory_scope not in {"user_global", "user_agent", "tenant_user"}:
            raise ValueError("unsupported memory scope")
        if self.memory_scope == "user_agent" and not self.agent_id:
            raise ValueError("user_agent memory requires agent_id")


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    memory_type: str
    key: str
    content: str
    scope: str = "user_global"
    namespace: str = "preferences"
    version: int = 1
    source_agent_id: str | None = None
    source_kind: str = "legacy"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.memory_id,
            "type": self.memory_type,
            "key": self.key,
            "content": self.content,
            "scope": self.scope,
            "namespace": self.namespace,
            "version": self.version,
            "source_agent_id": self.source_agent_id,
            "source_kind": self.source_kind,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MemoryWrite:
    key: str
    value: str
    memory_type: str = "semantic"
    source_agent_id: str | None = None
    source_session_id: str | None = None
    source_trace_id: str | None = None
    source_kind: str = "explicit"
    confidence: float = 1.0
    importance: float = 0.5
    expires_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        value = str(self.value or "").strip()
        if not key:
            raise ValueError("memory key is required")
        if not value:
            raise ValueError("memory value is required")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "value", value)
        if not 0 <= self.confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        if not 0 <= self.importance <= 1:
            raise ValueError("memory importance must be between 0 and 1")
        if self.source_kind not in {"explicit", "automatic", "tenant", "legacy"}:
            raise ValueError("unsupported memory source kind")


@dataclass(frozen=True)
class MemoryCandidate:
    operation: str
    namespace: str
    key: str
    value: str | None = None
    memory_type: str = "semantic"
    evidence_type: str = "explicit_fact"
    confidence: float = 0.0
    importance: float = 0.5
    update_intent: bool = False
    ttl_days: int | None = None
    rationale: str | None = None
    source_agent_id: str | None = None
    source_session_id: str | None = None
    source_trace_id: str | None = None
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        operation = str(self.operation or "").strip().lower()
        namespace = str(self.namespace or "").strip()
        key = str(self.key or "").strip()
        value = str(self.value).strip() if self.value is not None else None
        if operation not in {"upsert", "forget"}:
            raise ValueError("unsupported memory candidate operation")
        if not namespace or not key:
            raise ValueError("memory candidate requires namespace and key")
        if operation == "upsert" and not value:
            raise ValueError("upsert memory candidate requires value")
        if self.memory_type not in {"semantic", "episodic", "profile", "summary"}:
            raise ValueError("unsupported memory candidate type")
        if self.evidence_type not in {"explicit_fact", "explicit_instruction", "inferred"}:
            raise ValueError("unsupported memory evidence type")
        if not 0 <= self.confidence <= 1:
            raise ValueError("memory candidate confidence must be between 0 and 1")
        if not 0 <= self.importance <= 1:
            raise ValueError("memory candidate importance must be between 0 and 1")
        if self.ttl_days is not None and not 1 <= self.ttl_days <= 365:
            raise ValueError("memory candidate ttl_days must be between 1 and 365")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class MemoryCandidateDecision:
    status: str
    reason: str
    candidate_id: str | None = None
    item: MemoryItem | None = None

    def __post_init__(self) -> None:
        if self.status not in {"applied", "needs_review", "rejected", "skipped"}:
            raise ValueError("unsupported memory candidate decision status")


@dataclass(frozen=True)
class MemoryWriteResult:
    saved: bool
    item: MemoryItem | None = None
    reason: str | None = None

    def to_tool_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"saved": self.saved}
        if self.item is not None:
            result.update(
                {
                    "memory_id": self.item.memory_id,
                    "memory_type": self.item.memory_type,
                    "key": self.item.key,
                }
            )
        if self.reason:
            result["reason"] = self.reason
        return result
