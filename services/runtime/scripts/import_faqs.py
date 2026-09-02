#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.db import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "strapi_knowledge_parsed.json"
DEFAULT_TAXONOMY = ROOT / "config" / "category_taxonomy.toml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Strapi FAQ data into PostgreSQL.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    taxonomy = load_taxonomy(DEFAULT_TAXONOMY)
    rows = load_rows(args.input)
    with connect(settings) as conn:
        imported = import_rows(conn, rows, settings.database.embedding_model, settings.database.embedding_dim, taxonomy)
        conn.commit()

    print("FAQ import ok")
    print(f"input={args.input}")
    print(f"rows={len(rows)}")
    print(f"imported={imported}")


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"FAQ input must be a JSON list: {path}")
    return data


def load_taxonomy(path: Path) -> dict[str, str]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return dict(data.get("raw_to_project") or {})


def import_rows(
    conn,
    rows: list[dict[str, Any]],
    embedding_model: str,
    embedding_dim: int,
    taxonomy: dict[str, str],
) -> int:
    imported = 0
    with conn.cursor() as cur:
        for row in rows:
            strapi_id = int(row["id"])
            category_name = clean(row.get("Category")) or "未分类"
            project_category = taxonomy.get(category_name, "other")
            content_hash = faq_hash(row)

            cur.execute(
                """
                INSERT INTO faq_categories (name, project_category, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (name)
                DO UPDATE SET
                    project_category = excluded.project_category,
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (category_name, project_category),
            )
            category_id = cur.fetchone()["id"]

            cur.execute(
                """
                INSERT INTO faqs (
                    strapi_id,
                    title,
                    faq,
                    response,
                    keywords,
                    intention,
                    category_id,
                    user_side,
                    response_pic_app_url,
                    response_pic_pc_url,
                    source_payload,
                    content_hash,
                    updated_at,
                    deleted_at
                )
                VALUES (
                    %(strapi_id)s,
                    %(title)s,
                    %(faq)s,
                    %(response)s,
                    %(keywords)s,
                    %(intention)s,
                    %(category_id)s,
                    %(user_side)s,
                    %(response_pic_app_url)s,
                    %(response_pic_pc_url)s,
                    %(source_payload)s::jsonb,
                    %(content_hash)s,
                    now(),
                    NULL
                )
                ON CONFLICT (strapi_id)
                DO UPDATE SET
                    title = excluded.title,
                    faq = excluded.faq,
                    response = excluded.response,
                    keywords = excluded.keywords,
                    intention = excluded.intention,
                    category_id = excluded.category_id,
                    user_side = excluded.user_side,
                    response_pic_app_url = excluded.response_pic_app_url,
                    response_pic_pc_url = excluded.response_pic_pc_url,
                    source_payload = excluded.source_payload,
                    content_hash = excluded.content_hash,
                    version = CASE
                        WHEN faqs.content_hash <> excluded.content_hash THEN faqs.version + 1
                        ELSE faqs.version
                    END,
                    updated_at = now(),
                    deleted_at = NULL
                RETURNING id, content_hash, version
                """,
                {
                    "strapi_id": strapi_id,
                    "title": clean(row.get("Title")),
                    "faq": require_text(row, "FAQ"),
                    "response": require_text(row, "Response"),
                    "keywords": clean(row.get("Keywords")),
                    "intention": clean(row.get("Intention")),
                    "category_id": category_id,
                    "user_side": row.get("UserSide"),
                    "response_pic_app_url": clean(row.get("Response_Pic_App_URL")),
                    "response_pic_pc_url": clean(row.get("Response_Pic_Pc_URL")),
                    "source_payload": json.dumps(row, ensure_ascii=False, sort_keys=True),
                    "content_hash": content_hash,
                },
            )
            faq = cur.fetchone()

            cur.execute(
                """
                INSERT INTO faq_embeddings (
                    faq_id,
                    embedding,
                    embedding_model,
                    embedding_dim,
                    content_hash,
                    faq_version,
                    status,
                    updated_at
                )
                VALUES (%s, NULL, %s, %s, %s, %s, 'pending', now())
                ON CONFLICT (faq_id)
                DO UPDATE SET
                    embedding_model = excluded.embedding_model,
                    embedding_dim = excluded.embedding_dim,
                    content_hash = excluded.content_hash,
                    faq_version = excluded.faq_version,
                    embedding = CASE
                        WHEN faq_embeddings.content_hash <> excluded.content_hash THEN NULL
                        ELSE faq_embeddings.embedding
                    END,
                    status = CASE
                        WHEN faq_embeddings.content_hash <> excluded.content_hash THEN 'pending'
                        ELSE faq_embeddings.status
                    END,
                    attempt_count = CASE
                        WHEN faq_embeddings.content_hash <> excluded.content_hash THEN 0
                        ELSE faq_embeddings.attempt_count
                    END,
                    last_error = CASE
                        WHEN faq_embeddings.content_hash <> excluded.content_hash THEN NULL
                        ELSE faq_embeddings.last_error
                    END,
                    updated_at = CASE
                        WHEN faq_embeddings.content_hash <> excluded.content_hash THEN now()
                        ELSE faq_embeddings.updated_at
                    END
                """,
                (faq["id"], embedding_model, embedding_dim, faq["content_hash"], faq["version"]),
            )
            imported += 1
    return imported


def faq_hash(row: dict[str, Any]) -> str:
    parts = [
        clean(row.get("Title")),
        require_text(row, "FAQ"),
        require_text(row, "Response"),
        clean(row.get("Keywords")),
        clean(row.get("Intention")),
        clean(row.get("Category")),
    ]
    payload = "\n".join(part or "" for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def require_text(row: dict[str, Any], key: str) -> str:
    value = clean(row.get(key))
    if value is None:
        raise ValueError(f"FAQ row {row.get('id')} missing required field {key}")
    return value


if __name__ == "__main__":
    main()
