#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from aegora_runtime.config import load_settings
from aegora_runtime.real_agent import LazyBgeM3Encoder
from aegora_runtime.skills import build_skill_embedding_text, select_skills, skill_content_hash, skill_vector_candidates


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate active Skill retrieval and injection gates.")
    parser.add_argument("dataset", type=Path, nargs="?", default=ROOT / "evals" / "eval_skills.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_skill_eval.json")
    parser.add_argument("--skill-file", type=Path, help="Evaluate unpublished JSON Skills with the real BGE encoder.")
    parser.add_argument("--evidence-output", type=Path, help="Write promotion-ready evaluation evidence for one candidate Skill.")
    parser.add_argument("--min-injection-accuracy", type=float, default=1.0)
    parser.add_argument("--max-misinjection-rate", type=float, default=0.0)
    args = parser.parse_args()
    settings = load_settings(validate_secrets=True)
    encoder = LazyBgeM3Encoder(settings)
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    file_skills = load_file_skills(args.skill_file, encoder) if args.skill_file else None
    details = []
    correct = cross_product_errors = inactive_errors = commercial_false_positives = 0
    max_injected = frequency_correct = frequency_total = 0
    disallow_commercial_total = 0
    for row in rows:
        vector = encoder.encode([row["query"]])[0]
        product_id = row.get("product_id", settings.app.product_id)
        domain_hint = row.get("domain_hint")
        candidates = (
            file_skill_candidates(vector, file_skills, product_id, domain_hint, settings.skills.candidate_k)
            if file_skills is not None
            else skill_vector_candidates(vector, product_id, domain_hint, settings)
        )
        repeated_names = set(row.get("repeated_commercial_skill_names") or [])
        repeated_ids = {int(item["id"]) for item in candidates if item["name"] in repeated_names}
        selected, decisions = select_skills(
            candidates,
            query=row["query"],
            product_id=product_id,
            domain_hint=domain_hint,
            repeated_commercial_ids=repeated_ids,
            settings=settings,
        )
        actual = [item["name"] for item in selected]
        expected = row.get("expected_skill_names") or []
        passed = actual == expected
        correct += int(passed)
        cross_product_errors += int(any(item.get("product_id") not in {None, row.get("product_id", settings.app.product_id)} for item in selected))
        inactive_errors += int(any(item.get("status") not in {None, "active"} for item in selected))
        if not row.get("allow_commercial", False):
            disallow_commercial_total += 1
            commercial_false_positives += int(any(item.get("skill_type") == "commercial" for item in selected))
        max_injected = max(max_injected, len(selected))
        if row.get("frequency_case"):
            frequency_total += 1
            frequency_correct += int(passed)
        details.append(
            {
                "id": row["id"],
                "query": row["query"],
                "expected": expected,
                "actual": actual,
                "passed": passed,
                "decisions": decisions,
            }
        )
    total = len(rows)
    summary = {
        "total": total,
        "correct": correct,
        "injection_accuracy": correct / total if total else 0.0,
        "cross_product_misinjection_rate": cross_product_errors / total if total else 0.0,
        "inactive_skill_injection_rate": inactive_errors / total if total else 0.0,
        "commercial_irrelevant_misinjection_rate": (
            commercial_false_positives / disallow_commercial_total if disallow_commercial_total else 0.0
        ),
        "max_injected_per_turn": max_injected,
        "commercial_frequency_accuracy": frequency_correct / frequency_total if frequency_total else None,
        "commercial_frequency_cases": frequency_total,
        "details": details,
    }
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.evidence_output:
        if not args.skill_file or file_skills is None or len(file_skills) != 1:
            raise ValueError("--evidence-output 必须与仅包含一条 Skill 的 --skill-file 一起使用")
        evidence = build_evaluation_evidence(
            summary,
            args.dataset,
            file_skills[0],
            min_injection_accuracy=args.min_injection_accuracy,
            max_misinjection_rate=args.max_misinjection_rate,
        )
        args.evidence_output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "details"}, ensure_ascii=False, indent=2))


def load_file_skills(path: Path, encoder: LazyBgeM3Encoder) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        rows = [rows]
    vectors = encoder.encode([build_skill_embedding_text(row) for row in rows])
    return [{**row, "id": index, "status": "active", "_vector": vector} for index, (row, vector) in enumerate(zip(rows, vectors), 1)]


def file_skill_candidates(query_vector: list[float], skills: list[dict], product_id: str, domain_hint: str | None, limit: int) -> list[dict]:
    candidates = []
    for skill in skills:
        if skill.get("product_id") not in {None, product_id}:
            continue
        if domain_hint is None and skill.get("domain") is not None:
            continue
        if domain_hint is not None and skill.get("domain") not in {None, domain_hint}:
            continue
        candidates.append({**skill, "retrieval_score": cosine_similarity(query_vector, skill["_vector"])})
    candidates.sort(key=lambda item: item["retrieval_score"], reverse=True)
    return candidates[:limit]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def build_evaluation_evidence(
    summary: dict,
    dataset: Path,
    skill: dict,
    *,
    min_injection_accuracy: float,
    max_misinjection_rate: float,
) -> dict:
    """Bind promotion evidence to the exact candidate content and dataset bytes."""
    dataset_hash = hashlib.sha256(dataset.read_bytes()).hexdigest()
    metrics = {
        key: summary[key]
        for key in [
            "total",
            "correct",
            "injection_accuracy",
            "cross_product_misinjection_rate",
            "inactive_skill_injection_rate",
            "commercial_irrelevant_misinjection_rate",
            "max_injected_per_turn",
            "commercial_frequency_accuracy",
            "commercial_frequency_cases",
        ]
    }
    passed = (
        metrics["injection_accuracy"] >= min_injection_accuracy
        and metrics["cross_product_misinjection_rate"] <= max_misinjection_rate
        and metrics["inactive_skill_injection_rate"] <= max_misinjection_rate
        and metrics["commercial_irrelevant_misinjection_rate"] <= max_misinjection_rate
    )
    clean_skill = {key: value for key, value in skill.items() if key not in {"id", "status", "_vector", "retrieval_score"}}
    return {
        "status": "passed" if passed else "failed",
        "dataset": f"{dataset.name}@sha256:{dataset_hash}",
        "content_hash": skill_content_hash(clean_skill),
        "metrics": metrics,
        "criteria": {
            "min_injection_accuracy": min_injection_accuracy,
            "max_misinjection_rate": max_misinjection_rate,
        },
        "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


if __name__ == "__main__":
    main()
