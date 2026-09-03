from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Lock
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
        self._metrics_lock = Lock()
        self._delivery_counts = {"success": 0, "failure": 0, "disabled": 0}
        self._last_success_at: str | None = None
        self._last_failure_at: str | None = None
        self._last_error: str | None = None

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
            self._record_delivery("disabled")
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
            self._record_delivery("success")
            return True
        except Exception as error:
            self._record_delivery("failure", error=error)
            return False

    def delivery_metrics(self) -> dict[str, Any]:
        """Return low-cardinality process-local delivery metrics for operations."""
        with self._metrics_lock:
            return {
                "enabled": _enabled(),
                "configured": bool(_redis_url()),
                "channel": config.env_value("RUNTIME_CONFIG_INVALIDATION_CHANNEL")
                or DEFAULT_CHANNEL,
                "delivery_total": dict(self._delivery_counts),
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "last_error": self._last_error,
            }

    def _record_delivery(self, outcome: str, *, error: Exception | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._metrics_lock:
            self._delivery_counts[outcome] += 1
            if outcome == "success":
                self._last_success_at = now
            elif outcome == "failure":
                self._last_failure_at = now
                self._last_error = f"{type(error).__name__}: {error}" if error else None

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
