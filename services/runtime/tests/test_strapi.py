from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aegora_runtime.strapi import entity_reference, normalize_entity
from scripts.strapi_schema import content_type_payload
from scripts.sync_strapi_content import (
    normalize_strapi_skill,
    procedural_skill_state_errors,
    sync_skill_rows,
)


def test_normalize_entity_supports_strapi_v4() -> None:
    assert normalize_entity({"id": 7, "attributes": {"name": "skill"}}) == {"id": 7, "name": "skill"}


def test_normalize_entity_supports_strapi_v5() -> None:
    row = {"documentId": "doc-1", "name": "skill"}

    assert normalize_entity(row) == row
    assert entity_reference(row) == "doc-1"


def test_normalize_strapi_skill_maps_lifecycle_status() -> None:
    row = normalize_strapi_skill(
        {
            "id": 7,
            "name": "member",
            "title": "会员",
            "description": "会员说明",
            "content": "按规则回答",
            "lifecycle_status": "active",
        }
    )

    assert row["status"] == "active"
    assert row["metadata"]["strapi_id"] == 7


def test_strapi_sync_rejects_unevaluated_active_agent_skill() -> None:
    settings = SimpleNamespace(skills=SimpleNamespace(max_content_chars=1000))
    row = {
        "name": "agent_tip",
        "title": "Agent 建议",
        "description": "来自反思的候选经验",
        "content": "先确认上下文再回答",
        "source": "agent",
        "lifecycle_status": "active",
        "metadata": {"source_agent_id": "agent-1"},
    }

    with pytest.raises(ValueError, match="status=passed"):
        sync_skill_rows([row], settings)


def test_procedural_active_skill_rechecks_live_governance_state(monkeypatch) -> None:
    row = {
        "name": "membership_guidance",
        "source": "agent",
        "status": "active",
        "metadata": {
            "source_kind": "procedural_memory",
            "procedural_memory_id": 77,
            "procedural_fingerprint": "f" * 64,
            "source_agent_id": "agent-a",
        },
    }

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query, params):
            assert params == ([77],)

        def fetchall(self):
            return [
                {
                    "id": 77,
                    "agent_id": "agent-a",
                    "fingerprint": "f" * 64,
                    "status": "conflicted",
                    "promoted_skill_name": "membership_guidance",
                }
            ]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_connect(_settings):
        yield FakeConnection()

    monkeypatch.setattr("scripts.sync_strapi_content.connect", fake_connect)

    errors = procedural_skill_state_errors([row], SimpleNamespace())

    assert len(errors) == 1
    assert "conflicted" in errors[0]
    assert "禁止晋级 active" in errors[0]


def test_procedural_live_gate_accepts_matching_skill_drafted_state(monkeypatch) -> None:
    row = {
        "name": "membership_guidance",
        "source": "agent",
        "status": "active",
        "metadata": {
            "source_kind": "procedural_memory",
            "procedural_memory_id": 77,
            "procedural_fingerprint": "f" * 64,
            "source_agent_id": "agent-a",
        },
    }

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query, _params):
            pass

        def fetchall(self):
            return [
                {
                    "id": 77,
                    "agent_id": "agent-a",
                    "fingerprint": "f" * 64,
                    "status": "skill_drafted",
                    "promoted_skill_name": "membership_guidance",
                }
            ]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_connect(_settings):
        yield FakeConnection()

    monkeypatch.setattr("scripts.sync_strapi_content.connect", fake_connect)

    assert procedural_skill_state_errors([row], SimpleNamespace()) == []


def test_content_type_payload_keeps_schema_attributes() -> None:
    schema = {
        "kind": "collectionType",
        "collectionName": "items",
        "info": {"singularName": "item", "pluralName": "items", "displayName": "Items"},
        "attributes": {"name": {"type": "string"}},
    }

    assert content_type_payload(schema)["contentType"]["attributes"] == schema["attributes"]
