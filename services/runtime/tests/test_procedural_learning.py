from __future__ import annotations

from types import SimpleNamespace

from aegora_runtime.config import load_settings
from aegora_runtime.data_ops import reflection_flow
from aegora_runtime.data_ops.procedural_learning import (
    ProceduralPromotionPolicy,
    build_procedural_item,
    can_create_skill_draft,
    procedural_fingerprint,
)
from aegora_runtime.data_ops.reflection_flow import fallback_skill_draft_from_item
from aegora_runtime.data_ops.skill_drafts import write_skill_draft
from aegora_runtime.skills import agent_skill_promotion_errors, procedural_skill_lineage_errors


def cluster(**overrides):
    row = {
        "agent_id": "agent-a",
        "key": "会员功能怎么用",
        "title": "会员功能怎么用",
        "count": 3,
        "positive_count": 2,
        "negative_count": 0,
        "examples": ["会员功能怎么用", "会员功能如何使用", "怎么使用会员功能"],
        "successful_response_examples": ["先确认会员状态，再说明对应入口。", "先确认套餐，再按 FAQ 给出步骤。"],
        "source_trace_ids": ["t1", "t2", "t3"],
        "positive_trace_ids": ["t1", "t2"],
        "learning_policy": {"propose_skills": True, "requires_human_review": True},
    }
    row.update(overrides)
    return row


def policy():
    return ProceduralPromotionPolicy(
        min_occurrences=3,
        min_positive_feedback=2,
        max_negative_ratio=0.0,
    )


def test_procedural_candidate_requires_repetition_and_positive_evidence() -> None:
    weak = build_procedural_item(cluster(count=2, source_trace_ids=["t1", "t2"]), policy=policy())
    assert weak["promotion_status"] == "blocked"
    assert weak["promotion_reason"] == "insufficient_repetition"

    eligible = build_procedural_item(cluster(), policy=policy())
    assert eligible["promotion_status"] == "eligible"
    assert eligible["promotion_reason"] == "promotion_gate_passed"
    assert eligible["evidence_count"] == 3
    assert eligible["positive_count"] == 2


def test_procedural_candidate_negative_feedback_blocks_promotion() -> None:
    item = build_procedural_item(
        cluster(
            positive_count=2,
            negative_count=1,
            source_trace_ids=["t1", "t2", "t3"],
            positive_trace_ids=["t1", "t2"],
        ),
        policy=policy(),
    )

    assert item["promotion_status"] == "blocked"
    assert item["promotion_reason"] == "negative_feedback_conflict"


def test_procedural_candidate_requires_successful_assistant_response_evidence() -> None:
    item = build_procedural_item(
        cluster(successful_response_examples=["only one response"]),
        policy=policy(),
    )

    assert item["promotion_status"] == "blocked"
    assert item["promotion_reason"] == "successful_response_evidence_missing"


def test_procedural_candidate_requires_agent_identity_and_skill_policy() -> None:
    legacy = build_procedural_item(cluster(agent_id="legacy"), policy=policy())
    disallowed = build_procedural_item(
        cluster(learning_policy={"propose_skills": False, "requires_human_review": True}),
        policy=policy(),
    )

    assert legacy["promotion_reason"] == "agent_identity_required"
    assert disallowed["promotion_reason"] == "learning_policy_disallows_skill_proposal"


def test_procedural_fingerprint_is_agent_scoped_and_stable() -> None:
    first = procedural_fingerprint(agent_id="agent-a", cluster_key="Same Pattern")
    second = procedural_fingerprint(agent_id="agent-a", cluster_key="same pattern")
    other_agent = procedural_fingerprint(agent_id="agent-b", cluster_key="same pattern")

    assert first == second
    assert first != other_agent


