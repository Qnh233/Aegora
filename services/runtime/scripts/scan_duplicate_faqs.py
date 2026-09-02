#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aegora_runtime.db import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan exact duplicate FAQ groups by content_hash.")
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "faq_exact_duplicates.json")
    args = parser.parse_args()

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    content_hash,
                    count(*) AS duplicate_count,
                    array_agg(strapi_id ORDER BY strapi_id) AS strapi_ids,
                    min(title) AS title,
                    min(faq) AS faq,
                    min(response) AS response
                FROM faqs
                WHERE deleted_at IS NULL
                GROUP BY content_hash
                HAVING count(*) > 1
                ORDER BY count(*) DESC, min(strapi_id)
                """
            )
            groups = [dict(row) for row in cur.fetchall()]

    summary = {
        "duplicate_groups": len(groups),
        "duplicate_rows": sum(group["duplicate_count"] for group in groups),
        "redundant_rows": sum(group["duplicate_count"] - 1 for group in groups),
        "groups": groups,
    }
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "groups"}, ensure_ascii=False, indent=2))
    print(f"wrote={args.output}")


if __name__ == "__main__":
    main()

