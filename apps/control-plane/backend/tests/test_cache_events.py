import json

from app import cache_events


class FakeRedis:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def publish(self, channel: str, payload: str) -> None:
        self.messages.append((channel, payload))


def test_runtime_invalidation_publisher_emits_versioned_release_event(monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_CONFIG_EVENTS_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_CONFIG_REDIS_URL", "redis://example/0")
    redis = FakeRedis()
    publisher = cache_events.RuntimeInvalidationPublisher(client=redis)

    assert publisher.publish(
        "release.revoked",
        agent_id="agent_1",
        release_id="rel_1",
        release_version=4,
    ) is True

    channel, raw = redis.messages[0]
    payload = json.loads(raw)
    assert channel == cache_events.DEFAULT_CHANNEL
    assert payload["schema_version"] == 1
    assert payload["event_type"] == "release.revoked"
    assert payload["agent_id"] == "agent_1"
    assert payload["release_id"] == "rel_1"
    assert payload["release_version"] == 4
    assert payload["event_id"]
    assert payload["occurred_at"]


def test_runtime_invalidation_publisher_is_fail_open(monkeypatch) -> None:
    class BrokenRedis:
        def publish(self, channel: str, payload: str) -> None:
            raise OSError("redis unavailable")

    monkeypatch.setenv("RUNTIME_CONFIG_EVENTS_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_CONFIG_REDIS_URL", "redis://example/0")
    publisher = cache_events.RuntimeInvalidationPublisher(client=BrokenRedis())

    assert publisher.publish("tool.policy.changed", agent_id="*", tool_id="tool_1") is False
