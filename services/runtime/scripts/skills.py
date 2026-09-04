#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.db import connect
from aegora_runtime.embeddings import build_encoder, vector_literal
from aegora_runtime.skills import agent_skill_promotion_errors, build_skill_embedding_text, skill_content_hash, validate_skill


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Skill experience records.")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("path", type=Path)

    import_cmd = sub.add_parser("import")
    import_cmd.add_argument("path", type=Path)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--status")
    list_cmd.add_argument("--product-id")

    for command in ["publish", "reject", "archive"]:
        cmd = sub.add_parser(command)
        cmd.add_argument("--id", type=int, required=True)
        if command in {"publish", "reject"}:
            cmd.add_argument("--reviewer", required=True)

    embed_cmd = sub.add_parser("embed")
    embed_cmd.add_argument("--limit", type=int)
    embed_cmd.add_argument("--retry-failed", action="store_true")

    args = parser.parse_args()
    settings = load_settings(validate_secrets=True)
    if args.command in {"import", "list", "publish", "reject", "archive"}:
        ensure_pg_skill_management_allowed(settings)
    if args.command == "validate":
        rows = load_skill_file(args.path)
        validate_rows(rows, settings.skills.max_content_chars)
        print(f"skill_validate_ok rows={len(rows)}")
    elif args.command == "import":
        rows = load_skill_file(args.path)
        validate_rows(rows, settings.skills.max_content_chars)
        print(f"skill_import_ok rows={import_rows(rows, settings)}")
    elif args.command == "list":
        print(json.dumps(list_rows(args.status, args.product_id, settings), ensure_ascii=False, indent=2, default=str))
    elif args.command in {"publish", "reject", "archive"}:
        reviewer = getattr(args, "reviewer", None)
        update_status(args.id, args.command, reviewer, settings)
        print(f"skill_{args.command}_ok id={args.id}")
    elif args.command == "embed":
        embed_pending(settings, args.limit, args.retry_failed)


def ensure_pg_skill_management_allowed(settings) -> None:
    collection = settings.collections.get("skills_content")
    if collection and collection.owner == "strapi":
        raise RuntimeError(
            "Skill 内容由 Strapi 管理；请在 Strapi 审核修改后运行 "
            "scripts/sync_strapi_content.py --content skills。"
        )


def load_skill_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    if path.suffix.lower() == ".md":
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---json\n") or "\n---\n" not in text:
            raise ValueError("Markdown Skill 必须以 ---json 开始，并使用 --- 结束 JSON 元数据")
        raw_meta, content = text[len("---json\n") :].split("\n---\n", 1)
        metadata = json.loads(raw_meta)
        metadata["content"] = content.strip()
        return [metadata]
    raise ValueError("Skill 文件仅支持 .json 或 .md")


def validate_rows(rows: list[dict[str, Any]], max_content_chars: int) -> None:
    errors = []
    names = set()
    for index, row in enumerate(rows):
        row_errors = validate_skill(row, max_content_chars)
        if row.get("name") in names:
            row_errors.append("同一文件内 name 重复")
        names.add(row.get("name"))
        errors.extend(f"row[{index}]: {error}" for error in row_errors)
    if errors:
        raise ValueError("\n".join(errors))


