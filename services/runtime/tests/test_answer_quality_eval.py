from __future__ import annotations

import pytest

from scripts.eval_answer_quality import (
    JsonlCache,
    QualityStats,
    apply_judgement,
    deterministic_no_evidence_judgement,
    should_score,
    make_cache_key,
    normalize_judgement,
)


def test_no_evidence_judgement_fails_grounding_and_hallucinates() -> None:
    judgement = deterministic_no_evidence_judgement({"expected_key_points": ["入口", "按钮"]})

    assert judgement["grounded"] is False
    assert judgement["hallucination"] is True
    assert judgement["key_points_total"] == 2
    assert judgement["key_points_covered"] == 0


def test_normalize_judgement_rejects_invalid_key_point_counts() -> None:
    with pytest.raises(ValueError):
        normalize_judgement({"key_points_total": 2, "key_points_covered": 3}, 2)


def test_quality_stats_uses_judged_denominator() -> None:
    stats = QualityStats(dataset="demo")
    stats.total = 3
    stats.answer_scored = 2
    stats.judge_errors = 1
    apply_judgement(
        stats,
        {"id": "1", "query": "q", "must_include": ["入口"]},
        {"route": "faq_answer", "answer": "a", "retrieved_faqs": [{"faq_id": 1}]},
        {
            "grounded": True,
            "hallucination": False,
            "key_points_total": 1,
            "key_points_covered": 1,
            "answer_quality_score": 4,
            "missing_key_points": [],
            "unsupported_claims": [],
            "risk_level": "none",
            "reason": "ok",
        },
        include_details=False,
    )

    summary = stats.as_dict()
    assert summary["grounded_rate"] == 1.0
    assert summary["hallucination_rate"] == 0.0
    assert summary["key_step_coverage"] == 1.0
    assert summary["answer_quality_score_avg"] == 4.0
    assert summary["answer_quality_pass_rate"] == 1.0
    assert summary["low_quality_rate"] == 0.0
    assert summary["judge_errors"] == 1


def test_normalize_judgement_rejects_invalid_answer_quality_score() -> None:
    with pytest.raises(ValueError):
        normalize_judgement(
            {
                "grounded": True,
                "hallucination": False,
                "key_points_total": 0,
                "key_points_covered": 0,
                "answer_quality_score": 6,
            },
            0,
        )


def test_low_answer_quality_is_sampled() -> None:
    stats = QualityStats(dataset="demo")
    apply_judgement(
        stats,
        {"id": "1", "query": "q", "must_include": []},
        {"route": "faq_answer", "answer": "a", "retrieved_faqs": [{"faq_id": 1}]},
        {
            "grounded": True,
            "hallucination": False,
            "key_points_total": 0,
            "key_points_covered": 0,
            "answer_quality_score": 2,
            "missing_key_points": [],
            "unsupported_claims": [],
            "risk_level": "low",
            "reason": "表达不可用",
        },
        include_details=False,
    )

    summary = stats.as_dict()
    assert summary["answer_quality_pass_rate"] == 0.0
    assert summary["low_quality_rate"] == 1.0
    assert stats.samples[0]["judgement"]["answer_quality_score"] == 2


def test_cache_key_uses_scoring_inputs_only() -> None:
    row = {"id": "1", "query": "如何设置悬浮窗", "history": [], "must_include": ["入口"], "weight": 1}
    changed_weight = {**row, "weight": 2}

    assert make_cache_key(row, "real", "planner") == make_cache_key(changed_weight, "real", "planner")


def test_jsonl_cache_roundtrip_and_dedup(tmp_path) -> None:
    cache = JsonlCache(tmp_path / "cache.jsonl")
    item = {"key": "k1", "result": {"route": "faq_answer"}, "skipped": False}

    cache.put(item)
    cache.put(item)

    reloaded = JsonlCache(tmp_path / "cache.jsonl")
    assert reloaded.get("k1")["result"]["route"] == "faq_answer"
    assert len((tmp_path / "cache.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_handoff_audit_tool_is_not_answer_quality_evidence() -> None:
    result = {
        "route": "handoff",
        "answer": "请补充账号信息。",
        "tool_observations": [{"tool_name": "record_handoff", "status": "ok"}],
    }

    assert should_score(result) is False


def test_lookup_faq_detail_is_answer_quality_evidence() -> None:
    result = {
        "route": "tool_call",
        "answer": "按 FAQ 操作。",
        "tool_observations": [{"tool_name": "lookup_faq_detail", "status": "ok", "output": {"faq_id": 1}}],
    }

    assert should_score(result) is True
