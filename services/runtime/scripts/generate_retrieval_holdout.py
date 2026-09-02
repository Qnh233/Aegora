#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from scripts.generate_eval_sets import make_case, normalize_text, split_faq_variants, write_jsonl
    from scripts.import_faqs import faq_hash
except ModuleNotFoundError:
    from generate_eval_sets import make_case, normalize_text, split_faq_variants, write_jsonl
    from import_faqs import faq_hash


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE = ROOT / "data" / "strapi_knowledge_parsed.json"
DEFAULT_DEVELOPMENT_SET = ROOT / "evals" / "eval_retrieval_sample_300.jsonl"
DEFAULT_OUTPUT = ROOT / "evals" / "eval_retrieval_holdout_300.jsonl"
DEFAULT_MANIFEST = ROOT / "evals" / "retrieval_holdout_manifest.json"
RANDOM_SEED = 20260609


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a duplicate-disjoint retrieval holdout set.")
    parser.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE)
    parser.add_argument("--development-set", type=Path, default=DEFAULT_DEVELOPMENT_SET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--size", type=int, default=300)
    parser.add_argument("--per-category", type=int, default=5)
    args = parser.parse_args()

    raw_rows = json.loads(args.knowledge.read_text(encoding="utf-8"))
    development_rows = read_jsonl(args.development_set)
    holdout = build_holdout(raw_rows, development_rows, args.size, args.per_category, RANDOM_SEED)
    write_jsonl(args.output, holdout)

    isolation = validate_isolation(raw_rows, development_rows, holdout)
    manifest = {
        "output": str(args.output),
        "count": len(holdout),
        "random_seed": RANDOM_SEED,
        "development_set": str(args.development_set),
        "development_faq_ids": len(expected_ids(development_rows)),
        "query_sources": dict(Counter(tag for row in holdout for tag in row["tags"] if tag.endswith("_query"))),
        "categories": dict(Counter(row["expected_category"] for row in holdout)),
        "isolation": isolation,
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def build_holdout(
    raw_rows: list[dict[str, Any]],
    development_rows: list[dict[str, Any]],
    size: int,
    per_category: int,
    seed: int,
) -> list[dict[str, Any]]:
    if size <= 0 or per_category <= 0:
        raise ValueError("size and per_category must be greater than 0")

    groups = group_knowledge_rows(raw_rows)
    development_ids = expected_ids(development_rows)
    excluded_hashes = {
        content_hash
        for content_hash, rows in groups.items()
        if any(int(row["id"]) in development_ids for row in rows)
    }
    candidates = [rows for content_hash, rows in groups.items() if content_hash not in excluded_hashes]
    selected = stratified_sample(candidates, size, per_category, seed)
    if len(selected) != size:
        raise RuntimeError(f"holdout size should be {size}, got {len(selected)}")

    cases = []
    for index, group in enumerate(selected, start=1):
        row = min(group, key=lambda item: int(item["id"]))
        query, query_source = choose_holdout_query(row)
        cases.append(
            make_case(
                f"holdout_{index:03d}",
                query,
                "faq_answer",
                source="knowledge_holdout",
                expected_faq_ids=sorted(int(item["id"]) for item in group),
                expected_category=normalize_text(row.get("Category")) or "未分类",
                tags=["retrieval", "holdout", query_source, normalize_text(row.get("Category")) or "未分类"],
                difficulty="medium" if query_source != "original_query" else "easy",
            )
        )
    return cases


def group_knowledge_rows(raw_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        if normalize_text(row.get("FAQ")) and normalize_text(row.get("Response")):
            groups[faq_hash(row)].append(row)
    return dict(groups)


def stratified_sample(
    groups: list[list[dict[str, Any]]],
    size: int,
    per_category: int,
    seed: int,
) -> list[list[dict[str, Any]]]:
    rng = random.Random(seed)
    by_category: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
    for group in groups:
        category = normalize_text(group[0].get("Category")) or "未分类"
        by_category[category].append(group)

    selected: list[list[dict[str, Any]]] = []
    selected_hashes: set[str] = set()
    for category in sorted(by_category):
        if category == "未分类":
            continue
        category_groups = list(by_category[category])
        rng.shuffle(category_groups)
        for group in category_groups[:per_category]:
            selected.append(group)
            selected_hashes.add(faq_hash(group[0]))

    remaining = [group for group in groups if faq_hash(group[0]) not in selected_hashes]
    rng.shuffle(remaining)
    selected.extend(remaining[: max(0, size - len(selected))])
    return selected[:size]


def choose_holdout_query(row: dict[str, Any]) -> tuple[str, str]:
    faq = row.get("FAQ") or ""
    variants = split_faq_variants(faq)
    faq = normalize_text(faq)
    first = variants[0] if variants else faq
    for variant in variants[1:]:
        if canonical_question(variant) != canonical_question(first):
            return variant, "alternate_query"

    title = normalize_text(row.get("Title"))
    if len(title) >= 4 and canonical_question(title) != canonical_question(first):
        return title, "title_query"
    return first, "original_query"


def canonical_question(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def expected_ids(rows: list[dict[str, Any]]) -> set[int]:
    return {int(faq_id) for row in rows for faq_id in row.get("expected_faq_ids") or []}


def validate_isolation(
    raw_rows: list[dict[str, Any]],
    development_rows: list[dict[str, Any]],
    holdout_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    id_to_hash = {int(row["id"]): faq_hash(row) for row in raw_rows}
    development_ids = expected_ids(development_rows)
    holdout_ids = expected_ids(holdout_rows)
    development_hashes = {id_to_hash[faq_id] for faq_id in development_ids}
    holdout_hashes = {id_to_hash[faq_id] for faq_id in holdout_ids}
    overlapping_ids = sorted(development_ids & holdout_ids)
    overlapping_hashes = sorted(development_hashes & holdout_hashes)
    if overlapping_ids or overlapping_hashes:
        raise RuntimeError("holdout overlaps development set")
    return {
        "faq_id_overlap": 0,
        "content_hash_overlap": 0,
        "holdout_unique_content_hashes": len(holdout_hashes),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    main()
