from __future__ import annotations

from contextlib import contextmanager

import pytest

from aegora_runtime.sessions import (
    derive_user_id_from_session,
    latency_ms,
    load_recent_conversation,
    load_session_context_views,
    normalize_session_id,
    retrieved_faq_ids,
    save_chat_turn,
    save_message_feedback,
    session_execution_lock,
)


def test_retrieved_faq_ids_filters_missing_values() -> None:
    result = {"retrieved_faqs": [{"faq_id": "34"}, {"title": "missing"}, {"faq_id": 37}]}

    assert retrieved_faq_ids(result) == [34, 37]


def test_session_identity_is_session_centric() -> None:
    assert normalize_session_id(" external-session ") == "external-session"
    assert normalize_session_id(None) == "local-session"
    assert derive_user_id_from_session("external-session") == "session-owner:external-session"


def test_latency_ms_casts_float_value() -> None:
    assert latency_ms({"total_latency_ms": 123.9}) == 123
    assert latency_ms({}) is None


def test_retrieved_trace_id_is_preserved() -> None:
    from aegora_runtime.sessions import trace_id

    assert trace_id({"trace_id": "turn-123"}, "session-1") == "turn-123"
    assert trace_id({}, "session-1") == "session-1"


def test_save_message_feedback_rejects_unknown_rating() -> None:
    with pytest.raises(ValueError, match="rating must be positive or negative"):
        save_message_feedback(
            None,
            assistant_message_id=1,
            session_id="session-1",
            user_id="user-1",
            rating="neutral",
        )


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        return None


def fake_connect(connection):
    @contextmanager
    def _connect(_settings):
        yield connection

    return _connect


def test_load_recent_conversation_keeps_history_and_ids_in_one_snapshot(monkeypatch) -> None:
    client = FakeStrapiClient(
        [
            {"message_key": "m4", "role": "assistant", "content": "a2"},
            {"message_key": "m3", "role": "user", "content": "u2"},
            {"message_key": "m2", "role": "assistant", "content": "a1"},
            {"message_key": "m1", "role": "user", "content": "u1"},
        ]
    )
    monkeypatch.setattr("aegora_runtime.sessions.client_from_settings", lambda settings: client)
    monkeypatch.setattr("aegora_runtime.sessions.collection_endpoint", lambda settings, name: f"/api/{name}")

    history, assistant_ids = load_recent_conversation(None, "session-1")

    assert [item["content"] for item in history] == ["u1", "a1", "u2", "a2"]
    assert assistant_ids == ["m2", "m4"]
    assert client.list_calls[0]["filters"] == {"filters[session_id][$eq]": "session-1"}


def test_load_session_context_views_classifies_and_deduplicates_by_message_key(monkeypatch) -> None:
    rows = [
        {"message_key": "m6", "session_id": "session-1", "user_id": "u-1", "role": "assistant", "content": "a3"},
        {"message_key": "m5", "session_id": "session-1", "user_id": "u-1", "role": "user", "content": "u3"},
        {"message_key": "m4", "session_id": "session-1", "user_id": "u-2", "role": "assistant", "content": "other-a"},
        {"message_key": "m3", "session_id": "session-1", "user_id": "u-2", "role": "user", "content": "other-u"},
        {"message_key": "m2", "session_id": "session-1", "user_id": "u-1", "role": "assistant", "content": "a1"},
        {"message_key": "m1", "session_id": "session-1", "user_id": "u-1", "role": "user", "content": "u1"},
    ]
    client = FakeStrapiClient(rows)
    monkeypatch.setattr("aegora_runtime.sessions.client_from_settings", lambda settings: client)
    monkeypatch.setattr("aegora_runtime.sessions.collection_endpoint", lambda settings, name: f"/api/{name}")

    context = load_session_context_views(None, "session-1", "u-1", session_limit=4, user_limit=4)

    assert [item["message_key"] for item in context["session_context"]] == ["m3", "m4", "m5", "m6"]
    assert [item["message_key"] for item in context["session_user_context"]] == ["m1", "m2"]
    assert client.list_calls[0]["filters"] == {"filters[session_id][$eq]": "session-1"}
    assert client.list_calls[1]["filters"] == {
        "filters[session_id][$eq]": "session-1",
        "filters[user_id][$eq]": "u-1",
    }


class FakeStrapiClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.list_calls = []
        self.upserts = []

    def list(self, endpoint, **kwargs):
        self.list_calls.append({"endpoint": endpoint, **kwargs})
        return self.rows

    def find_one(self, endpoint, field, value):
        return next((row for row in self.rows if row.get(field) == value), None)

    def upsert(self, endpoint, field, value, data):
        self.upserts.append({"endpoint": endpoint, "field": field, "value": value, "data": data})
        return dict(data)


def test_save_chat_turn_writes_session_and_messages_to_strapi(monkeypatch) -> None:
    client = FakeStrapiClient()
    monkeypatch.setattr("aegora_runtime.sessions.client_from_settings", lambda settings: client)
    monkeypatch.setattr("aegora_runtime.sessions.collection_endpoint", lambda settings, name: f"/api/{name}")
    settings = type("Settings", (), {"observability": type("Obs", (), {"instance_id": "test"})()})()

    message_key = save_chat_turn(
        settings,
        session_id="s1",
        user_id="u1",
        user_message="问题",
        assistant_message="回答",
        result={"trace_id": "turn-1", "route": "chat", "skills": []},
        request_metadata={"agent_id": "agent-1", "release_id": "rel-1", "release_version": 3},
    )

    assert message_key == "turn-1:assistant"
    assert [item["endpoint"] for item in client.upserts] == [
        "/api/chat_sessions",
        "/api/chat_messages",
        "/api/chat_messages",
    ]
    user_metadata = client.upserts[1]["data"]["metadata"]
    assert user_metadata["agent_id"] == "agent-1"
    assert user_metadata["release_id"] == "rel-1"
    assert user_metadata["release_version"] == 3


def test_save_feedback_verifies_assistant_message_in_strapi(monkeypatch) -> None:
    client = FakeStrapiClient(
        [{"message_key": "turn-1:assistant", "session_id": "s1", "role": "assistant", "trace_id": "turn-1"}]
    )
    monkeypatch.setattr("aegora_runtime.sessions.client_from_settings", lambda settings: client)
    monkeypatch.setattr("aegora_runtime.sessions.collection_endpoint", lambda settings, name: f"/api/{name}")

    result = save_message_feedback(
        None,
        assistant_message_id="turn-1:assistant",
        session_id="s1",
        user_id="u1",
        rating="positive",
    )

    assert result["assistant_message_key"] == "turn-1:assistant"
    assert client.upserts[-1]["endpoint"] == "/api/message_feedback"


def test_session_execution_lock_unlocks_after_failure(monkeypatch) -> None:
    cursor = FakeCursor()
    monkeypatch.setattr("aegora_runtime.sessions.connect", fake_connect(FakeConnection(cursor)))

    with pytest.raises(RuntimeError):
        with session_execution_lock(None, "session-1"):
            raise RuntimeError("failed")

    statements = [query for query, _ in cursor.calls]
    assert "pg_advisory_lock" in statements[0]
    assert "pg_advisory_unlock" in statements[-1]
