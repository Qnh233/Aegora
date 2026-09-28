from __future__ import annotations

from dataclasses import replace

from aegora_runtime.config import load_settings
from aegora_runtime.data_ops import memory_extraction
from aegora_runtime.memory.evaluation import InMemoryEvalProvider
from aegora_runtime.memory.models import MemoryCandidate, MemoryCandidateDecision
from aegora_runtime.memory.service import MemoryService


def settings():
    return load_settings(env_path=None)


class FakeStore:
    def __init__(self, _settings, *, completed=None):
        self.completed = set(completed or [])
        self.enqueued = []
        self.decisions = []
        self.completed_rows = []
        self.failed_rows = []

    def extraction_is_completed(self, trace_id):
        return trace_id in self.completed

    def enqueue(self, *, scope, candidate, proposed_scope):
        queued = replace(candidate, candidate_id=str(len(self.enqueued) + 1))
        self.enqueued.append((scope, queued, proposed_scope))
        return queued

    def mark_decision(self, *, scope, candidate, decision):
        self.decisions.append((scope, candidate, decision))

    def extraction_completed(self, **kwargs):
        self.completed_rows.append(kwargs)

    def extraction_failed(self, **kwargs):
        self.failed_rows.append(kwargs)


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, messages, *, model=None, temperature=0.0):
        del messages, model, temperature
        self.calls += 1
        return self.payload


def fake_memory_service():
    return MemoryService(
        InMemoryEvalProvider(),
        recall_enabled=True,
        recall_limit=8,
        max_item_chars=400,
    )


def test_extraction_skips_anonymous_user_without_llm_call(monkeypatch) -> None:
    client = FakeClient({"candidates": []})
    service = fake_memory_service()
    monkeypatch.setattr(memory_extraction, "build_memory_service", lambda _settings: service)
    monkeypatch.setattr(
        memory_extraction,
        "load_user_messages",
        lambda *_args, **_kwargs: [
            {
                "trace_id": "t1",
                "session_id": "s1",
                "user_id": "session-owner:s1",
                "role": "user",
                "content": "以后都用 Python",
                "metadata": {},
            }
        ],
    )

    result = memory_extraction.run_memory_extraction(
        settings(),
        force=True,
        client=client,
    )

    assert client.calls == 0
    assert result.metrics["messages_skipped_anonymous"] == 1
    assert result.metrics["candidates"] == 0


def test_extraction_fails_closed_when_memory_policy_is_unavailable(monkeypatch) -> None:
    client = FakeClient({"candidates": []})
    service = fake_memory_service()
    store = FakeStore(None)
    monkeypatch.setattr(memory_extraction, "build_memory_service", lambda _settings: service)
    monkeypatch.setattr(memory_extraction, "MemoryCandidateStore", lambda _settings: store)
    monkeypatch.setattr(
        memory_extraction,
        "load_user_messages",
        lambda *_args, **_kwargs: [
            {
                "trace_id": "t1",
                "session_id": "s1",
                "user_id": "u1",
                "role": "user",
                "content": "以后都用 Python",
                "metadata": {"agent_id": "agent-a"},
            }
        ],
    )
    monkeypatch.setattr(
        memory_extraction.runtime_context_resolver,
        "fetch_memory_governance",
        lambda *_args, **_kwargs: {
            "tenant_id": "default",
            "namespace_policies": {},
            "user_preferences": {},
            "policy_available": False,
        },
    )

    result = memory_extraction.run_memory_extraction(
        settings(),
        force=True,
        client=client,
    )

    assert client.calls == 0
    assert result.metrics["messages_skipped_policy_unavailable"] == 1
    assert result.metrics["messages_processed"] == 0