def test_procedural_skill_draft_preserves_memory_and_trace_lineage() -> None:
    settings = load_settings(env_path=None)
    item = build_procedural_item(cluster(), policy=policy())
    item.update(
        {
            "procedural_memory_id": 77,
            "source_memory_candidate_ids": [10, 11],
        }
    )

    draft = fallback_skill_draft_from_item(item, settings)

    metadata = draft["metadata"]
    assert metadata["source_kind"] == "procedural_memory"
    assert metadata["procedural_memory_id"] == 77
    assert metadata["procedural_fingerprint"] == item["fingerprint"]
    assert metadata["positive_trace_ids"] == ["t1", "t2"]
    assert metadata["source_memory_candidate_ids"] == [10, 11]
    assert metadata["procedural_evidence"]["positive_count"] == 2
    assert "已验证处理样例" in draft["content"]


def test_procedural_lineage_gate_rejects_forged_or_incomplete_metadata() -> None:
    errors = procedural_skill_lineage_errors(
        {
            "source_kind": "procedural_memory",
            "procedural_memory_id": None,
            "procedural_fingerprint": "",
            "source_agent_id": "legacy",
            "source_trace_ids": ["t1"],
            "positive_trace_ids": ["other"],
            "procedural_evidence": {
                "evidence_count": 1,
                "positive_count": 0,
                "negative_count": -1,
                "negative_ratio": 2.0,
                "promotion_reason": "manual_override",
            },
        }
    )

    assert any("procedural_memory_id" in error for error in errors)
    assert any("procedural_fingerprint" in error for error in errors)
    assert any("source_agent_id" in error for error in errors)
    assert any("source_trace_ids" in error for error in errors)
    assert any("positive_trace_ids" in error for error in errors)
    assert any("promotion gate" in error for error in errors)


def test_procedural_lineage_gate_accepts_reflection_generated_lineage() -> None:
    settings = load_settings(env_path=None)
    item = build_procedural_item(cluster(), policy=policy())
    item.update(
        {
            "procedural_memory_id": 77,
            "source_memory_candidate_ids": [10, 11],
        }
    )
    draft = fallback_skill_draft_from_item(item, settings)

    assert procedural_skill_lineage_errors(draft["metadata"]) == []


def test_procedural_generated_skill_still_requires_eval_canary_and_reviewer() -> None:
    settings = load_settings(env_path=None)
    item = build_procedural_item(cluster(), policy=policy())
    item["procedural_memory_id"] = 77
    draft = fallback_skill_draft_from_item(item, settings)
    draft["content_hash"] = "placeholder"

    errors = agent_skill_promotion_errors(draft)

    assert any("status=passed" in error for error in errors)
    assert any("canary" in error.lower() for error in errors)
    assert any("reviewed_by" in error for error in errors)


class FakeProceduralStore:
    def __init__(self, row_status="candidate"):
        self.row_status = row_status
        self.items = []
        self.marked = []

    def source_memory_candidate_ids(self, trace_ids):
        assert trace_ids == ["t1", "t2", "t3"]
        return [101, 102]

    def upsert(self, item):
        self.items.append(dict(item))
        return {
            "id": 88,
            "status": self.row_status,
            "promoted_skill_name": "existing_skill" if self.row_status == "skill_drafted" else None,
            "skill_draft_id": "draft-existing" if self.row_status == "skill_drafted" else None,
        }

    def mark_skill_drafted(self, **kwargs):
        self.marked.append(kwargs)


def _reflection_messages():
    learning = {
        "request_metadata": {
            "learning_policy": {
                "capture_evidence": True,
                "propose_skills": True,
                "requires_human_review": True,
            }
        },
        "agent_id": "agent-a",
    }
    rows = []
    for index, trace_id in enumerate(["t1", "t2", "t3"], start=1):
        rows.append(
            {
                "role": "assistant",
                "content": f"成功处理步骤 {index}",
                "trace_id": trace_id,
                "metadata": {"agent_id": "agent-a"},
            }
        )
        rows.append(
            {
                "role": "user",
                "content": "会员功能怎么用",
                "trace_id": trace_id,
                "metadata": learning,
            }
        )
    return rows


def _reflection_feedback():
    return [
        {"trace_id": "t1", "rating": "positive"},
        {"trace_id": "t2", "rating": "positive"},
    ]


