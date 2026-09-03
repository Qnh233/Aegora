from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import config


DEFAULT_CHANNEL = "aegora:runtime-config:events:v1"


def _enabled() -> bool:
    return config.env_value("RUNTIME_CONFIG_EVENTS_ENABLED").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _redis_url() -> str | None:
    return config.env_value("RUNTIME_CONFIG_REDIS_URL") or config.env_value("REDIS_URL") or None


class RuntimeInvalidationPublisher:
    """Best-effort publisher; Redis must never become a control-plane dependency."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def publish(
        self,
        event_type: str,
        *,
        agent_id: str,
        release_id: str | None = None,
        release_version: int | None = None,
        tool_id: str | None = None,
        reason: str | None = None,
    ) -> bool:
        if not _enabled() or not _redis_url():
            return False
        payload = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "event_type": event_type,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "release_id": release_id,
            "release_version": release_version,
            "tool_id": tool_id,
            "reason": reason,
        }
        try:
            self._redis().publish(
                config.env_value("RUNTIME_CONFIG_INVALIDATION_CHANNEL") or DEFAULT_CHANNEL,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
            return True
        except Exception:
            return False

    def _redis(self) -> Any:
        if self._client is not None:
            return self._client
        from redis import Redis

        self._client = Redis.from_url(
            str(_redis_url()),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        return self._client


_PUBLISHER: RuntimeInvalidationPublisher | None = None


def get_runtime_invalidation_publisher() -> RuntimeInvalidationPublisher:
    global _PUBLISHER
    if _PUBLISHER is None:
        _PUBLISHER = RuntimeInvalidationPublisher()
    return _PUBLISHER
