from __future__ import annotations

import json
import os
from dataclasses import dataclass
from threading import Event
from typing import Any

from aegora_runtime.runtime_cache import RedisReleaseConfigCache, get_runtime_config_cache


DEFAULT_CHANNEL = "aegora:runtime-config:events:v1"
SUPPORTED_EVENTS = {"release.published", "release.revoked", "tool.policy.changed"}


@dataclass(frozen=True)
class RuntimeInvalidationSettings:
    enabled: bool
    redis_url: str | None
    channel: str = DEFAULT_CHANNEL
    retry_seconds: float = 2.0


def runtime_invalidation_settings() -> RuntimeInvalidationSettings:
    redis_url = os.getenv("RUNTIME_CONFIG_REDIS_URL") or os.getenv("REDIS_URL")
    enabled = os.getenv("RUNTIME_CONFIG_EVENTS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return RuntimeInvalidationSettings(
        enabled=enabled and bool(redis_url),
        redis_url=redis_url,
        channel=os.getenv("RUNTIME_CONFIG_INVALIDATION_CHANNEL", DEFAULT_CHANNEL),
        retry_seconds=max(0.1, float(os.getenv("RUNTIME_CONFIG_EVENT_RETRY_SECONDS", "2"))),
    )


class RuntimeInvalidationSubscriber:
    """Consumes cache events without making Redis part of runtime correctness."""

    def __init__(
        self,
        settings: RuntimeInvalidationSettings,
        *,
        cache: RedisReleaseConfigCache | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or get_runtime_config_cache()
        self._client = client

    def apply_event(self, raw: str | dict[str, object]) -> bool:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if payload.get("schema_version") != 1 or payload.get("event_type") not in SUPPORTED_EVENTS:
            return False
        release_id = payload.get("release_id")
        version = payload.get("release_version")
        agent_id = payload.get("agent_id")
        if release_id and isinstance(version, int) and agent_id:
            self.cache.delete(
                agent_id=str(agent_id),
                release_id=str(release_id),
                version=version,
            )
        # Tool policy is deliberately not cached today; consuming the event still
        # establishes a versioned cross-pod convergence/observability contract.
        return True

    def run(self, stop_event: Event) -> None:
        if not self.settings.enabled:
            return
        while not stop_event.is_set():
            try:
                pubsub = self._redis().pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.settings.channel)
                while not stop_event.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "message":
                        self.apply_event(message.get("data", ""))
            except Exception:
                stop_event.wait(self.settings.retry_seconds)

    def _redis(self) -> Any:
        if self._client is not None:
            return self._client
        from redis import Redis

        self._client = Redis.from_url(
            str(self.settings.redis_url),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        return self._client
