#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.embeddings import build_encoder
from aegora_runtime.retrieval import hybrid_retrieve


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate fulltext + vector RRF retrieval.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "evals" / "eval_retrieval_sample_300.jsonl",
    )
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--candidate-k", type=int)
    parser.add_argument("--rrf-k", type=int)
    parser.add_argument("--tie-break-source", choices=["fulltext", "vector"])
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_rrf_eval.json")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    top_k = args.top_k or settings.retrieval.top_k
    candidate_k = args.candidate_k or settings.retrieval.candidate_k
    rrf_k = args.rrf_k or settings.retrieval.rrf_k
    tie_break_source = args.tie_break_source or settings.retrieval.rrf_tie_break_source
    rows = [row for row in read_jsonl(args.dataset) if row.get("expected_faq_ids")]

    encoder = build_encoder(replace(settings.embedding, batch_size=args.batch_size))
    summary = evaluate(args.dataset.name, rows, encoder, settings, top_k, candidate_k, rrf_k, tie_break_source)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote={args.output}")


def evaluate(
    name: str,
    rows: list[dict[str, Any]],
    encoder,
    settings,
    top_k: int,
    candidate_k: int,
    rrf_k: int,
    tie_break_source: str,
) -> dict[str, Any]:
    encode_started = time.perf_counter()
    vectors = encoder.encode([row["query"] for row in rows])
    encode_ms = (time.perf_counter() - encode_started) * 1000

    top1 = recall = 0
    mrr_sum = 0.0
    latencies = []
    failures = []
    for row, vector in zip(rows, vectors, strict=True):
        started = time.perf_counter()
        results = hybrid_retrieve(
            row["query"],
            vector,
            top_k=top_k,
            candidate_k=candidate_k,
            rrf_k=rrf_k,
            tie_break_source=tie_break_source,
            settings=settings,
        )
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
                            "rrf_score": round(float(item["score"]), 6),
                            "source_ranks": item["source_ranks"],
                        }
                        for item in results
                    ],
                }
            )

    count = len(rows)
    return {
        "dataset": name,
        "fusion_method": "rrf",
        "rrf_k": rrf_k,
        "tie_break_source": tie_break_source,
        "candidate_k": candidate_k,
        "top_k": top_k,
        "total": count,
        "recall_at_k": round(recall / count, 4),
        "top1_accuracy": round(top1 / count, 4),
        "mrr_at_k": round(mrr_sum / count, 4),
        "query_encoding": {
            "total_ms": round(encode_ms, 2),
            "mean_ms": round(encode_ms / count, 2),
        },
        "hybrid_search": {
            "mean_ms": round(sum(latencies) / count, 2),
            "p95_ms": round(percentile(sorted(latencies), 95), 2),
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
