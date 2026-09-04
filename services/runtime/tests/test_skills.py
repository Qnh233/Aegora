from __future__ import annotations

import json
from pathlib import Path

from aegora_runtime.config import load_settings
from aegora_runtime.skills import (
    agent_skill_promotion_errors,
    build_skill_embedding_text,
    select_skills,
    skill_index_item,
    validate_skill,
)
from aegora_runtime.data_ops import reflection_flow
from aegora_runtime.data_ops.reflection_flow import build_report_item, cluster_key, fallback_skill_draft_from_item
from apps import data_ops_app


def candidate(**overrides):
    row = {
        "id": 1,
        "name": "membership_info",
        "title": "会员权益介绍",
        "description": "购买会员时介绍权益",
        "content": "根据用户需求介绍会员权益，但不要编造价格。",
        "product_id": "aicoin",
        "domain": "membership",
        "skill_type": "commercial",
        "source": "manual",
        "priority": 20,
        "trigger_rules": {
            "strong_related_terms": ["功能受限"],
            "exclude_terms": ["投诉"],
        },
        "retrieval_score": 0.8,
    }
    row.update(overrides)
    return row


def test_validate_skill_rejects_domain_without_product() -> None:
    row = candidate(product_id=None)

    assert "domain 有值时 product_id 不能为空" in validate_skill(row, 1000)


def test_validate_skill_rejects_long_content() -> None:
    assert any("不能超过" in error for error in validate_skill(candidate(content="x" * 1001), 1000))


def test_agent_skill_promotion_requires_auditable_evaluation() -> None:
    row = candidate(source="agent", metadata={})
    errors = agent_skill_promotion_errors(row)

    assert any("status=passed" in error for error in errors)
    assert any("evaluation.dataset" in error for error in errors)
    assert any("evaluation.metrics" in error for error in errors)
    assert any("evaluation.evaluated_at" in error for error in errors)
    assert any("reviewed_by" in error for error in errors)


def test_agent_skill_promotion_accepts_passed_evaluation_and_manual_skills() -> None:
    evaluated = candidate(
        source="agent",
        reviewed_by="reviewer-1",
        metadata={
            "evaluation": {
                "status": "passed",
                "dataset": "eval_skills.jsonl@sha256:abc",
                "metrics": {"accuracy": 1.0, "regressions": 0},
                "evaluated_at": "2026-09-04T03:00:00Z",
            }
        },
    )

    assert agent_skill_promotion_errors(evaluated) == []
    assert agent_skill_promotion_errors(candidate(source="manual", metadata={})) == []


