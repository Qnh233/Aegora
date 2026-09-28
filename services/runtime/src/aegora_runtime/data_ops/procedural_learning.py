from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect


SAFETY_TERMS = {
    "password",
    "密码",
    "token",
    "api key",
    "apikey",
    "secret",
    "私钥",
    "身份证",
    "银行卡",
}


@dataclass(frozen=True)
class ProceduralPromotionPolicy:
    min_occurrences: int = 3
    min_positive_feedback: int = 2
    max_negative_ratio: float = 0.0

    def __post_init__(self) -> None:
        if self.min_occurrences < 2:
            raise ValueError("procedural min_occurrences must be >= 2")
        if self.min_positive_feedback < 1:
            raise ValueError("procedural min_positive_feedback must be >= 1")
        if not 0 <= self.max_negative_ratio <= 1:
            raise ValueError("procedural max_negative_ratio must be between 0 and 1")


def build_procedural_item(
    cluster: dict[str, Any],
    *,
    policy: ProceduralPromotionPolicy,
) -> dict[str, Any]:
    trace_ids = _unique_strings(cluster.get("source_trace_ids") or [])
    positive_trace_ids = _unique_strings(cluster.get("positive_trace_ids") or [])
    examples = _clean_examples(cluster.get("examples") or [])
    strategy_examples = _clean_examples(cluster.get("successful_response_examples") or [])
    positive_count = int(cluster.get("positive_count") or len(positive_trace_ids))
    negative_count = int(cluster.get("negative_count") or 0)
    evidence_count = len(trace_ids)
    feedback_total = positive_count + negative_count
    negative_ratio = negative_count / feedback_total if feedback_total else 0.0
    agent_id = str(cluster.get("agent_id") or "legacy")
    learning_policy = cluster.get("learning_policy") if isinstance(cluster.get("learning_policy"), dict) else {}

    status, reason = procedural_promotion_decision(
        agent_id=agent_id,
        examples=examples,
        evidence_count=evidence_count,
        positive_count=positive_count,
        negative_count=negative_count,
        negative_ratio=negative_ratio,
        strategy_example_count=len(strategy_examples),
        propose_skills=bool(learning_policy.get("propose_skills")),
        policy=policy,
    )
    fingerprint = procedural_fingerprint(
        agent_id=agent_id,
        cluster_key=str(cluster.get("key") or cluster.get("title") or ""),
    )
    return {
        "type": "procedural_memory",
        "agent_id": agent_id,
        "fingerprint": fingerprint,
        "cluster_key": str(cluster.get("key") or ""),
        "cluster_title": str(cluster.get("title") or "procedural memory")[:160],
        "promotion_status": status,
        "promotion_reason": reason,
        "count": int(cluster.get("count") or evidence_count),
        "evidence_count": evidence_count,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "negative_ratio": round(negative_ratio, 6),
        "examples": examples[:5],
        "successful_response_examples": strategy_examples[:5],
        "source_trace_ids": trace_ids[:50],
        "positive_trace_ids": positive_trace_ids[:50],
        "source_memory_candidate_ids": [],
        "learning_policy": {
            "propose_skills": bool(learning_policy.get("propose_skills")),
            "requires_human_review": True,
        },
    }


def procedural_promotion_decision(
    *,
    agent_id: str,
    examples: list[str],
    evidence_count: int,
    positive_count: int,
    negative_count: int,
    negative_ratio: float,
    strategy_example_count: int,
    propose_skills: bool,
    policy: ProceduralPromotionPolicy,
) -> tuple[str, str]:
    text = " ".join(examples).lower()
    if any(term in text for term in SAFETY_TERMS):
        return "blocked", "sensitive_evidence"
    if agent_id == "legacy":
        return "blocked", "agent_identity_required"
    if not propose_skills:
        return "blocked", "learning_policy_disallows_skill_proposal"
    if evidence_count < policy.min_occurrences:
        return "blocked", "insufficient_repetition"
    if positive_count < policy.min_positive_feedback:
        return "blocked", "insufficient_positive_feedback"
    if strategy_example_count < policy.min_positive_feedback:
        return "blocked", "successful_response_evidence_missing"
    if negative_count and negative_ratio > policy.max_negative_ratio:
        return "blocked", "negative_feedback_conflict"
    return "eligible", "promotion_gate_passed"


