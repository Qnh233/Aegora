#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aegora_runtime.memory.evaluation import load_memory_eval_cases, run_memory_eval


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Aegora governed memory behavior.")
    parser.add_argument(
        "--dataset",
        default="evals/memory_eval_seed.jsonl",
        help="JSONL memory evaluation dataset.",
    )
    parser.add_argument("--output", help="Optional JSON report path.")
    args = parser.parse_args()

    cases = load_memory_eval_cases(args.dataset)
    report = run_memory_eval(cases).to_dict()
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
