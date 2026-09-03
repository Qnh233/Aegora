from __future__ import annotations

from aegora_runtime.runtime_cache import RedisReleaseConfigCache, RuntimeCacheSettings


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.last_ttl: int | None = None

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int):
        self.values[key] = value
        self.last_ttl = ex

    def delete(self, key: str):
        self.values.pop(key, None)


def test_release_config_cache_roundtrip_uses_versioned_key() -> None:
    redis = FakeRedis()
    cache = RedisReleaseConfigCache(
        RuntimeCacheSettings(
            enabled=True,
            redis_url="redis://unused",
            key_prefix="test:aegora",
            ttl_seconds=900,
        ),
        client=redis,
    )
    config = {"agent": {"id": "agent_1"}, "tools": []}

    cache.set(config, agent_id="agent_1", release_id="rel_1", version=3)

    assert redis.last_ttl == 900
    assert cache.get(agent_id="agent_1", release_id="rel_1", version=3) == config
    assert cache.get(agent_id="agent_1", release_id="rel_1", version=4) is None


def test_release_config_cache_fails_open_when_redis_errors() -> None:
    class BrokenRedis:
        def get(self, _key):
            raise ConnectionError("redis unavailable")

        def set(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    cache = RedisReleaseConfigCache(
        RuntimeCacheSettings(enabled=True, redis_url="redis://unused"),
        client=BrokenRedis(),
    )

    assert cache.get(agent_id="agent_1", release_id="rel_1", version=1) is None
    cache.set({}, agent_id="agent_1", release_id="rel_1", version=1)