def import_rows(rows: list[dict[str, Any]], settings) -> int:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            for row in rows:
                values = normalized_skill(row)
                values["content_hash"] = skill_content_hash(values)
                cur.execute(
                    """
                    INSERT INTO skills (
                        name, title, description, content, product_id, domain,
                        skill_type, source, status, priority, trigger_rules,
                        metadata, content_hash, updated_at
                    )
                    VALUES (
                        %(name)s, %(title)s, %(description)s, %(content)s, %(product_id)s, %(domain)s,
                        %(skill_type)s, %(source)s, %(status)s, %(priority)s, %(trigger_rules)s::jsonb,
                        %(metadata)s::jsonb, %(content_hash)s, now()
                    )
                    ON CONFLICT (name)
                    DO UPDATE SET
                        title = excluded.title,
                        description = excluded.description,
                        content = excluded.content,
                        product_id = excluded.product_id,
                        domain = excluded.domain,
                        skill_type = excluded.skill_type,
                        source = excluded.source,
                        status = 'draft',
                        priority = excluded.priority,
                        trigger_rules = excluded.trigger_rules,
                        metadata = excluded.metadata,
                        version = CASE WHEN skills.content_hash <> excluded.content_hash THEN skills.version + 1 ELSE skills.version END,
                        content_hash = excluded.content_hash,
                        reviewed_by = NULL,
                        reviewed_at = NULL,
                        updated_at = now()
                    RETURNING id, version, content_hash
                    """,
                    values,
                )
                skill = cur.fetchone()
                ensure_embedding_row(cur, skill["id"], skill["version"], skill["content_hash"], settings)
        conn.commit()
    return len(rows)


def normalized_skill(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"].strip(),
        "title": row["title"].strip(),
        "description": row["description"].strip(),
        "content": row["content"].strip(),
        "product_id": clean(row.get("product_id")),
        "domain": clean(row.get("domain")),
        "skill_type": row.get("skill_type", "guidance"),
        "source": row.get("source", "manual"),
        "status": "draft",
        "priority": int(row.get("priority", 0)),
        "trigger_rules": json.dumps(row.get("trigger_rules") or {}, ensure_ascii=False, sort_keys=True),
        "metadata": json.dumps(row.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
    }


def ensure_embedding_row(cur, skill_id: int, version: int, content_hash: str, settings) -> None:
    cur.execute(
        """
        INSERT INTO skill_embeddings (
            skill_id, embedding_model, embedding_dim, content_hash, skill_version, status
        )
        VALUES (%s, %s, %s, %s, %s, 'pending')
        ON CONFLICT (skill_id)
        DO UPDATE SET
            embedding = CASE
                WHEN skill_embeddings.content_hash <> excluded.content_hash THEN NULL
                ELSE skill_embeddings.embedding
            END,
            embedding_model = excluded.embedding_model,
            embedding_dim = excluded.embedding_dim,
            content_hash = excluded.content_hash,
            skill_version = excluded.skill_version,
            status = CASE
                WHEN skill_embeddings.content_hash <> excluded.content_hash THEN 'pending'
                ELSE skill_embeddings.status
            END,
            attempt_count = CASE
                WHEN skill_embeddings.content_hash <> excluded.content_hash THEN 0
                ELSE skill_embeddings.attempt_count
            END,
            last_error = CASE
                WHEN skill_embeddings.content_hash <> excluded.content_hash THEN NULL
                ELSE skill_embeddings.last_error
            END,
            updated_at = now()
        """,
        (skill_id, settings.embedding.storage_model, settings.database.embedding_dim, content_hash, version),
    )


def update_status(skill_id: int, action: str, reviewer: str | None, settings) -> None:
    status = {"publish": "active", "reject": "rejected", "archive": "archived"}[action]
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM skills WHERE id = %s", (skill_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Skill 不存在: {skill_id}")
            if action == "publish":
                errors = validate_skill(dict(row), settings.skills.max_content_chars)
                errors.extend(agent_skill_promotion_errors(dict(row), reviewer))
                if errors:
                    raise ValueError("\n".join(errors))
            cur.execute(
                """
                UPDATE skills
                SET status = %s,
                    reviewed_by = %s,
                    reviewed_at = CASE WHEN %s::text IS NULL THEN reviewed_at ELSE now() END,
                    updated_at = now()
                WHERE id = %s
                """,
                (status, reviewer, reviewer, skill_id),
            )
            if action == "publish":
                ensure_embedding_row(cur, row["id"], row["version"], row["content_hash"], settings)
        conn.commit()


