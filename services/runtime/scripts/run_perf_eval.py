#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from aegora_runtime.agent_loop import AgentRequest, run_agent
from aegora_runtime.config import load_settings
from aegora_runtime.demo_agent import build_demo_dependencies
from aegora_runtime.real_agent import build_real_dependencies


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evals" / "eval_perf_seed_100.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run latency smoke/perf eval for the current demo Agent.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--agent", choices=["demo", "real"], default="real")
    parser.add_argument("--loop-mode", choices=["planner"], default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "latest_perf_eval.json")
    args = parser.parse_args()

    rows = read_jsonl(args.dataset)
    if args.limit:
        rows = rows[: args.limit]
    settings = load_settings(validate_secrets=True)
    deps = build_real_dependencies(settings) if args.agent == "real" else build_demo_dependencies(settings)

    started = time.perf_counter()
    if args.concurrency <= 1:
        results = []
        for index, row in enumerate(rows, start=1):
            result = run_one(row, settings, deps, args.loop_mode)
            results.append(result)
            print_progress(index, len(rows), result)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(run_one, row, settings, deps, args.loop_mode) for row in rows]
            results = []
            for index, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                results.append(result)
                print_progress(index, len(rows), result)
    wall_ms = (time.perf_counter() - started) * 1000

    summary = summarize(results, wall_ms, args.concurrency)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote={args.output}")


def run_one(row: dict[str, Any], settings, deps, loop_mode: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = run_agent(
            AgentRequest(query=row["query"], session_id=f"perf-{row['id']}"),
            deps,
            settings,
            loop_mode=loop_mode,
            attach_hooks=False,
            enable_pocoflow_db=False,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "id": row["id"],
            "ok": True,
            "latency_ms": latency_ms,
            "route": result.get("route"),
            "status": result.get("status"),
            "evidence_count": len(result.get("retrieved_faqs") or []),
            "model_usage": result.get("model_usage") or {},
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return {
            "id": row["id"],
            "ok": False,
            "latency_ms": latency_ms,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def summarize(results: list[dict[str, Any]], wall_ms: float, concurrency: int) -> dict[str, Any]:
    latencies = sorted(item["latency_ms"] for item in results)
    ok_count = sum(1 for item in results if item["ok"])
    route_counts: dict[str, int] = {}
    model_calls = []
    total_tokens = []
    for item in results:
        route = str(item.get("route") or "error")
        route_counts[route] = route_counts.get(route, 0) + 1
        usage = item.get("model_usage") or {}
        model_calls.append(int(usage.get("total_calls") or 0))
        total_tokens.append(sum_model_tokens(usage))
    return {
        "total": len(results),
        "ok": ok_count,
        "success_rate": round(ok_count / len(results), 4) if results else None,
        "concurrency": concurrency,
        "wall_ms": round(wall_ms, 2),
        "throughput_qps": round(len(results) / (wall_ms / 1000), 4) if wall_ms > 0 else None,
        "latency_ms": {
            "min": round(latencies[0], 2) if latencies else None,
            "p50": round(percentile(latencies, 50), 2) if latencies else None,
            "p90": round(percentile(latencies, 90), 2) if latencies else None,
            "p95": round(percentile(latencies, 95), 2) if latencies else None,
            "p99": round(percentile(latencies, 99), 2) if latencies else None,
            "max": round(latencies[-1], 2) if latencies else None,
            "mean": round(statistics.mean(latencies), 2) if latencies else None,
        },
        "route_counts": route_counts,
        "model_usage": {
            "calls_total": sum(model_calls),
            "calls_mean": round(statistics.mean(model_calls), 4) if model_calls else None,
            "calls_p50": round(percentile(sorted(model_calls), 50), 2) if model_calls else None,
            "tokens_total": sum(total_tokens),
            "tokens_mean": round(statistics.mean(total_tokens), 2) if total_tokens else None,
        },
        "slowest": sorted(results, key=lambda item: item["latency_ms"], reverse=True)[:10],
        "errors": [item for item in results if not item["ok"]][:10],
    }


def print_progress(done: int, total: int, result: dict[str, Any]) -> None:
    status = "ok" if result.get("ok") else "error"
    print(
        f"[{done}/{total}] {status} id={result.get('id')} route={result.get('route')} latency_ms={result.get('latency_ms'):.2f}",
        file=sys.stderr,
        flush=True,
    )


def percentile(sorted_values: list[float], p: int) -> float:
    if not sorted_values:
        raise ValueError("empty values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * p / 100
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def sum_model_tokens(usage: dict[str, Any]) -> int:
    total = 0
    for item in (usage.get("by_model") or {}).values():
        total += int(item.get("total_tokens") or 0)
    return total


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    main()
