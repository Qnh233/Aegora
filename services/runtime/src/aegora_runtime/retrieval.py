from __future__ import annotations

from typing import Any, Sequence

from aegora_runtime.config import Settings, load_settings
from aegora_runtime.db import connect
from aegora_runtime.embeddings import vector_literal


def vector_retrieve(
    query_vector: Sequence[float],
    top_k: int = 5,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    cfg = settings or load_settings()
    if len(query_vector) != cfg.database.embedding_dim:
        raise ValueError(
            f"query vector dimension={len(query_vector)}, expected={cfg.database.embedding_dim}"
        )
    vector = vector_literal(query_vector)
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    f.strapi_id AS faq_id,
                    f.title,
                    f.response,
                    f.response_pic_app_url,
                    f.response_pic_pc_url,
                    c.name AS raw_category,
                    c.project_category,
                    1 - (e.embedding <=> %s::vector) AS score
                FROM faq_embeddings e
                JOIN faqs f ON f.id = e.faq_id
                JOIN faq_categories c ON c.id = f.category_id
                WHERE f.deleted_at IS NULL
                  AND e.embedding IS NOT NULL
                  AND e.status = 'completed'
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, vector, top_k),
            )
            return [dict(row) for row in cur.fetchall()]


def fulltext_retrieve(
    query: str,
    top_k: int = 5,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    cfg = settings or load_settings()
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    f.strapi_id AS faq_id,
                    f.title,
                    f.response,
                    f.response_pic_app_url,
                    f.response_pic_pc_url,
                    c.name AS raw_category,
                    c.project_category,
                    ts_rank(f.search_document, app_to_tsquery(%s)) AS score
                FROM faqs f
                JOIN faq_categories c ON c.id = f.category_id
                WHERE f.deleted_at IS NULL
                  AND f.search_document @@ app_to_tsquery(%s)
                ORDER BY score DESC, f.strapi_id
                LIMIT %s
                """,
                (query, query, top_k),
            )
            return [dict(row) for row in cur.fetchall()]


def reciprocal_rank_fusion(
    rankings: dict[str, Sequence[dict[str, Any]]],
    top_k: int = 5,
    rrf_k: int = 60,
    tie_break_source: str | None = None,
) -> list[dict[str, Any]]:
    """Fuse heterogeneous retrieval rankings without comparing raw scores."""
    if top_k <= 0:
        raise ValueError("top_k must be greater than 0")
    if rrf_k <= 0:
        raise ValueError("rrf_k must be greater than 0")

    fused: dict[int, dict[str, Any]] = {}
    for source, results in rankings.items():
        for rank, result in enumerate(results, start=1):
            faq_id = int(result["faq_id"])
            item = fused.setdefault(
                faq_id,
                {
                    **result,
                    "faq_id": faq_id,
                    "score": 0.0,
                    "source_ranks": {},
                    "source_scores": {},
                },
            )
            item["score"] += 1.0 / (rrf_k + rank)
            item["source_ranks"][source] = rank
            item["source_scores"][source] = float(result["score"])

    ordered = sorted(
        fused.values(),
        key=lambda item: (
            -item["score"],
            item["source_ranks"].get(tie_break_source, float("inf")),
            min(item["source_ranks"].values()),
            item["faq_id"],
        ),
    )
    return ordered[:top_k]


def hybrid_retrieve(
    query: str,
    query_vector: Sequence[float],
    top_k: int | None = None,
    candidate_k: int | None = None,
    rrf_k: int | None = None,
    tie_break_source: str | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Retrieve lexical and semantic candidates, then fuse them with RRF."""
    cfg = settings or load_settings()
    top_k = top_k or cfg.retrieval.top_k
    candidate_k = candidate_k or cfg.retrieval.candidate_k
    rrf_k = rrf_k or cfg.retrieval.rrf_k
    tie_break_source = tie_break_source or cfg.retrieval.rrf_tie_break_source
    if candidate_k < top_k:
        raise ValueError("candidate_k must be greater than or equal to top_k")
    return reciprocal_rank_fusion(
        {
            "fulltext": fulltext_retrieve(query, candidate_k, cfg),
            "vector": vector_retrieve(query_vector, candidate_k, cfg),
        },
        top_k=top_k,
        rrf_k=rrf_k,
        tie_break_source=tie_break_source,
    )


def trusted_top1_images(retrieved_faqs: Sequence[dict[str, Any]], route: str | None) -> list[dict[str, Any]]:
    """Return Top1 images only when lexical and semantic retrieval agree."""
    if route != "faq_answer" or not retrieved_faqs:
        return []
    top = retrieved_faqs[0]
    source_ranks = top.get("source_ranks") or {}
    if not {"fulltext", "vector"}.issubset(source_ranks):
        return []

    images = []
    seen_urls = set()
    for platform, field in (
        ("APP", "response_pic_app_url"),
        ("PC", "response_pic_pc_url"),
    ):
        url = str(top.get(field) or "").strip()
        if not url.startswith(("https://", "http://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        images.append({"faq_id": top.get("faq_id"), "platform": platform, "url": url})
    return images
