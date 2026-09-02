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
from aegora_runtime.retrieval import vector_retrieve


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASETS = [
    ROOT / "evals" / "eval_core_100.jsonl",
    ROOT / "evals" / "eval_retrieval_sample_300.jsonl",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate BGE-M3 vector retrieval.")
    parser.add_argument("datasets", nargs="*", type=Path, default=DEFAULT_DATASETS)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_vector_eval.json")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    embedding_settings = replace(settings.embedding, batch_size=args.batch_size)
    encoder = build_encoder(embedding_settings)

    summaries = []
    for dataset in args.datasets:
        rows = [row for row in read_jsonl(dataset) if row.get("expected_faq_ids")]
        summary = evaluate(dataset.name, rows, encoder, settings, args.top_k)
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    args.output.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={args.output}")


def evaluate(name: str, rows: list[dict[str, Any]], encoder, settings, top_k: int) -> dict[str, Any]:
    started = time.perf_counter()
    vectors = encoder.encode([row["query"] for row in rows])
    encode_ms = (time.perf_counter() - started) * 1000

    top1 = recall = 0
    mrr_sum = 0.0
    search_latencies = []
    failures = []
    for row, vector in zip(rows, vectors, strict=True):
        search_started = time.perf_counter()
        results = vector_retrieve(vector, top_k, settings)
        search_latencies.append((time.perf_counter() - search_started) * 1000)
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
    return {
        "dataset": name,
        "total": count,
        "top_k": top_k,
        "recall_at_k": round(recall / count, 4) if count else None,
        "top1_accuracy": round(top1 / count, 4) if count else None,
        "mrr_at_k": round(mrr_sum / count, 4) if count else None,
        "query_encoding": {
            "total_ms": round(encode_ms, 2),
            "mean_ms": round(encode_ms / count, 2) if count else None,
        },
        "vector_search": {
            "mean_ms": round(sum(search_latencies) / count, 2) if count else None,
            "p95_ms": round(percentile(sorted(search_latencies), 95), 2) if count else None,
            "max_ms": round(max(search_latencies), 2) if count else None,
        },
        "sample_top1_failures": failures,
    }


def percentile(values: list[float], p: int) -> float:
    if len(values) == 1:
        return values[0]
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