def procedural_fingerprint(*, agent_id: str, cluster_key: str) -> str:
    normalized = f"{agent_id.strip()}\n{cluster_key.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def can_create_skill_draft(row: dict[str, Any]) -> bool:
    return (
        str(row.get("status") or "") == "candidate"
        and not row.get("promoted_skill_name")
        and not row.get("skill_draft_id")
    )


class ProceduralMemoryStore:
    """Canonical agent-level procedural memory candidates and promotion lineage."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def source_memory_candidate_ids(self, trace_ids: list[str]) -> list[int]:
        values = _unique_strings(trace_ids)
        if not values:
            return []
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM memory_candidates
                    WHERE source_trace_id = ANY(%s::text[])
                      AND status = 'applied'
                    ORDER BY id
                    """,
                    (values,),
                )
                return [int(row["id"]) for row in cur.fetchall()]

    def upsert(self, item: dict[str, Any]) -> dict[str, Any]:
        desired_status = "candidate" if item.get("promotion_status") == "eligible" else "blocked"
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO procedural_memories (
                        agent_id,
                        fingerprint,
                        cluster_key,
                        title,
                        status,
                        promotion_reason,
                        evidence_count,
                        positive_count,
                        negative_count,
                        negative_ratio,
                        source_trace_ids,
                        positive_trace_ids,
                        source_memory_candidate_ids,
                        examples,
                        strategy_examples,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, now()
                    )
                    ON CONFLICT (agent_id, fingerprint)
                    DO UPDATE SET
                        cluster_key = EXCLUDED.cluster_key,
                        title = EXCLUDED.title,
                        promotion_reason = EXCLUDED.promotion_reason,
                        evidence_count = EXCLUDED.evidence_count,
                        positive_count = EXCLUDED.positive_count,
                        negative_count = EXCLUDED.negative_count,
                        negative_ratio = EXCLUDED.negative_ratio,
                        source_trace_ids = EXCLUDED.source_trace_ids,
                        positive_trace_ids = EXCLUDED.positive_trace_ids,
                        source_memory_candidate_ids = EXCLUDED.source_memory_candidate_ids,
                        examples = EXCLUDED.examples,
                        strategy_examples = EXCLUDED.strategy_examples,
                        status = CASE
                            WHEN EXCLUDED.status = 'blocked'
                                 AND procedural_memories.status = 'skill_drafted'
                                THEN 'conflicted'
                            WHEN procedural_memories.status IN ('skill_drafted', 'rejected')
                                THEN procedural_memories.status
                            ELSE EXCLUDED.status
                        END,
                        updated_at = now()
                    RETURNING *
                    """,
                    (
                        item["agent_id"],
                        item["fingerprint"],
                        item.get("cluster_key") or "",
                        item.get("cluster_title") or "procedural memory",
                        desired_status,
                        item.get("promotion_reason") or "",
                        int(item.get("evidence_count") or 0),
                        int(item.get("positive_count") or 0),
                        int(item.get("negative_count") or 0),
                        float(item.get("negative_ratio") or 0.0),
                        json.dumps(item.get("source_trace_ids") or [], ensure_ascii=False),
                        json.dumps(item.get("positive_trace_ids") or [], ensure_ascii=False),
                        json.dumps(item.get("source_memory_candidate_ids") or [], ensure_ascii=False),
                        json.dumps(item.get("examples") or [], ensure_ascii=False),
                        json.dumps(item.get("successful_response_examples") or [], ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            raise RuntimeError("procedural memory upsert returned no row")
        return dict(row)

    def mark_skill_drafted(
        self,
        *,
        procedural_memory_id: int,
        skill_name: str,
        skill_draft_id: str | int,
    ) -> None:
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE procedural_memories
                    SET status = 'skill_drafted',
                        promoted_skill_name = %s,
                        skill_draft_id = %s,
                        updated_at = now()
                    WHERE id = %s
                      AND status = 'candidate'
                    """,
                    (skill_name, str(skill_draft_id), procedural_memory_id),
                )
            conn.commit()


def _clean_examples(values: list[Any]) -> list[str]:
    return _unique_strings(
        str(value).strip()[:1200]
        for value in values
        if str(value).strip()
    )


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