def _patch_reflection(monkeypatch, store):
    monkeypatch.setattr(reflection_flow, "StrapiClient", lambda _settings: SimpleNamespace())
    monkeypatch.setattr(reflection_flow, "load_messages", lambda *_args, **_kwargs: _reflection_messages())
    monkeypatch.setattr(reflection_flow, "load_feedback", lambda *_args, **_kwargs: _reflection_feedback())
    monkeypatch.setattr(reflection_flow, "ProceduralMemoryStore", lambda _settings: store)
    monkeypatch.setattr(reflection_flow, "load_skill_examples", lambda _settings: [])
    monkeypatch.setenv("DATA_OPS_REFLECTION_CLUSTER_MODE", "rule")


def test_weekly_reflection_promotes_eligible_procedural_memory_to_one_draft(monkeypatch) -> None:
    store = FakeProceduralStore(row_status="candidate")
    _patch_reflection(monkeypatch, store)
    written = []

    def fake_write(_settings, draft):
        written.append(draft)
        return {"documentId": "draft-1", "name": draft["name"]}

    monkeypatch.setattr(reflection_flow, "write_skill_draft", fake_write)
    monkeypatch.setattr(
        reflection_flow,
        "skill_draft_from_item",
        lambda item, settings, *_args: fallback_skill_draft_from_item(item, settings),
    )

    result = reflection_flow.run_weekly_reflection(
        load_settings(env_path=None),
        days=7,
        dry_run=False,
        write_drafts=True,
        min_cluster_size=99,
        min_negative_feedback=99,
        procedural_enabled=True,
        procedural_min_occurrences=3,
        procedural_min_positive_feedback=2,
        procedural_max_negative_ratio=0.0,
    )

    assert result.metrics["procedural_eligible"] == 1
    assert result.metrics["procedural_blocked"] == 0
    assert result.created_skill_draft_ids == ["draft-1"]
    assert len(written) == 1
    assert written[0]["metadata"]["procedural_memory_id"] == 88
    assert written[0]["metadata"]["source_memory_candidate_ids"] == [101, 102]
    assert store.marked == [
        {
            "procedural_memory_id": 88,
            "skill_name": written[0]["name"],
            "skill_draft_id": "draft-1",
        }
    ]


def test_weekly_reflection_does_not_duplicate_already_drafted_procedural_memory(monkeypatch) -> None:
    store = FakeProceduralStore(row_status="skill_drafted")
    _patch_reflection(monkeypatch, store)
    written = []
    monkeypatch.setattr(
        reflection_flow,
        "write_skill_draft",
        lambda *_args, **_kwargs: written.append(True) or {"documentId": "unexpected"},
    )

    result = reflection_flow.run_weekly_reflection(
        load_settings(env_path=None),
        days=7,
        dry_run=False,
        write_drafts=True,
        min_cluster_size=99,
        min_negative_feedback=99,
        procedural_enabled=True,
    )

    assert result.metrics["procedural_eligible"] == 1
    assert written == []
    assert store.marked == []


def test_write_skill_draft_never_bypasses_draft_lifecycle(monkeypatch) -> None:
    settings = load_settings(env_path=None)
    item = build_procedural_item(cluster(), policy=policy())
    item["procedural_memory_id"] = 77
    draft = fallback_skill_draft_from_item(item, settings)
    captured = {}

    class FakeClient:
        def __init__(self, _settings):
            pass

        def upsert(self, endpoint, field, value, payload):
            captured.update(
                {
                    "endpoint": endpoint,
                    "field": field,
                    "value": value,
                    "payload": payload,
                }
            )
            return {"documentId": "draft-77", **payload}

    monkeypatch.setattr(
        "aegora_runtime.data_ops.skill_drafts.StrapiClient",
        FakeClient,
    )

    created = write_skill_draft(settings, draft)

    assert created["lifecycle_status"] == "draft"
    assert captured["payload"]["lifecycle_status"] == "draft"
    assert captured["payload"]["source"] == "agent"
    assert "reviewed_by" not in captured["payload"]


def test_can_create_skill_draft_only_for_unpromoted_candidate() -> None:
    assert can_create_skill_draft(
        {"status": "candidate", "promoted_skill_name": None, "skill_draft_id": None}
    )
    assert not can_create_skill_draft(
        {"status": "skill_drafted", "promoted_skill_name": "x", "skill_draft_id": "1"}
    )
