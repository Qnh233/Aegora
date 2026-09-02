#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aegora_runtime.db import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASETS = sorted((ROOT / "evals").glob("eval_*.jsonl"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand expected FAQ IDs using exact duplicate groups.")
    parser.add_argument("datasets", nargs="*", type=Path, default=DEFAULT_DATASETS)
    args = parser.parse_args()

    duplicate_map = load_duplicate_map()
    for dataset in args.datasets:
        rows = read_jsonl(dataset)
        changed_rows, added_ids = expand_expected_faq_ids(rows, duplicate_map)
        write_jsonl(dataset, rows)
        print(f"dataset={dataset.name} changed_rows={changed_rows} added_ids={added_ids}")


def load_duplicate_map() -> dict[int, list[int]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT array_agg(strapi_id ORDER BY strapi_id) AS strapi_ids
                FROM faqs
                WHERE deleted_at IS NULL
                GROUP BY content_hash
                """
            )
            groups = [list(row["strapi_ids"]) for row in cur.fetchall()]
    return {faq_id: group for group in groups for faq_id in group}


def expand_expected_faq_ids(rows: list[dict[str, Any]], duplicate_map: dict[int, list[int]]) -> tuple[int, int]:
    changed_rows = 0
    added_ids = 0
    for row in rows:
        expected = [int(item) for item in row.get("expected_faq_ids") or []]
        expanded = sorted({duplicate_id for faq_id in expected for duplicate_id in duplicate_map.get(faq_id, [faq_id])})
        if expanded != expected:
            row["expected_faq_ids"] = expanded
            changed_rows += 1
            added_ids += len(expanded) - len(expected)
    return changed_rows, added_ids


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
