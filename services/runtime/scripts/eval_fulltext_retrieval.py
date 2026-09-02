#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.retrieval import fulltext_retrieve


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate PostgreSQL fulltext retrieval.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "evals" / "eval_retrieval_sample_300.jsonl",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_fulltext_eval.json")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    rows = [row for row in read_jsonl(args.dataset) if row.get("expected_faq_ids")]
    summary = evaluate(args.dataset.name, rows, settings, args.top_k)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote={args.output}")


def evaluate(name: str, rows: list[dict[str, Any]], settings, top_k: int) -> dict[str, Any]:
    top1 = recall = 0
    mrr_sum = 0.0
    latencies = []
    failures = []
    for row in rows:
        started = time.perf_counter()
        results = fulltext_retrieve(row["query"], top_k, settings)
        latencies.append((time.perf_counter() - started) * 1000)
        result_ids = [int(item["faq_id"]) for item in results]
        expected_ids = [int(item) for item in row["expected_faq_ids"]]
        positions = [index for index, faq_id in enumerate(result_ids, start=1) if faq_id in expected_ids]
        if positions:
            recall += 1
            mrr_sum += 1.0 / positions[0]
        if result_ids[:1] and result_ids[0] in expected_ids:
            top1 += 1
        elif len(failures) < 20:
            failures.append(
                {
                    "id": row["id"],
                    "query": row["query"],
                    "expected_faq_ids": expected_ids,
                    "results": [
                        {
                            "faq_id": item["faq_id"],
                            "title": item["title"],
                            "score": round(float(item["score"]), 6),
                        }
                        for item in results
                    ],
                }
            )

    count = len(rows)
    ordered_latencies = sorted(latencies)
    return {
        "dataset": name,
        "total": count,
        "top_k": top_k,
        "recall_at_k": round(recall / count, 4),
        "top1_accuracy": round(top1 / count, 4),
        "mrr_at_k": round(mrr_sum / count, 4),
        "search": {
            "mean_ms": round(sum(latencies) / count, 2),
            "p95_ms": round(percentile(ordered_latencies, 95), 2),
            "max_ms": round(max(latencies), 2),
        },
        "sample_top1_failures": failures,
    }


def percentile(values: list[float], p: int) -> float:
    rank = (len(values) - 1) * p / 100
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    weight = rank - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    main()

