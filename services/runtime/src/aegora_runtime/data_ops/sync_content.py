from __future__ import annotations

from pathlib import Path
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.strapi import StrapiClient, collection_endpoint

from scripts.import_faqs import import_rows as import_faq_rows
from scripts.import_faqs import load_taxonomy
from scripts.sync_strapi_content import normalize_strapi_skill, sync_skill_rows


ROOT = Path(__file__).resolve().parents[3]


def sync_content(settings: Settings, content: str = "all") -> dict[str, Any]:
    client = StrapiClient(settings.strapi)
    metrics: dict[str, Any] = {}
    if content in {"all", "faq"}:
        rows = client.list(collection_endpoint(settings, "faq_content"), page_size=100)
        with connect(settings) as conn:
            imported = import_faq_rows(
                conn,
                rows,
                settings.embedding.storage_model,
                settings.database.embedding_dim,
                load_taxonomy(ROOT / "config" / "category_taxonomy.toml"),
            )
            deleted = mark_missing_faqs_deleted(conn, {int(row["id"]) for row in rows})
            conn.commit()
        metrics.update({"faq_source_rows": len(rows), "faq_synced": imported, "faq_marked_deleted": deleted})
    if content in {"all", "skills"}:
        rows = client.list(collection_endpoint(settings, "skills_content"), page_size=100)
        synced = sync_skill_rows(rows, settings)
        archived = mark_missing_skills_archived(settings, rows)
        metrics.update({"skill_source_rows": len(rows), "skill_synced": synced, "skill_marked_archived": archived})
    return metrics


def mark_missing_faqs_deleted(conn, source_ids: set[int]) -> int:
    with conn.cursor() as cur:
        if source_ids:
            cur.execute(
                """
                UPDATE faqs
                SET deleted_at = now(), updated_at = now()
                WHERE deleted_at IS NULL AND NOT (strapi_id = ANY(%s))
                """,
                (list(source_ids),),
            )
        else:
            cur.execute(
                """
                UPDATE faqs
                SET deleted_at = now(), updated_at = now()
                WHERE deleted_at IS NULL
                """
            )
        return int(cur.rowcount or 0)


def mark_missing_skills_archived(settings: Settings, rows: list[dict[str, Any]]) -> int:
    normalized = [normalize_strapi_skill(row) for row in rows]
    source_names = {row["name"] for row in normalized}
    with connect(settings) as conn:
        with conn.cursor() as cur:
            if source_names:
                cur.execute(
                    """
                    UPDATE skills
                    SET status = 'archived', updated_at = now()
                    WHERE status <> 'archived'
                      AND metadata ? 'strapi_id'
                      AND NOT (name = ANY(%s))
                    """,
                    (list(source_names),),
                )
            else:
                cur.execute(
                    """
                    UPDATE skills
                    SET status = 'archived', updated_at = now()
                    WHERE status <> 'archived' AND metadata ? 'strapi_id'
                    """
                )
            count = int(cur.rowcount or 0)
        conn.commit()
    return count
