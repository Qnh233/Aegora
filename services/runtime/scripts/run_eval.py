#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegora_runtime.agent_loop import AgentRequest, run_agent
from aegora_runtime.config import load_settings
from aegora_runtime.demo_agent import build_demo_dependencies
from aegora_runtime.real_agent import build_real_dependencies


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASETS = [
    ROOT / "evals" / "eval_core_100.jsonl",
    ROOT / "evals" / "eval_retrieval_sample_300.jsonl",
    ROOT / "evals" / "eval_bad_feedback_100.jsonl",
    ROOT / "evals" / "eval_security_50.jsonl",
]


@dataclass
class EvalStats:
    name: str
    total: int = 0
    route_scored: int = 0
    route_correct: int = 0
    route_correct_normalized: int = 0
    faq_scored: int = 0
    recall_at_5: int = 0
    top1: int = 0
    mrr_sum: float = 0.0
    answer_scored: int = 0
    must_include_pass: int = 0
    must_not_include_scored: int = 0
    must_not_include_pass: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.name,
            "total": self.total,
            "route_accuracy": pct(self.route_correct, self.route_scored),
            "route_accuracy_normalized": pct(self.route_correct_normalized, self.route_scored),
            "faq_recall_at_5": pct(self.recall_at_5, self.faq_scored),
            "faq_top1": pct(self.top1, self.faq_scored),
            "faq_mrr_at_5": round(self.mrr_sum / self.faq_scored, 4) if self.faq_scored else None,
            "must_include_pass": pct(self.must_include_pass, self.answer_scored),
            "must_not_include_pass": pct(self.must_not_include_pass, self.must_not_include_scored),
            "scored": {
                "route": self.route_scored,
                "faq": self.faq_scored,
                "must_include": self.answer_scored,
                "must_not_include": self.must_not_include_scored,
            },
            "sample_failures": self.failures[:10],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate current demo Agent against JSONL eval sets.")
    parser.add_argument("datasets", nargs="*", type=Path, default=DEFAULT_DATASETS)
    parser.add_argument("--agent", choices=["demo", "real"], default="real")
    parser.add_argument("--loop-mode", choices=["planner"], default=None)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_demo_eval.json")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    deps = build_real_dependencies(settings) if args.agent == "real" else build_demo_dependencies(settings)

    summaries = []
    for dataset in args.datasets:
        rows = read_jsonl(dataset)
        if args.limit:
            rows = rows[: args.limit]
        loop_mode = args.loop_mode or settings.agent.loop_mode
        stats = evaluate_dataset(f"{args.agent}:{loop_mode}:{dataset.name}", rows, settings, deps, loop_mode)
        summaries.append(stats.as_dict())
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))

    args.output.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={args.output}")


def evaluate_dataset(name: str, rows: list[dict[str, Any]], settings, deps, loop_mode: str | None = None) -> EvalStats:
    stats = EvalStats(name=name)
    for row in rows:
        stats.total += 1
        try:
            result = run_agent(
                AgentRequest(
                    query=row["query"],
                    session_id=f"eval-{name}-{row['id']}",
                    history=row.get("history") or [],
                ),
                deps,
                settings,
                loop_mode=loop_mode,
                attach_hooks=False,
                enable_pocoflow_db=False,
            )
        except Exception as exc:
            result = {
                "status": "error",
                "route": None,
                "answer": "",
                "retrieved_faqs": [],
                "decision": {"reason": f"{type(exc).__name__}: {exc}"},
            }
        score_row(stats, row, result)
    return stats


def score_row(stats: EvalStats, row: dict[str, Any], result: dict[str, Any]) -> None:
    expected_route = row.get("expected_route")
    actual_route = result.get("route")
    normalized_actual = normalize_route(actual_route, result)
    normalized_expected = normalize_expected_route(expected_route)

    if expected_route != "not_scored":
        stats.route_scored += 1
        if actual_route == expected_route:
            stats.route_correct += 1
        if normalized_actual == normalized_expected:
            stats.route_correct_normalized += 1
        elif len(stats.failures) < 20:
            stats.failures.append(
                {
                    "id": row["id"],
                    "query": row["query"][:120],
                    "kind": "route",
                    "expected": expected_route,
                    "actual": actual_route,
                    "normalized_actual": normalized_actual,
                    "answer": (result.get("answer") or "")[:120],
                }
            )

    expected_ids = [int(item) for item in row.get("expected_faq_ids") or []]
    if expected_ids:
        stats.faq_scored += 1
        retrieved_ids = [int(item["faq_id"]) for item in result.get("retrieved_faqs") or [] if item.get("faq_id")]
        hit_positions = [idx for idx, faq_id in enumerate(retrieved_ids[:5], start=1) if faq_id in expected_ids]
        if hit_positions:
            stats.recall_at_5 += 1
            stats.mrr_sum += 1.0 / hit_positions[0]
        if retrieved_ids[:1] and retrieved_ids[0] in expected_ids:
            stats.top1 += 1
        elif len(stats.failures) < 20:
            stats.failures.append(
                {
                    "id": row["id"],
                    "query": row["query"][:120],
                    "kind": "retrieval",
                    "expected_faq_ids": expected_ids,
                    "retrieved_ids": retrieved_ids[:5],
                }
            )

    must_include = row.get("must_include") or []
    if must_include:
        stats.answer_scored += 1
        answer = result.get("answer") or ""
        if all(contains(answer, item) for item in must_include):
            stats.must_include_pass += 1
        elif len(stats.failures) < 20:
            stats.failures.append(
                {
                    "id": row["id"],
                    "query": row["query"][:120],
                    "kind": "must_include",
                    "missing": [item for item in must_include if not contains(answer, item)],
                    "answer": answer[:160],
                }
            )

    must_not_include = row.get("must_not_include") or []
    if must_not_include:
        stats.must_not_include_scored += 1
        answer = result.get("answer") or ""
        if all(not contains(answer, item) for item in must_not_include):
            stats.must_not_include_pass += 1
        elif len(stats.failures) < 20:
            stats.failures.append(
                {
                    "id": row["id"],
                    "query": row["query"][:120],
                    "kind": "must_not_include",
                    "violated": [item for item in must_not_include if contains(answer, item)],
                    "answer": answer[:160],
                }
            )


def normalize_expected_route(route: str | None) -> str | None:
    if route == "safe_reject_or_neutral_answer":
        return "safe"
    return route


def normalize_route(route: str | None, result: dict[str, Any]) -> str | None:
    decision = result.get("decision") or {}
    if decision.get("reason") == "unsafe_input":
        return "safe"
    return route


def contains(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    main()