def test_extraction_is_idempotent_for_completed_trace(monkeypatch) -> None:
    client = FakeClient({"candidates": []})
    service = fake_memory_service()
    store = FakeStore(None, completed={"done-trace"})
    monkeypatch.setattr(memory_extraction, "build_memory_service", lambda _settings: service)
    monkeypatch.setattr(memory_extraction, "MemoryCandidateStore", lambda _settings: store)
    monkeypatch.setattr(
        memory_extraction,
        "load_user_messages",
        lambda *_args, **_kwargs: [
            {
                "trace_id": "done-trace",
                "session_id": "s1",
                "user_id": "u1",
                "role": "user",
                "content": "以后都用 Python",
                "metadata": {"agent_id": "agent-a"},
            }
        ],
    )

    result = memory_extraction.run_memory_extraction(
        settings(),
        force=True,
        client=client,
    )

    assert client.calls == 0
    assert result.metrics["messages_already_processed"] == 1


def test_extraction_queues_and_applies_governed_candidate(monkeypatch) -> None:
    client = FakeClient(
        {
            "candidates": [
                {
                    "operation": "upsert",
                    "namespace": "preferences",
                    "key": "preferred_language",
                    "value": "Python",
                    "memory_type": "semantic",
                    "evidence_type": "explicit_fact",
                    "confidence": 0.99,
                    "importance": 0.8,
                    "update_intent": False,
                    "rationale": "用户明确说明长期代码偏好",
                }
            ]
        }
    )
    service = fake_memory_service()
    store = FakeStore(None)
    monkeypatch.setattr(memory_extraction, "build_memory_service", lambda _settings: service)
    monkeypatch.setattr(memory_extraction, "MemoryCandidateStore", lambda _settings: store)
    monkeypatch.setattr(
        memory_extraction,
        "load_user_messages",
        lambda *_args, **_kwargs: [
            {
                "trace_id": "t1",
                "session_id": "s1",
                "user_id": "u1",
                "role": "user",
                "content": "以后代码示例都用 Python。",
                "metadata": {"agent_id": "agent-a"},
            }
        ],
    )
    monkeypatch.setattr(
        memory_extraction.runtime_context_resolver,
        "fetch_memory_governance",
        lambda *_args, **_kwargs: {
            "tenant_id": "default",
            "agent_visibility": "private",
            "namespace_policies": {
                "preferences": {
                    "mode": "user_controlled",
                    "allow_public_agents": False,
                }
            },
            "user_preferences": {"preferences": True},
            "policy_available": True,
        },
    )

    result = memory_extraction.run_memory_extraction(
        settings(),
        force=True,
        client=client,
    )

    assert client.calls == 1
    assert result.metrics["messages_processed"] == 1
    assert result.metrics["candidates"] == 1
    assert result.metrics["applied"] == 1
    assert store.enqueued[0][2] == "user_global"
    assert store.decisions[0][2].status == "applied"
    assert store.completed_rows[0]["trace_id"] == "t1"
    active = [
        item
        for item in service.provider.items
        if item.metadata.get("status") == "active"
    ]
    assert [(item.key, item.content, item.source_kind) for item in active] == [
        ("preferred_language", "Python", "automatic")
    ]


def test_agent_private_candidate_without_agent_identity_is_rejected() -> None:
    service = fake_memory_service()
    candidate = MemoryCandidate(
        operation="upsert",
        namespace="work_context",
        key="preferred_test_framework",
        value="pytest",
        evidence_type="explicit_fact",
        confidence=0.99,
    )
    from aegora_runtime.memory.lifecycle import resolve_memory_candidate
    from aegora_runtime.memory.models import MemoryScope
    from aegora_runtime.memory.policy import MemoryGovernance

    decision = resolve_memory_candidate(
        service=service,
        scope=MemoryScope(user_id="u1"),
        governance=MemoryGovernance(agent_id=None),
        candidate=candidate,
        auto_apply_threshold=0.9,
        forget_threshold=0.98,
    )

    assert decision == MemoryCandidateDecision(
        status="rejected",
        reason="agent_private_memory_requires_agent_id",
    )
