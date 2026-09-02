#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from aegora_runtime.config import ConfigError, load_settings
from aegora_runtime.db import connect


@contextmanager
def migration_lock(conn) -> Iterator[None]:
    """Allow only one schema migration process at a time."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext('aegora_runtime_schema_migration'))")
    conn.commit()
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext('aegora_runtime_schema_migration'))")
        conn.commit()


def main() -> None:
    settings = load_settings(validate_secrets=True)
    with connect(settings) as conn:
        with migration_lock(conn):
            ensure_required_extensions(conn)
            text_config = ensure_text_search_config(conn)
            apply_schema(conn, text_config)
            conn.commit()
    print("Database migration ok")
    print(f"text_search_config={text_config}")
    print(f"embedding_dim={settings.database.embedding_dim}")


def ensure_required_extensions(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT extname
            FROM pg_extension
            WHERE extname IN ('vector')
            """
        )
        installed = {row["extname"] for row in cur.fetchall()}
    missing = {"vector"} - installed
    if missing:
        raise ConfigError(
            "缺少数据库扩展: "
            + ", ".join(sorted(missing))
            + "。请先用超级用户运行 scripts/db_bootstrap.py。"
        )


def ensure_text_search_config(conn) -> str:
    settings = load_settings()
    preferred = settings.database.preferred_text_search_config
    fallback = settings.database.fallback_text_search_config
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT installed_version IS NOT NULL AS installed
            FROM pg_available_extensions
            WHERE name = 'zhparser'
            """
        )
        row = cur.fetchone()
        zhparser_available = bool(row and row["installed"])

        if not zhparser_available:
            cur.execute(
                """
                SELECT default_version IS NOT NULL AS available
                FROM pg_available_extensions
                WHERE name = 'zhparser'
                """
            )
            available_row = cur.fetchone()
            if available_row and available_row["available"]:
                cur.execute("CREATE EXTENSION IF NOT EXISTS zhparser")
                zhparser_available = True

        if zhparser_available:
            cur.execute(
                """
                SELECT 1
                FROM pg_ts_config
                WHERE cfgnamespace = current_schema()::regnamespace
                  AND cfgname = %s
                """,
                (preferred,),
            )
            if cur.fetchone() is None:
                cur.execute(f"CREATE TEXT SEARCH CONFIGURATION {preferred} (PARSER = zhparser)")
                cur.execute(
                    f"""
                    ALTER TEXT SEARCH CONFIGURATION {preferred}
                    ADD MAPPING FOR n,v,a,i,e,l,j WITH simple
                    """
                )
            return preferred
        return fallback


def apply_schema(conn, text_config: str) -> None:
    settings = load_settings()
    dim = settings.database.embedding_dim
    if dim <= 0:
        raise ConfigError("database.embedding_dim 必须大于 0")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE OR REPLACE FUNCTION app_to_tsvector(input_text text)
            RETURNS tsvector
            LANGUAGE sql
            IMMUTABLE
            PARALLEL SAFE
            AS $$
                SELECT to_tsvector('{text_config}'::regconfig, coalesce(input_text, ''))
            $$;
            """
        )
        cur.execute(
            f"""
            CREATE OR REPLACE FUNCTION app_to_tsquery(input_text text)
            RETURNS tsquery
            LANGUAGE sql
            IMMUTABLE
            PARALLEL SAFE
            AS $$
                SELECT plainto_tsquery('{text_config}'::regconfig, coalesce(input_text, ''))
            $$;
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS faq_categories (
                id bigserial PRIMARY KEY,
                strapi_id bigint UNIQUE,
                name text NOT NULL UNIQUE,
                project_category text NOT NULL DEFAULT 'other',
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("ALTER TABLE faq_categories ADD COLUMN IF NOT EXISTS project_category text NOT NULL DEFAULT 'other'")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS faqs (
                id bigserial PRIMARY KEY,
                strapi_id bigint NOT NULL UNIQUE,
                title text,
                faq text NOT NULL,
                response text NOT NULL,
                keywords text,
                intention text,
                category_id bigint REFERENCES faq_categories(id),
                user_side boolean,
                response_pic_app_url text,
                response_pic_pc_url text,
                source_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                content_hash text NOT NULL,
                version integer NOT NULL DEFAULT 1,
                search_document tsvector GENERATED ALWAYS AS (
                    app_to_tsvector(
                        coalesce(title, '') || ' ' ||
                        coalesce(faq, '') || ' ' ||
                        coalesce(keywords, '') || ' ' ||
                        coalesce(response, '') || ' ' ||
                        coalesce(intention, '')
                    )
                ) STORED,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                deleted_at timestamptz
            )
            """
        )

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS faq_embeddings (
                faq_id bigint PRIMARY KEY REFERENCES faqs(id) ON DELETE CASCADE,
                embedding vector({dim}),
                embedding_model text NOT NULL,
                embedding_dim integer NOT NULL DEFAULT {dim},
                content_hash text NOT NULL,
                faq_version integer NOT NULL DEFAULT 1,
                status text NOT NULL DEFAULT 'pending',
                attempt_count integer NOT NULL DEFAULT 0,
                last_error text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                CHECK (embedding_dim = {dim})
            )
            """
        )
        cur.execute("ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS faq_version integer NOT NULL DEFAULT 1")
        cur.execute("ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending'")
        cur.execute("ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS last_error text")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
                id bigserial PRIMARY KEY,
                name text NOT NULL UNIQUE,
                title text NOT NULL,
                description text NOT NULL,
                content text NOT NULL,
                product_id text,
                domain text,
                skill_type text NOT NULL DEFAULT 'guidance',
                source text NOT NULL DEFAULT 'manual',
                status text NOT NULL DEFAULT 'draft',
                priority integer NOT NULL DEFAULT 0,
                trigger_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                version integer NOT NULL DEFAULT 1,
                content_hash text NOT NULL,
                reviewed_by text,
                reviewed_at timestamptz,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                CHECK (domain IS NULL OR product_id IS NOT NULL),
                CHECK (skill_type IN ('guidance', 'commercial', 'workflow', 'safety')),
                CHECK (source IN ('manual', 'agent')),
                CHECK (status IN ('draft', 'active', 'rejected', 'archived')),
                CHECK (priority BETWEEN -100 AND 100)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS skill_embeddings (
                skill_id bigint PRIMARY KEY REFERENCES skills(id) ON DELETE CASCADE,
                embedding vector({dim}),
                embedding_model text NOT NULL,
                embedding_dim integer NOT NULL DEFAULT {dim},
                content_hash text NOT NULL,
                skill_version integer NOT NULL DEFAULT 1,
                status text NOT NULL DEFAULT 'pending',
                attempt_count integer NOT NULL DEFAULT 0,
                last_error text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                CHECK (embedding_dim = {dim}),
                CHECK (status IN ('pending', 'processing', 'completed', 'failed'))
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_memories (
                user_id text PRIMARY KEY,
                profile jsonb NOT NULL DEFAULT '{}'::jsonb,
                facts jsonb NOT NULL DEFAULT '{}'::jsonb,
                timeline jsonb NOT NULL DEFAULT '[]'::jsonb,
                summary text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_logs (
                id bigserial PRIMARY KEY,
                trace_id text NOT NULL,
                session_id text,
                user_id text,
                tool_name text NOT NULL,
                input jsonb NOT NULL DEFAULT '{}'::jsonb,
                output jsonb,
                status text NOT NULL,
                error_code text,
                error_message text,
                latency_ms integer,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id bigserial PRIMARY KEY,
                trace_id text NOT NULL,
                session_id text,
                user_id text,
                event_type text NOT NULL,
                node_name text,
                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS data_ops_reports (
                id bigserial PRIMARY KEY,
                job_id text NOT NULL UNIQUE,
                job_type text NOT NULL,
                period_start timestamptz,
                period_end timestamptz,
                status text NOT NULL,
                summary text,
                metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
                items jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_skill_draft_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                error text,
                started_at timestamptz NOT NULL DEFAULT now(),
                finished_at timestamptz,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_categories_name ON faq_categories(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_categories_project_category ON faq_categories(project_category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faqs_category_id ON faqs(category_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faqs_intention ON faqs(intention)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faqs_updated_at ON faqs(updated_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faqs_search_document ON faqs USING GIN(search_document)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_faq_embeddings_embedding_hnsw "
            "ON faq_embeddings USING hnsw (embedding vector_cosine_ops)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_faq_embeddings_status ON faq_embeddings(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_skills_scope_status ON skills(product_id, domain, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_skills_type_status ON skills(skill_type, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_skill_embeddings_status ON skill_embeddings(status)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_skill_embeddings_embedding_hnsw "
            "ON skill_embeddings USING hnsw (embedding vector_cosine_ops)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_trace_id ON tool_logs(trace_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_created_at ON tool_logs(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_trace_id ON audit_logs(trace_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_data_ops_reports_job_type ON data_ops_reports(job_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_data_ops_reports_created_at ON data_ops_reports(created_at)")


if __name__ == "__main__":
    main()
