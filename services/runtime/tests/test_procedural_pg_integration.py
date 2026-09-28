from __future__ import annotations

import os

import psycopg
import pytest

from aegora_runtime.config import load_settings
from aegora_runtime.data_ops.procedural_learning import ProceduralMemoryStore


pytestmark = pytest.mark.skipif(
    os.getenv("AEGORA_TEST_PG_INTEGRATION") != "1",
    reason="set AEGORA_TEST_PG_INTEGRATION=1 to run against isolated PostgreSQL",
)


def candidate_item(*, blocked: bool = False) -> dict:
    return {
        "type": "procedural_memory",
        "agent_id": "p6-agent",
        "fingerprint": "f" * 64,
        "cluster_key": "membership_help",
        "cluster_title": "会员功能怎么用",
        "promotion_status": "blocked" if blocked else "eligible",
        "promotion_reason": "negative_feedback_conflict" if blocked else "promotion_gate_passed",
        "evidence_count": 3,
        "positive_count": 2,
        "negative_count": 1 if blocked else 0,
        "negative_ratio": 1 / 3 if blocked else 0.0,
        "examples": ["会员功能怎么用"],
        "successful_response_examples": ["先确认会员状态，再按 FAQ 说明入口。"],
        "source_trace_ids": ["p6-t1", "p6-t2", "p6-t3"],
        "positive_trace_ids": ["p6-t1", "p6-t2"],
        "source_memory_candidate_ids": [],
    }


def test_procedural_memory_real_dedup_lineage_and_conflict_transition() -> None:
    settings = load_settings(env_path=None)
    store = ProceduralMemoryStore(settings)

    with psycopg.connect(settings.postgres.dsn, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM procedural_memories WHERE agent_id = 'p6-agent'")
            cur.execute(
                "DELETE FROM memory_candidates WHERE subject_user_id = 'p6-user'"
            )
            ids = []
            for index, trace_id in enumerate(["p6-t1", "p6-t2"], start=1):
                cur.execute(
                    """
                    INSERT INTO memory_candidates (
                        tenant_id,
                        subject_user_id,
                        namespace,
                        proposed_scope,
                        agent_id,
                        operation,
                        memory_type,
                        memory_key,
                        content,
                        evidence_type,
                        source_agent_id,
                        source_trace_id,
                        confidence,
                        importance,
                        status
                    )
                    VALUES (
                        'default', 'p6-user', 'preferences', 'user_global', 'p6-agent',
                        'upsert', 'semantic', %s, %s, 'explicit_fact',
                        'p6-agent', %s, 0.99, 0.8, 'applied'
                    )
                    RETURNING id
                    """,
                    (f"key-{index}", f"value-{index}", trace_id),
                )
                ids.append(int(cur.fetchone()["id"]))
        conn.commit()

    assert store.source_memory_candidate_ids(["p6-t1", "p6-t2"]) == ids

    item = candidate_item()
    item["source_memory_candidate_ids"] = ids
    first = store.upsert(item)
    second = store.upsert(item)

    assert first["id"] == second["id"]
    assert second["status"] == "candidate"
    assert second["source_memory_candidate_ids"] == ids

    store.mark_skill_drafted(
        procedural_memory_id=int(first["id"]),
        skill_name="membership_guidance",
        skill_draft_id="draft-p6-1",
    )

    conflicted = candidate_item(blocked=True)
    conflicted["source_memory_candidate_ids"] = ids
    after_conflict = store.upsert(conflicted)

    assert after_conflict["id"] == first["id"]
    assert after_conflict["status"] == "conflicted"
    assert after_conflict["promoted_skill_name"] == "membership_guidance"
    assert after_conflict["skill_draft_id"] == "draft-p6-1"
