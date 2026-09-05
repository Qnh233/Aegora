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
    assert cache.get(agent_id="agent_1", release_id="rel_1", version=1) == {}


def test_release_config_cache_l1_is_bounded_and_returns_copies() -> None:
    redis = FakeRedis()
    cache = RedisReleaseConfigCache(
        RuntimeCacheSettings(
            enabled=True,
            redis_url="redis://unused",
            key_prefix="test:aegora",
            l1_max_entries=2,
        ),
        client=redis,
    )

    cache.set({"agent": {"id": "a1"}}, agent_id="a1", release_id="r1", version=1)
    cache.set({"agent": {"id": "a2"}}, agent_id="a2", release_id="r2", version=1)
    first = cache.get(agent_id="a1", release_id="r1", version=1)
    assert first == {"agent": {"id": "a1"}}
    first["agent"]["id"] = "mutated"
    assert cache.get(agent_id="a1", release_id="r1", version=1) == {"agent": {"id": "a1"}}

    cache.set({"agent": {"id": "a3"}}, agent_id="a3", release_id="r3", version=1)
    redis.values.pop("test:aegora:agent:a2:release:r2:version:1", None)
    assert cache.get(agent_id="a2", release_id="r2", version=1) is None


def test_release_config_cache_delete_clears_l1_and_l2() -> None:
    redis = FakeRedis()
    cache = RedisReleaseConfigCache(
        RuntimeCacheSettings(enabled=True, redis_url="redis://unused", key_prefix="test:aegora"),
        client=redis,
    )
    cache.set({"agent": {"id": "a1"}}, agent_id="a1", release_id="r1", version=2)

    cache.delete(agent_id="a1", release_id="r1", version=2)

    assert cache.get(agent_id="a1", release_id="r1", version=2) is None
