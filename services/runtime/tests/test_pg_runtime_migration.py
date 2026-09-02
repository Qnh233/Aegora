from __future__ import annotations

from datetime import datetime

from scripts.migrate_pg_runtime_to_strapi import legacy_message_key, message_payload, session_payload


def test_legacy_message_key_is_stable() -> None:
    assert legacy_message_key("17") == "pg:17"


def test_message_payload_preserves_legacy_pg_metadata() -> None:
    row = {
        "id": 17,
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t1",
        "role": "assistant",
        "content": "answer",
        "route": "faq",
        "intent": {"category": "account"},
        "retrieved_faq_ids": [1, 2],
        "tool_calls": [{"name": "faq_search"}],
        "observability": [],
        "latency_ms": 123,
        "metadata": {"a": 1},
        "created_at": datetime(2026, 1, 1, 12, 0, 0),
    }

    payload = message_payload("pg:17", row)

    assert payload["message_key"] == "pg:17"
    assert payload["source"] == "pg_migration"
    assert payload["metadata"]["a"] == 1
    assert payload["metadata"]["legacy_pg"]["id"] == 17
    assert payload["metadata"]["legacy_pg"]["created_at"] == "2026-01-01T12:00:00"


def test_session_payload_can_create_missing_session_reference() -> None:
    payload = session_payload("s1", None)

    assert payload["session_id"] == "s1"
    assert payload["user_id"] == "session-owner:s1"
    assert payload["source"] == "pg_migration"
