#!/usr/bin/env python3
from __future__ import annotations

from aegora_runtime.db import connect


TABLES = [
    "faq_categories",
    "faqs",
    "faq_embeddings",
    "skills",
    "skill_embeddings",
    "user_memories",
    "tool_logs",
    "audit_logs",
]


def main() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                ORDER BY table_name
                """,
                (TABLES,),
            )
            tables = [row["table_name"] for row in cur.fetchall()]

            cur.execute("SELECT count(*) AS count FROM faqs")
            faq_count = cur.fetchone()["count"]

            cur.execute("SELECT count(*) AS count FROM faq_embeddings")
            embedding_count = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT c.project_category, count(*) AS count
                FROM faqs f
                JOIN faq_categories c ON c.id = f.category_id
                GROUP BY c.project_category
                ORDER BY count DESC, c.project_category
                """
            )
            project_categories = cur.fetchall()

            cur.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename IN ('faqs', 'faq_embeddings', 'skills', 'skill_embeddings')
                ORDER BY indexname
                """
            )
            indexes = [row["indexname"] for row in cur.fetchall()]

            cur.execute(
                """
                SELECT table_name, column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name IN ('faq_embeddings', 'skill_embeddings')
                  AND column_name = 'embedding'
                ORDER BY table_name
                """
            )
            embedding_columns = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT strapi_id, title
                FROM faqs
                WHERE search_document @@ app_to_tsquery('K线')
                ORDER BY strapi_id
                LIMIT 3
                """
            )
            fulltext_samples = cur.fetchall()

    print("Schema check")
    print("tables=" + ",".join(tables))
    print(f"faq_count={faq_count}")
    print(f"embedding_count={embedding_count}")
    print("indexes=" + ",".join(indexes))
    print(f"embedding_columns={embedding_columns}")
    print(f"fulltext_sample_count={len(fulltext_samples)}")
    print("project_categories=" + ",".join(f"{row['project_category']}:{row['count']}" for row in project_categories))


if __name__ == "__main__":
    main()
