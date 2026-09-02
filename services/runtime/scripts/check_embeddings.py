#!/usr/bin/env python3
from __future__ import annotations

from aegora_runtime.db import connect


def main() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) AS total,
                    count(e.embedding) AS embedded,
                    count(*) FILTER (WHERE e.status = 'completed') AS completed,
                    count(*) FILTER (WHERE e.status = 'failed') AS failed,
                    count(*) FILTER (WHERE e.embedding IS NULL AND e.status <> 'failed') AS pending,
                    count(*) FILTER (WHERE e.faq_version <> f.version) AS version_mismatch,
                    count(*) FILTER (WHERE e.content_hash <> f.content_hash) AS hash_mismatch,
                    min(e.updated_at) AS oldest_updated_at,
                    max(e.updated_at) AS newest_updated_at
                FROM faq_embeddings e
                JOIN faqs f ON f.id = e.faq_id
                """
            )
            summary = dict(cur.fetchone())

            cur.execute(
                """
                SELECT
                    e.faq_id,
                    f.strapi_id,
                    e.embedding_model,
                    e.embedding_dim,
                    e.faq_version,
                    e.status,
                    e.attempt_count,
                    e.last_error,
                    e.updated_at
                FROM faq_embeddings e
                JOIN faqs f ON f.id = e.faq_id
                WHERE e.status = 'failed'
                ORDER BY e.updated_at DESC
                LIMIT 10
                """
            )
            failed = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    f.strapi_id,
                    round(vector_norm(e.embedding)::numeric, 6) AS vector_norm
                FROM faq_embeddings e
                JOIN faqs f ON f.id = e.faq_id
                WHERE e.embedding IS NOT NULL
                ORDER BY e.faq_id
                LIMIT 5
                """
            )
            samples = [dict(row) for row in cur.fetchall()]

    print(f"summary={summary}")
    print(f"vector_samples={samples}")
    print(f"failed_samples={failed}")

    if summary["embedded"] != summary["completed"]:
        raise SystemExit("embedding/status count mismatch")
    if summary["version_mismatch"] or summary["hash_mismatch"]:
        raise SystemExit("embedding metadata is stale")
    if summary["failed"]:
        raise SystemExit("failed embeddings exist")


if __name__ == "__main__":
    main()

