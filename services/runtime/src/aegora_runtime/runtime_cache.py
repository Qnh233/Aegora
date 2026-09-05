from __future__ import annotations

import json
import os
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class RuntimeCacheSettings:
    enabled: bool
    redis_url: str | None
    key_prefix: str = "aegora:runtime-config:v1"
    ttl_seconds: int = 3600
    l1_max_entries: int = 256


def runtime_cache_settings() -> RuntimeCacheSettings:
    enabled = os.getenv("RUNTIME_CONFIG_CACHE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    redis_url = os.getenv("RUNTIME_CONFIG_REDIS_URL") or os.getenv("REDIS_URL")
    ttl_seconds = max(60, int(os.getenv("RUNTIME_CONFIG_CACHE_TTL_SECONDS", "3600")))
    return RuntimeCacheSettings(
        enabled=enabled and bool(redis_url),
        redis_url=redis_url,
        key_prefix=os.getenv("RUNTIME_CONFIG_CACHE_KEY_PREFIX", "aegora:runtime-config:v1"),
        ttl_seconds=ttl_seconds,
        l1_max_entries=max(1, int(os.getenv("RUNTIME_CONFIG_L1_MAX_ENTRIES", "256"))),
    )


class RedisReleaseConfigCache:
    """Fail-open L2 cache for immutable release config only.

    Mutable governance facts (release status, actor permissions, tool state and
    MCP connection state) are deliberately excluded from this cache.
    """

    def __init__(self, settings: RuntimeCacheSettings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client
        self._l1: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def get(self, *, agent_id: str, release_id: str, version: int) -> dict[str, object] | None:
        if not self.enabled:
            return None
        key = self._key(agent_id, release_id, version)
        cached = self._l1_get(key)
        if cached is not None:
            return cached
        try:
            payload = self._redis().get(key)
            if not payload:
                return None
            value = json.loads(payload)
            if not isinstance(value, dict):
                return None
            self._l1_set(key, value)
            return deepcopy(value)
        except Exception:
            # Cache availability must never block runtime correctness.
            return None

    def set(
        self,
        config_json: dict[str, object],
        *,
        agent_id: str,
        release_id: str,
        version: int,
    ) -> None:
        if not self.enabled:
            return
        key = self._key(agent_id, release_id, version)
        self._l1_set(key, config_json)
        try:
            self._redis().set(
                key,
                json.dumps(config_json, ensure_ascii=False, separators=(",", ":")),
                ex=self.settings.ttl_seconds,
            )
        except Exception:
            return

    def delete(self, *, agent_id: str, release_id: str, version: int) -> None:
        if not self.enabled:
            return
        key = self._key(agent_id, release_id, version)
        with self._lock:
            self._l1.pop(key, None)
        try:
            self._redis().delete(key)
        except Exception:
            return

    def _l1_get(self, key: str) -> dict[str, object] | None:
        with self._lock:
            value = self._l1.get(key)
            if value is None:
                return None
            self._l1.move_to_end(key)
            return deepcopy(value)

    def _l1_set(self, key: str, value: dict[str, object]) -> None:
        with self._lock:
            self._l1[key] = deepcopy(value)
            self._l1.move_to_end(key)
            while len(self._l1) > self.settings.l1_max_entries:
                self._l1.popitem(last=False)

    def _redis(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from redis import Redis
        except ImportError as exc:
            raise RuntimeError("redis dependency is unavailable") from exc
        self._client = Redis.from_url(
            str(self.settings.redis_url),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        return self._client

    def _key(self, agent_id: str, release_id: str, version: int) -> str:
        return f"{self.settings.key_prefix}:agent:{agent_id}:release:{release_id}:version:{version}"


_CACHE: RedisReleaseConfigCache | None = None


def get_runtime_config_cache() -> RedisReleaseConfigCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = RedisReleaseConfigCache(runtime_cache_settings())
    return _CACHE


def reset_runtime_config_cache() -> None:
    global _CACHE
    _CACHE = None
