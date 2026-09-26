from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    session_id: str | None = None
    agent_id: str | None = None
    tenant_id: str | None = None
    namespace: str = "user"

    def __post_init__(self) -> None:
        user_id = str(self.user_id or "").strip()
        if not user_id:
            raise ValueError("memory scope requires user_id")
        object.__setattr__(self, "user_id", user_id)


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    memory_type: str
    key: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.memory_id,
            "type": self.memory_type,
            "key": self.key,
            "content": self.content,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MemoryWrite:
    key: str
    value: str
    memory_type: str = "semantic"

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        value = str(self.value or "").strip()
        if not key:
            raise ValueError("memory key is required")
        if not value:
            raise ValueError("memory value is required")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "value", value)


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
