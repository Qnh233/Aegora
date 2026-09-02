#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.embeddings import build_encoder
from aegora_runtime.retrieval import hybrid_retrieve, vector_retrieve


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate SiliconFlow BGE-M3 compatibility with local vectors.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "evals" / "eval_retrieval_sample_300.jsonl",
    )
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--compare-limit", type=int, default=20)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_embedding_compatibility.json")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    api_key = settings.embedding.api_key or os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise ValueError("配置 EMBEDDING_API_KEY 后才能运行硅基流动兼容性评估")

    rows = [
        row
        for row in read_jsonl(args.dataset)
        if row.get("expected_faq_ids")
    ][: args.limit]
    queries = [row["query"] for row in rows]
    remote_settings = replace(settings.embedding, provider="siliconflow", api_key=api_key)
    remote = build_encoder(remote_settings)

    started = time.perf_counter()
    remote_vectors = remote.encode(queries)
    remote_ms = (time.perf_counter() - started) * 1000

    local = build_encoder(replace(settings.embedding, provider="local"))
    local_vectors = local.encode(queries)
    pairwise = [
        cosine_similarity(local_vector, remote_vector)
        for local_vector, remote_vector in zip(
            local_vectors[: args.compare_limit],
            remote_vectors[: args.compare_limit],
            strict=True,
        )
    ]

    local_vector_metrics = retrieval_metrics(rows, local_vectors, settings, hybrid=False)
    local_rrf_metrics = retrieval_metrics(rows, local_vectors, settings, hybrid=True)
    vector_metrics = retrieval_metrics(rows, remote_vectors, settings, hybrid=False)
    rrf_metrics = retrieval_metrics(rows, remote_vectors, settings, hybrid=True)
    vector_delta = metric_delta(vector_metrics, local_vector_metrics)
    rrf_delta = metric_delta(rrf_metrics, local_rrf_metrics)
    result = {
        "remote_provider": "siliconflow",
        "model": settings.embedding.model,
        "dataset": args.dataset.name,
        "total": len(rows),
        "remote_encoding": {
            "total_ms": round(remote_ms, 2),
            "mean_ms": round(remote_ms / len(rows), 2) if rows else None,
        },
        "same_text_local_remote_cosine": {
            "count": len(pairwise),
            "mean": round(sum(pairwise) / len(pairwise), 6) if pairwise else None,
            "min": round(min(pairwise), 6) if pairwise else None,
            "max": round(max(pairwise), 6) if pairwise else None,
        },
        "local_query_against_current_document_vectors": local_vector_metrics,
        "remote_query_against_current_document_vectors": vector_metrics,
        "remote_minus_local_vector_metrics": vector_delta,
        "local_query_rrf_against_current_document_vectors": local_rrf_metrics,
        "remote_query_rrf_against_current_document_vectors": rrf_metrics,
        "remote_minus_local_rrf_metrics": rrf_delta,
        "compatible_without_reembedding": (
            min(pairwise, default=0.0) >= 0.999
            and min(vector_delta.values(), default=-1.0) >= -0.005
            and min(rrf_delta.values(), default=-1.0) >= -0.005
        ),
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"wrote={args.output}")


def retrieval_metrics(rows: list[dict[str, Any]], vectors: list[list[float]], settings, *, hybrid: bool) -> dict[str, Any]:
    recall = top1 = 0
    mrr_sum = 0.0
    failures = []
    for row, vector in zip(rows, vectors, strict=True):
        results = hybrid_retrieve(row["query"], vector, settings=settings) if hybrid else vector_retrieve(vector, 5, settings)
        result_ids = [int(item["faq_id"]) for item in results]
        expected = {int(item) for item in row["expected_faq_ids"]}
        positions = [index for index, faq_id in enumerate(result_ids[:5], start=1) if faq_id in expected]
        if positions:
            recall += 1
            mrr_sum += 1.0 / positions[0]
        if result_ids[:1] and result_ids[0] in expected:
            top1 += 1
        elif len(failures) < 10:
            failures.append({"id": row["id"], "query": row["query"], "expected": sorted(expected), "actual": result_ids[:5]})
    count = len(rows)
    return {
        "recall_at_5": round(recall / count, 4) if count else None,
        "top1": round(top1 / count, 4) if count else None,
        "mrr_at_5": round(mrr_sum / count, 4) if count else None,
        "sample_top1_failures": failures,
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def metric_delta(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, float]:
    return {
        key: round(float(remote[key]) - float(local[key]), 4)
        for key in ("recall_at_5", "top1", "mrr_at_5")
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
