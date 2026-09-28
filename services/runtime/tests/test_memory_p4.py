from __future__ import annotations

from pathlib import Path

from aegora_runtime.memory.evaluation import (
    InMemoryEvalProvider,
    load_memory_eval_cases,
    run_memory_eval,
)
from aegora_runtime.memory.extractor import extract_memory_candidates, parse_memory_candidate
from aegora_runtime.memory.lifecycle import contains_sensitive_memory, resolve_memory_candidate
from aegora_runtime.memory.models import MemoryCandidate, MemoryItem, MemoryScope
from aegora_runtime.memory.policy import (
    TENANT_REQUIRED,
    USER_CONTROLLED,
    MemoryGovernance,
    MemoryNamespacePolicy,
    can_recall,
)
from aegora_runtime.memory.service import MemoryService, rank_memory_items


class FakeExtractionClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_json(self, messages, *, model=None, temperature=0.0):
        self.calls.append((messages, model, temperature))
        return self.payload


def governance(*, agent_id="agent-a", share=True) -> MemoryGovernance:
    return MemoryGovernance(
        tenant_id="default",
        agent_id=agent_id,
        namespace_policies={
            "preferences": MemoryNamespacePolicy(
                namespace="preferences",
                mode=USER_CONTROLLED,
                allow_public_agents=False,
            )
        },
        user_preferences={"preferences": share},
    )


def test_extractor_only_parses_supported_candidate_shape() -> None:
    client = FakeExtractionClient(
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
                },
                {
                    "operation": "upsert",
                    "namespace": "unknown",
                    "key": "ignored",
                    "value": "x",
                    "evidence_type": "explicit_fact",
                    "confidence": 1,
                },
            ]
        }
    )

    candidates = extract_memory_candidates(
        client,
        user_message="以后代码示例都用 Python。",
        source_agent_id="coding-agent",
        source_session_id="s1",
        source_trace_id="t1",
        model="aegora-fast",
    )

    assert len(candidates) == 1
    assert candidates[0].key == "preferred_language"
    assert candidates[0].source_agent_id == "coding-agent"
    assert candidates[0].source_trace_id == "t1"
    assert client.calls[0][1] == "aegora-fast"


def test_parse_candidate_rejects_invalid_ttl_or_missing_upsert_value() -> None:
    assert (
        parse_memory_candidate(
            {
                "operation": "upsert",
                "namespace": "preferences",
                "key": "style",
                "value": "",
                "evidence_type": "explicit_fact",
                "confidence": 0.9,
            }
        )
        is None
    )
    assert (
        parse_memory_candidate(
            {
                "operation": "upsert",
                "namespace": "preferences",
                "key": "style",
                "value": "concise",
                "evidence_type": "explicit_fact",
                "confidence": 0.9,
                "ttl_days": 999,
            }
        )
        is None
    )


def test_sensitive_memory_is_blocked_before_provider_write() -> None:
    provider = InMemoryEvalProvider()
    service = MemoryService(provider)
    candidate = MemoryCandidate(
        operation="upsert",
        namespace="preferences",
        key="api_key",
        value="sk-secret",
        evidence_type="explicit_fact",
        confidence=1.0,
        source_agent_id="agent-a",
    )
    assert contains_sensitive_memory(candidate) is True

    decision = resolve_memory_candidate(
        service=service,
        scope=MemoryScope(user_id="u1", agent_id="agent-a"),
        governance=governance(),
        candidate=candidate,
        auto_apply_threshold=0.9,
        forget_threshold=0.98,
    )

    assert decision.status == "rejected"
    assert decision.reason == "sensitive_memory_blocked"
    assert provider.items == []


def test_auto_update_does_not_silently_override_explicit_memory_without_intent() -> None:
    provider = InMemoryEvalProvider(
        [
            MemoryItem(
                memory_id="1",
                memory_type="semantic",
                key="preferred_language",
                content="Python",
                scope="user_global",
                namespace="preferences",
                source_agent_id="agent-a",
                source_kind="explicit",
                metadata={
                    "tenant_id": "default",
                    "user_id": "u1",
                    "confidence": 1.0,
                    "importance": 0.8,
                    "status": "active",
                },
            )
        ]
    )
    service = MemoryService(provider)
    candidate = MemoryCandidate(
        operation="upsert",
        namespace="preferences",
        key="preferred_language",
        value="Rust",
        evidence_type="explicit_fact",
        confidence=0.99,
        update_intent=False,
        source_agent_id="agent-a",
    )

    decision = resolve_memory_candidate(
        service=service,
        scope=MemoryScope(user_id="u1", agent_id="agent-a"),
        governance=governance(),
        candidate=candidate,
        auto_apply_threshold=0.9,
        forget_threshold=0.98,
    )

    assert decision.status == "needs_review"
    assert decision.reason == "conflict_with_explicit_memory"
    current = service.get_current(
        scope=MemoryScope(
            user_id="u1",
            agent_id="agent-a",
            namespace="preferences",
            memory_scope="user_global",
        ),
        key="preferred_language",
    )
    assert current is not None
    assert current.content == "Python"


