from aegora_runtime.runtime_invalidation import (
    RuntimeInvalidationSettings,
    RuntimeInvalidationSubscriber,
)


class FakeCache:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, int]] = []

    def delete(self, *, agent_id: str, release_id: str, version: int) -> None:
        self.deleted.append((agent_id, release_id, version))


def subscriber(cache: FakeCache) -> RuntimeInvalidationSubscriber:
    return RuntimeInvalidationSubscriber(
        RuntimeInvalidationSettings(enabled=True, redis_url="redis://example/0"),
        cache=cache,  # type: ignore[arg-type]
    )


def test_release_event_evicts_exact_versioned_cache_key() -> None:
    cache = FakeCache()
    consumer = subscriber(cache)

    assert consumer.apply_event(
        {
            "schema_version": 1,
            "event_id": "evt_1",
            "event_type": "release.revoked",
            "occurred_at": "2026-09-04T00:00:00+00:00",
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "release_version": 3,
            "tool_id": None,
            "reason": None,
        }
    ) is True
    assert cache.deleted == [("agent_1", "rel_1", 3)]


def test_tool_policy_event_is_consumed_without_evicting_static_release_config() -> None:
    cache = FakeCache()
    consumer = subscriber(cache)

    assert consumer.apply_event(
        {
            "schema_version": 1,
            "event_id": "evt_2",
            "event_type": "tool.policy.changed",
            "occurred_at": "2026-09-04T00:00:00+00:00",
            "agent_id": "*",
            "release_id": None,
            "release_version": None,
            "tool_id": "tool_1",
            "reason": "status:disabled",
        }
    ) is True
    assert cache.deleted == []


def test_invalid_event_is_ignored() -> None:
    cache = FakeCache()
    consumer = subscriber(cache)

    assert consumer.apply_event('{"schema_version":2,"event_type":"release.revoked"}') is False
    assert consumer.apply_event("not-json") is False
    assert cache.deleted == []
