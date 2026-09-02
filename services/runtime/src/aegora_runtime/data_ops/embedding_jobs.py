from __future__ import annotations

from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.embeddings import build_encoder

from scripts import embed_faqs
from scripts.skills import embed_pending as embed_pending_skills


def embed_pending(settings: Settings, content: str = "all", *, retry_failed: bool = False, limit: int | None = None) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if content in {"all", "faq"}:
        metrics["faq_before"] = embedding_status(settings, "faq_embeddings")
        metrics["faq"] = embed_pending_faqs(settings, retry_failed=retry_failed, limit=limit)
        metrics["faq_after"] = embedding_status(settings, "faq_embeddings")
    if content in {"all", "skills"}:
        metrics["skill_before"] = embedding_status(settings, "skill_embeddings")
        embed_pending_skills(settings, limit, retry_failed)
        metrics["skill_after"] = embedding_status(settings, "skill_embeddings")
    return metrics


def embed_pending_faqs(settings: Settings, *, retry_failed: bool, limit: int | None) -> dict[str, int]:
    if retry_failed:
        embed_faqs.reset_failed(settings.embedding.max_retries)
    if not embed_faqs.has_pending_jobs(settings.embedding.max_retries, settings.embedding.storage_model):
        return {"processed": 0, "succeeded": 0, "failed": 0}
    encoder = build_encoder(settings.embedding)
    if encoder.dimension != settings.database.embedding_dim:
        raise ValueError(
            f"embedding dimension mismatch: model={encoder.dimension}, database={settings.database.embedding_dim}"
        )
    processed = succeeded = failed = 0
    while limit is None or processed < limit:
        remaining = None if limit is None else limit - processed
        batch_size = min(settings.embedding.batch_size, remaining) if remaining else settings.embedding.batch_size
        jobs = embed_faqs.claim_jobs(batch_size, settings.embedding.max_retries, settings.embedding.storage_model)
        if not jobs:
            break
        try:
            vectors = embed_faqs.encode_with_retry(encoder, [job.text for job in jobs], settings.embedding.max_retries)
            embed_faqs.save_vectors(
                jobs,
                vectors,
                settings.embedding.storage_model,
                settings.database.embedding_dim,
            )
            succeeded += len(jobs)
        except Exception as exc:
            embed_faqs.mark_failed(jobs, exc)
            failed += len(jobs)
        processed += len(jobs)
    return {"processed": processed, "succeeded": succeeded, "failed": failed}


def embedding_status(settings: Settings, table: str) -> dict[str, int]:
    if table not in {"faq_embeddings", "skill_embeddings"}:
        raise ValueError(f"unsupported embedding table: {table}")
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    count(*) AS total,
                    count(embedding) AS completed_vectors,
                    count(*) FILTER (WHERE status = 'completed') AS completed,
                    count(*) FILTER (WHERE status = 'pending') AS pending,
                    count(*) FILTER (WHERE status = 'processing') AS processing,
                    count(*) FILTER (WHERE status = 'failed') AS failed
                FROM {table}
                """
            )
            return {key: int(value or 0) for key, value in dict(cur.fetchone()).items()}