def test_explicit_update_intent_can_version_explicit_memory_at_high_confidence() -> None:
    provider = InMemoryEvalProvider(
        [
            MemoryItem(
                memory_id="1",
                memory_type="semantic",
                key="preferred_language",
                content="Python",
                scope="user_global",
                namespace="preferences",
                source_agent_id="agent-a",
                source_kind="explicit",
                metadata={
                    "tenant_id": "default",
                    "user_id": "u1",
                    "confidence": 1.0,
                    "importance": 0.8,
                    "status": "active",
                },
            )
        ]
    )
    service = MemoryService(provider)
    candidate = MemoryCandidate(
        operation="upsert",
        namespace="preferences",
        key="preferred_language",
        value="Go",
        evidence_type="explicit_fact",
        confidence=0.99,
        update_intent=True,
        source_agent_id="agent-a",
        source_trace_id="t-update",
    )

    decision = resolve_memory_candidate(
        service=service,
        scope=MemoryScope(user_id="u1", agent_id="agent-a"),
        governance=governance(),
        candidate=candidate,
        auto_apply_threshold=0.9,
        forget_threshold=0.98,
    )

    assert decision.status == "applied"
    assert decision.item is not None
    assert decision.item.content == "Go"
    assert decision.item.version == 2
    assert decision.item.source_kind == "automatic"


def test_forget_requires_explicit_instruction_and_high_threshold() -> None:
    initial = MemoryItem(
        memory_id="1",
        memory_type="semantic",
        key="preferred_language",
        content="Python",
        scope="user_global",
        namespace="preferences",
        source_agent_id="agent-a",
        source_kind="explicit",
        metadata={
            "tenant_id": "default",
            "user_id": "u1",
            "confidence": 1.0,
            "importance": 0.8,
            "status": "active",
        },
    )
    provider = InMemoryEvalProvider([initial])
    service = MemoryService(provider)

    weak = MemoryCandidate(
        operation="forget",
        namespace="preferences",
        key="preferred_language",
        evidence_type="explicit_instruction",
        confidence=0.9,
        source_agent_id="agent-a",
    )
    weak_decision = resolve_memory_candidate(
        service=service,
        scope=MemoryScope(user_id="u1", agent_id="agent-a"),
        governance=governance(),
        candidate=weak,
        auto_apply_threshold=0.9,
        forget_threshold=0.98,
    )
    assert weak_decision.status == "needs_review"

    strong = MemoryCandidate(
        operation="forget",
        namespace="preferences",
        key="preferred_language",
        evidence_type="explicit_instruction",
        confidence=0.99,
        source_agent_id="agent-a",
    )
    strong_decision = resolve_memory_candidate(
        service=service,
        scope=MemoryScope(user_id="u1", agent_id="agent-a"),
        governance=governance(),
        candidate=strong,
        auto_apply_threshold=0.9,
        forget_threshold=0.98,
    )
    assert strong_decision.status == "applied"
    assert (
        service.get_current(
            scope=MemoryScope(
                user_id="u1",
                agent_id="agent-a",
                namespace="preferences",
                memory_scope="user_global",
            ),
            key="preferred_language",
        )
        is None
    )


def test_tenant_policy_never_promotes_legacy_user_memory() -> None:
    legacy = MemoryItem(
        memory_id="legacy",
        memory_type="profile",
        key="department",
        content="Sales",
        scope="user_global",
        namespace="org_profile",
        source_agent_id="old-agent",
        source_kind="explicit",
    )
    tenant = MemoryItem(
        memory_id="tenant",
        memory_type="profile",
        key="department",
        content="AI Platform",
        scope="tenant_user",
        namespace="org_profile",
        source_kind="tenant",
    )
    gov = MemoryGovernance(
        agent_id="office-agent",
        namespace_policies={
            "org_profile": MemoryNamespacePolicy(
                namespace="org_profile",
                mode=TENANT_REQUIRED,
            )
        },
    )

    assert can_recall(legacy, gov) is False
    assert can_recall(tenant, gov) is True


def test_memory_ranking_prefers_query_relevance_over_unrelated_importance() -> None:
    relevant = MemoryItem(
        memory_id="1",
        memory_type="semantic",
        key="preferred_language",
        content="Python",
        metadata={"importance": 0.5, "confidence": 1.0},
    )
    distractor = MemoryItem(
        memory_id="2",
        memory_type="semantic",
        key="timezone",
        content="Asia/Tokyo",
        metadata={"importance": 1.0, "confidence": 1.0},
    )

    ranked = rank_memory_items("请用 Python 写代码", [distractor, relevant])

    assert ranked[0].key == "preferred_language"


def test_seed_memory_eval_covers_quality_and_isolation_metrics() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = load_memory_eval_cases(root / "evals" / "memory_eval_seed.jsonl")

    report = run_memory_eval(cases)

    assert report.metrics["cases"] >= 8
    assert report.metrics["recall_relevance"] == 1.0
    assert report.metrics["distractor_rejection_rate"] == 1.0
    assert report.metrics["privacy_isolation_rate"] == 1.0
    assert report.metrics["update_correctness"] == 1.0
    assert report.metrics["forget_correctness"] == 1.0
    assert report.metrics["candidate_decision_accuracy"] == 1.0
    assert report.metrics["context_budget_pass_rate"] == 1.0
