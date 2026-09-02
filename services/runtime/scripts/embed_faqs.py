#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from collections.abc import Sequence

from aegora_runtime.config import load_settings
from aegora_runtime.db import connect
from aegora_runtime.embeddings import EmbeddingJob, build_embedding_text, build_encoder, vector_literal


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate BGE-M3 embeddings for FAQ rows.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum FAQ rows to process.")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true", help="Reset exhausted failed rows and retry them.")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    if args.batch_size:
        object.__setattr__(settings.embedding, "batch_size", args.batch_size)

    if args.retry_failed:
        reset_failed(settings.embedding.max_retries)

    if not has_pending_jobs(settings.embedding.max_retries, settings.embedding.storage_model):
        print(f"embedding_complete processed=0 succeeded=0 failed=0 status={embedding_status()}")
        return

    print(f"embedding_provider={settings.embedding.provider} model={settings.embedding.model}")
    encoder = build_encoder(settings.embedding)
    if encoder.dimension != settings.database.embedding_dim:
        raise ValueError(
            f"embedding dimension mismatch: model={encoder.dimension}, database={settings.database.embedding_dim}"
        )

    started = time.perf_counter()
    processed = succeeded = failed = 0
    while args.limit is None or processed < args.limit:
        remaining = None if args.limit is None else args.limit - processed
        batch_size = min(settings.embedding.batch_size, remaining) if remaining else settings.embedding.batch_size
        jobs = claim_jobs(batch_size, settings.embedding.max_retries, settings.embedding.storage_model)
        if not jobs:
            break

        try:
            vectors = encode_with_retry(encoder, [job.text for job in jobs], settings.embedding.max_retries)
            save_vectors(jobs, vectors, settings.embedding.storage_model, settings.database.embedding_dim)
            succeeded += len(jobs)
        except Exception as exc:
            mark_failed(jobs, exc)
            failed += len(jobs)
            print(f"batch_failed size={len(jobs)} error={type(exc).__name__}: {exc}")
        processed += len(jobs)
        print(f"progress processed={processed} succeeded={succeeded} failed={failed}")

    elapsed = time.perf_counter() - started
    status = embedding_status()
    print(
        "embedding_complete "
        f"processed={processed} succeeded={succeeded} failed={failed} "
        f"elapsed_seconds={elapsed:.2f} status={status}"
    )


def claim_jobs(limit: int, max_retries: int, embedding_model: str) -> list[EmbeddingJob]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    e.faq_id,
                    f.strapi_id,
                    f.version AS faq_version,
                    f.content_hash,
                    f.title,
                    f.faq,
                    f.keywords,
                    f.response
                FROM faq_embeddings e
                JOIN faqs f ON f.id = e.faq_id
                WHERE f.deleted_at IS NULL
                  AND (
                    e.embedding IS NULL
                    OR e.embedding_model <> %s
                    OR e.content_hash <> f.content_hash
                    OR e.faq_version <> f.version
                  )
                  AND e.attempt_count < %s
                ORDER BY
                    length(coalesce(f.title, ''))
                    + length(f.faq)
                    + length(coalesce(f.keywords, ''))
                    + length(f.response),
                    e.faq_id
                FOR UPDATE OF e SKIP LOCKED
                LIMIT %s
                """,
                (embedding_model, max_retries, limit),
            )
            rows = cur.fetchall()
            faq_ids = [row["faq_id"] for row in rows]
            if faq_ids:
                cur.execute(
                    """
                    UPDATE faq_embeddings
                    SET status = 'processing',
                        attempt_count = attempt_count + 1,
                        last_error = NULL,
                        updated_at = now()
                    WHERE faq_id = ANY(%s)
                    """,
                    (faq_ids,),
                )
        conn.commit()
    return [
        EmbeddingJob(
            faq_id=row["faq_id"],
            strapi_id=row["strapi_id"],
            faq_version=row["faq_version"],
            content_hash=row["content_hash"],
            text=build_embedding_text(row["title"], row["faq"], row["keywords"], row["response"]),
        )
        for row in rows
    ]


def has_pending_jobs(max_retries: int, embedding_model: str) -> bool:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM faq_embeddings e
                    JOIN faqs f ON f.id = e.faq_id
                    WHERE f.deleted_at IS NULL
                      AND (
                        e.embedding IS NULL
                        OR e.embedding_model <> %s
                        OR e.content_hash <> f.content_hash
                        OR e.faq_version <> f.version
                      )
                      AND e.attempt_count < %s
                ) AS pending
                """,
                (embedding_model, max_retries),
            )
            return bool(cur.fetchone()["pending"])


def encode_with_retry(encoder, texts: Sequence[str], max_retries: int) -> list[list[float]]:
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return encoder.encode(texts)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(min(attempt, 3))
    assert last_exc is not None
    raise last_exc


def save_vectors(jobs: list[EmbeddingJob], vectors: list[list[float]], model_name: str, dimension: int) -> None:
    if len(jobs) != len(vectors):
        raise ValueError(f"encoder returned {len(vectors)} vectors for {len(jobs)} jobs")
    with connect() as conn:
        with conn.cursor() as cur:
            for job, vector in zip(jobs, vectors, strict=True):
                if len(vector) != dimension:
                    raise ValueError(f"FAQ {job.strapi_id} vector dimension={len(vector)}, expected={dimension}")
                cur.execute(
                    """
                    UPDATE faq_embeddings
                    SET embedding = %s::vector,
                        embedding_model = %s,
                        embedding_dim = %s,
                        content_hash = %s,
                        faq_version = %s,
                        status = 'completed',
                        attempt_count = 0,
                        last_error = NULL,
                        updated_at = now()
                    WHERE faq_id = %s
                    """,
                    (
                        vector_literal(vector),
                        model_name,
                        dimension,
                        job.content_hash,
                        job.faq_version,
                        job.faq_id,
                    ),
                )
        conn.commit()


def mark_failed(jobs: list[EmbeddingJob], exc: Exception) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE faq_embeddings
                SET status = 'failed',
                    last_error = %s,
                    updated_at = now()
                WHERE faq_id = ANY(%s)
                """,
                (f"{type(exc).__name__}: {exc}"[:2000], [job.faq_id for job in jobs]),
            )
        conn.commit()


def reset_failed(max_retries: int) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE faq_embeddings
                SET status = 'pending',
                    attempt_count = 0,
                    last_error = NULL,
                    updated_at = now()
                WHERE status = 'failed' AND attempt_count >= %s
                """,
                (max_retries,),
            )
            print(f"reset_failed={cur.rowcount}")
        conn.commit()


def embedding_status() -> dict[str, int]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) AS total,
                    count(embedding) AS completed,
                    count(*) FILTER (WHERE status = 'failed') AS failed,
                    count(*) FILTER (WHERE embedding IS NULL AND status <> 'failed') AS pending
                FROM faq_embeddings
                """
            )
            return dict(cur.fetchone())


if __name__ == "__main__":
    main()