def test_commercial_skill_requires_explicit_or_strong_related_trigger() -> None:
    settings = load_settings(env_path=None)

    selected, decisions = select_skills(
        [candidate()],
        query="如何设置悬浮窗",
        product_id="aicoin",
        domain_hint="membership",
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert selected == []
    assert decisions[0]["decision"] == "commercial_gate_not_matched"


def test_commercial_skill_injects_for_explicit_purchase_intent() -> None:
    settings = load_settings(env_path=None)

    selected, _ = select_skills(
        [candidate()],
        query="会员怎么买，有什么权益",
        product_id="aicoin",
        domain_hint="membership",
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert [item["name"] for item in selected] == ["membership_info"]


def test_commercial_frequency_limit_allows_repeated_explicit_intent() -> None:
    settings = load_settings(env_path=None)
    selected, _ = select_skills(
        [candidate()],
        query="功能受限了",
        product_id="aicoin",
        domain_hint="membership",
        repeated_commercial_ids={1},
        settings=settings,
    )
    explicit, _ = select_skills(
        [candidate()],
        query="我想购买会员",
        product_id="aicoin",
        domain_hint="membership",
        repeated_commercial_ids={1},
        settings=settings,
    )

    assert selected == []
    assert explicit


def test_commercial_strong_related_trigger_records_specific_reason() -> None:
    settings = load_settings(env_path=None)
    selected, _ = select_skills(
        [candidate()],
        query="功能受限了",
        product_id="aicoin",
        domain_hint="membership",
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert selected[0]["injection_reason"] == "matched_strong_related_trigger"


def test_exclude_term_blocks_skill() -> None:
    settings = load_settings(env_path=None)
    selected, decisions = select_skills(
        [candidate()],
        query="我要投诉会员购买问题",
        product_id="aicoin",
        domain_hint="membership",
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert selected == []
    assert decisions[0]["decision"] == "excluded_by_term"


def test_manual_skill_beats_agent_skill_with_same_score() -> None:
    settings = load_settings(env_path=None)
    manual = candidate(id=1, domain=None, skill_type="guidance", trigger_rules={}, source="manual", priority=0)
    agent = candidate(id=2, name="agent_tip", domain=None, skill_type="guidance", trigger_rules={}, source="agent", priority=0)

    selected, _ = select_skills(
        [agent, manual],
        query="会员权益",
        product_id="aicoin",
        domain_hint=None,
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert selected[0]["id"] == 1


def test_cross_product_and_inactive_skills_are_blocked_defensively() -> None:
    settings = load_settings(env_path=None)
    selected, decisions = select_skills(
        [
            candidate(id=1, product_id="other-product"),
            candidate(id=2, status="draft"),
        ],
        query="购买会员",
        product_id="aicoin",
        domain_hint=None,
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert selected == []
    assert {item["decision"] for item in decisions} == {"cross_product_scope", "inactive_status"}


def test_domain_skill_requires_matching_domain_hint() -> None:
    settings = load_settings(env_path=None)
    selected, decisions = select_skills(
        [candidate(skill_type="guidance", trigger_rules={})],
        query="会员权益",
        product_id="aicoin",
        domain_hint=None,
        repeated_commercial_ids=set(),
        settings=settings,
    )

    assert selected == []
    assert decisions[0]["decision"] == "domain_hint_missing"


def test_embedding_text_excludes_full_content() -> None:
    text = build_skill_embedding_text(candidate(content="不应进入向量文本"))

    assert "不应进入向量文本" not in text
    assert "会员权益介绍" in text


def test_reflection_cluster_promotes_negative_feedback_to_skill_candidate() -> None:
    item = build_report_item(
        {
            "title": "会员权益怎么判断",
            "count": 2,
            "negative_count": 1,
            "examples": ["会员权益怎么判断"],
            "source_trace_ids": ["t1"],
            "learning_policy": {"propose_skills": True, "requires_human_review": True},
        },
        min_negative_feedback=1,
    )

    assert item["type"] == "skill_candidate"
    assert cluster_key("会员权益怎么判断？") == cluster_key("会员权益怎么判断")


def test_reflection_learning_policy_blocks_unapproved_evidence_and_drafts() -> None:
    rows = [
        {
            "role": "user",
            "content": "允许进入学习报告",
            "trace_id": "allowed",
            "metadata": {
                "agent_id": "agent-a",
                "request_metadata": {
                    "learning_policy": {
                        "capture_evidence": True,
                        "propose_skills": False,
                        "requires_human_review": True,
                    }
                },
            },
        },
        {
            "role": "user",
            "content": "禁止采集",
            "trace_id": "blocked",
            "metadata": {
                "agent_id": "agent-a",
                "request_metadata": {
                    "learning_policy": {
                        "capture_evidence": False,
                        "propose_skills": True,
                    }
                },
            },
        },
    ]

    clusters = reflection_flow.cluster_user_messages_by_rule(rows, {"allowed", "blocked"})
    assert len(clusters) == 1
    assert clusters[0]["source_trace_ids"] == ["allowed"]

    item = build_report_item(clusters[0], min_negative_feedback=1)
    assert item["type"] == "learning_review"
    assert item["learning_policy"]["propose_skills"] is False


def test_data_ops_uses_global_lock(monkeypatch) -> None:
    lock_names = []

    class DummyLock:
        def __enter__(self):
            return True

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(data_ops_app, "advisory_job_lock", lambda _settings, name: lock_names.append(name) or DummyLock())
    monkeypatch.setattr(data_ops_app, "save_report", lambda _settings, report: report)
    monkeypatch.setattr(data_ops_app, "log_report", lambda report, **_kwargs: None)

    data_ops_app.run_reported_job(None, "sync_content", lambda: {}, summary_prefix="sync")

    assert lock_names == ["global"]


def test_reflection_skill_draft_hides_generation_noise() -> None:
    settings = load_settings(env_path=None)
    draft = fallback_skill_draft_from_item(
        {
            "cluster_title": "你好，指标胜率这个会员也是添加时间级别的这个功能吗",
            "examples": ["你好，指标胜率这个会员也是添加时间级别的这个功能吗"],
            "source_trace_ids": ["t1"],
            "suggestion": "建议生成 Skill 草稿，沉淀客服处理策略。",
        },
        settings,
    )

    visible = "\n".join([draft["name"], draft["title"], draft["description"], draft["content"]])
    assert "reflection_" not in draft["name"]
    assert "反思经验" not in visible
    assert "生成原因" not in visible
    assert "近 7 天" not in visible


def test_reflection_embedding_cluster_falls_back_to_semantic_groups(monkeypatch) -> None:
    class FakeEncoder:
        def encode(self, texts):
            return [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]

    monkeypatch.setattr(reflection_flow, "build_encoder", lambda _settings: FakeEncoder())
    settings = load_settings(env_path=None)
    rows = [
        {"role": "user", "content": "会员怎么买", "trace_id": "a"},
        {"role": "user", "content": "VIP如何购买", "trace_id": "b"},
        {"role": "user", "content": "交易所授权", "trace_id": "c"},
    ]

    clusters = reflection_flow.cluster_user_messages(rows, {"b"}, settings)

    assert [item["count"] for item in clusters] == [2, 1]
    assert clusters[0]["negative_count"] == 1


def test_reflection_never_clusters_evidence_across_agents(monkeypatch) -> None:
    class FakeEncoder:
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(reflection_flow, "build_encoder", lambda _settings: FakeEncoder())
    settings = load_settings(env_path=None)
    rows = [
        {
            "role": "user",
            "content": "同一个会员问题",
            "trace_id": "a",
            "metadata": {"agent_id": "agent-a"},
        },
        {
            "role": "user",
            "content": "同一个会员问题",
            "trace_id": "b",
            "metadata": {"agent_id": "agent-b"},
        },
    ]

    clusters = reflection_flow.cluster_user_messages(rows, {"a", "b"}, settings)

    assert len(clusters) == 2
    assert {item["agent_id"] for item in clusters} == {"agent-a", "agent-b"}
    assert all(item["count"] == 1 for item in clusters)


def test_reflection_skill_draft_preserves_evidence_lineage() -> None:
    settings = load_settings(env_path=None)
    draft = fallback_skill_draft_from_item(
        {
            "agent_id": "agent-a",
            "cluster_title": "会员功能咨询",
            "examples": ["会员功能怎么用"],
            "source_trace_ids": ["trace-1", "trace-2"],
        },
        settings,
    )

    assert draft["metadata"] == {
        "source_agent_id": "agent-a",
        "source_trace_ids": ["trace-1", "trace-2"],
    }


def test_skill_index_excludes_content_and_trigger_rules() -> None:
    index = skill_index_item(candidate())

    assert index["name"] == "membership_info"
    assert "content" not in index
    assert "trigger_rules" not in index


def test_aicoin_legacy_skill_gates_match_eval_cases() -> None:
    root = Path(__file__).resolve().parents[1]
    skills = json.loads((root / "data/skills/aicoin_legacy_experiences.json").read_text(encoding="utf-8"))
    cases = [
        json.loads(line)
        for line in (root / "evals/eval_skills_aicoin_legacy.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    candidates = [
        {
            **skill,
            "id": index,
            "status": "active",
            "retrieval_score": 0.9,
        }
        for index, skill in enumerate(skills, start=1)
    ]
    settings = load_settings(env_path=None)

    for case in cases:
        selected, _ = select_skills(
            candidates,
            query=case["query"],
            product_id=case["product_id"],
            domain_hint=case.get("domain_hint"),
            repeated_commercial_ids=set(),
            settings=settings,
        )
        assert [item["name"] for item in selected] == case["expected_skill_names"], case["id"]