def list_rows(status: str | None, product_id: str | None, settings) -> list[dict[str, Any]]:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.name, s.title, s.product_id, s.domain, s.skill_type, s.source,
                       s.status, s.priority, s.version, s.reviewed_by, s.updated_at,
                       e.status AS embedding_status, e.attempt_count, e.last_error
                FROM skills s
                LEFT JOIN skill_embeddings e ON e.skill_id = s.id
                WHERE (%s::text IS NULL OR s.status = %s)
                  AND (%s::text IS NULL OR s.product_id = %s)
                ORDER BY s.updated_at DESC, s.id
                """,
                (status, status, product_id, product_id),
            )
            return [dict(row) for row in cur.fetchall()]


def embed_pending(settings, limit: int | None, retry_failed: bool) -> None:
    if retry_failed:
        with connect(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE skill_embeddings
                    SET status = 'pending', attempt_count = 0, last_error = NULL, updated_at = now()
                    WHERE status = 'failed'
                    """
                )
            conn.commit()
    encoder = build_encoder(settings.embedding)
    processed = succeeded = failed = 0
    while limit is None or processed < limit:
        batch_limit = min(settings.embedding.batch_size, limit - processed) if limit is not None else settings.embedding.batch_size
        jobs = claim_embedding_jobs(batch_limit, settings)
        if not jobs:
            break
        try:
            vectors = encoder.encode([build_skill_embedding_text(job) for job in jobs])
            save_skill_vectors(jobs, vectors, settings)
            succeeded += len(jobs)
        except Exception as exc:
            mark_skill_embeddings_failed(jobs, exc, settings)
            failed += len(jobs)
            time.sleep(0.1)
        processed += len(jobs)
    print(f"skill_embedding_complete processed={processed} succeeded={succeeded} failed={failed}")


def claim_embedding_jobs(limit: int, settings) -> list[dict[str, Any]]:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.skill_id, s.title, s.description, s.domain, s.trigger_rules,
                       s.version AS skill_version, s.content_hash
                FROM skill_embeddings e
                JOIN skills s ON s.id = e.skill_id
                WHERE s.status = 'active'
                  AND (
                    e.embedding IS NULL
                    OR e.embedding_model <> %s
                    OR e.content_hash <> s.content_hash
                    OR e.skill_version <> s.version
                  )
                  AND e.attempt_count < %s
                ORDER BY e.skill_id
                FOR UPDATE OF e SKIP LOCKED
                LIMIT %s
                """,
                (settings.embedding.storage_model, settings.embedding.max_retries, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if rows:
                cur.execute(
                    """
                    UPDATE skill_embeddings
                    SET status = 'processing', attempt_count = attempt_count + 1, last_error = NULL, updated_at = now()
                    WHERE skill_id = ANY(%s)
                    """,
                    ([row["skill_id"] for row in rows],),
                )
        conn.commit()
    return rows


def save_skill_vectors(jobs: list[dict[str, Any]], vectors: list[list[float]], settings) -> None:
    if len(jobs) != len(vectors):
        raise ValueError("Skill encoder result count mismatch")
    with connect(settings) as conn:
        with conn.cursor() as cur:
            for job, vector in zip(jobs, vectors, strict=True):
                if len(vector) != settings.database.embedding_dim:
                    raise ValueError("Skill vector dimension mismatch")
                cur.execute(
                    """
                    UPDATE skill_embeddings
                    SET embedding = %s::vector, embedding_model = %s, embedding_dim = %s,
                        content_hash = %s, skill_version = %s, status = 'completed',
                        attempt_count = 0, last_error = NULL, updated_at = now()
                    WHERE skill_id = %s
                    """,
                    (
                        vector_literal(vector),
                        settings.embedding.storage_model,
                        settings.database.embedding_dim,
                        job["content_hash"],
                        job["skill_version"],
                        job["skill_id"],
                    ),
                )
        conn.commit()


def mark_skill_embeddings_failed(jobs: list[dict[str, Any]], exc: Exception, settings) -> None:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE skill_embeddings
                SET status = 'failed', last_error = %s, updated_at = now()
                WHERE skill_id = ANY(%s)
                """,
                (f"{type(exc).__name__}: {exc}"[:2000], [job["skill_id"] for job in jobs]),
            )
        conn.commit()


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


if __name__ == "__main__":
    main()
