from __future__ import annotations

from pathlib import Path

from aegora_runtime.skills import skill_content_hash
from scripts.eval_skills import build_evaluation_evidence, build_regression_result


def test_build_evaluation_evidence_binds_dataset_and_candidate(tmp_path: Path) -> None:
    dataset = tmp_path / "eval.jsonl"
    dataset.write_text('{"id":"case-1"}\n', encoding="utf-8")
    skill = {
        "name": "candidate_skill",
        "title": "候选经验",
        "description": "测试候选经验",
        "content": "先澄清，再回答。",
        "product_id": "aicoin",
        "domain": None,
        "skill_type": "guidance",
        "source": "agent",
        "priority": 0,
        "trigger_rules": {},
        "metadata": {"source_agent_id": "agent-1"},
        "id": 1,
        "status": "active",
        "_vector": [1.0, 0.0],
    }
    summary = {
        "total": 1,
        "correct": 1,
        "injection_accuracy": 1.0,
        "cross_product_misinjection_rate": 0.0,
        "inactive_skill_injection_rate": 0.0,
        "commercial_irrelevant_misinjection_rate": 0.0,
        "max_injected_per_turn": 1,
        "commercial_frequency_accuracy": None,
        "commercial_frequency_cases": 0,
    }

    evidence = build_evaluation_evidence(
        summary,
        dataset,
        skill,
        min_injection_accuracy=1.0,
        max_misinjection_rate=0.0,
        baseline_evidence={
            "status": "passed",
            "dataset": "baseline.jsonl@sha256:old",
            "content_hash": "old",
            "metrics": {"injection_accuracy": 1.0},
        },
    )

    expected_skill = {key: value for key, value in skill.items() if key not in {"id", "status", "_vector", "retrieval_score"}}
    assert evidence["status"] == "passed"
    assert evidence["dataset"].startswith("eval.jsonl@sha256:")
    assert evidence["content_hash"] == skill_content_hash(expected_skill)
    assert evidence["criteria"] == {
        "min_injection_accuracy": 1.0,
        "max_misinjection_rate": 0.0,
        "max_accuracy_regression": 0.0,
    }
    assert evidence["regression"]["status"] == "passed"


def test_regression_result_rejects_accuracy_drop() -> None:
    result = build_regression_result(
        {"injection_accuracy": 0.95},
        {"status": "passed", "metrics": {"injection_accuracy": 1.0}, "dataset": "baseline", "content_hash": "abc"},
        max_accuracy_regression=0.01,
    )

    assert result["status"] == "failed"
    assert result["accuracy_delta"] == -0.05


def test_regression_result_rejects_unaccepted_baseline() -> None:
    result = build_regression_result(
        {"injection_accuracy": 1.0},
        {"status": "failed", "metrics": {"injection_accuracy": 1.0}, "dataset": "baseline", "content_hash": "abc"},
        max_accuracy_regression=0.0,
    )

    assert result == {"status": "failed", "reason": "baseline_not_accepted"}
