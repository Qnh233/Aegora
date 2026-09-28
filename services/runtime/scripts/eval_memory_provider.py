#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import replace
from pathlib import Path

from aegora_runtime.config import load_settings
from aegora_runtime.memory.evaluation import (
    load_memory_eval_cases,
    run_memory_provider_eval,
)
from aegora_runtime.memory.native_pg import NativePgMemoryProvider
from aegora_runtime.memory.openviking import OpenVikingMemoryProvider


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the governed memory eval corpus against a concrete provider."
    )
    parser.add_argument(
        "--provider",
        choices=["native_pg", "openviking"],
        required=True,
    )
    parser.add_argument(
        "--dataset",
        default="evals/memory_eval_seed.jsonl",
    )
    parser.add_argument("--output")
    parser.add_argument("--run-id")
    args = parser.parse_args()

    settings = load_settings(env_path=None)
    settings = replace(
        settings,
        memory=replace(settings.memory, provider=args.provider),
    )
    provider_factory = (
        (lambda: NativePgMemoryProvider(settings))
        if args.provider == "native_pg"
        else (lambda: OpenVikingMemoryProvider(settings))
    )
    run_id = args.run_id or uuid.uuid4().hex[:10]
    report = run_memory_provider_eval(
        load_memory_eval_cases(args.dataset),
        provider_factory=provider_factory,
        run_id=run_id,
    ).to_dict()
    report["provider"] = args.provider
    report["run_id"] = run_id
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
