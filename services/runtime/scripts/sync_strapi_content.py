#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.db import connect
from aegora_runtime.skills import agent_skill_promotion_errors, skill_content_hash, validate_skill
from aegora_runtime.strapi import StrapiClient, collection_endpoint

from scripts.import_faqs import import_rows as import_faq_rows
from scripts.import_faqs import load_taxonomy
from scripts.skills import ensure_embedding_row


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Strapi-owned FAQ and Skill content into PG retrieval copies.")
    parser.add_argument("--content", choices=["all", "faq", "skills"], default="all")
    args = parser.parse_args()
    settings = load_settings(validate_secrets=True)
    client = StrapiClient(settings.strapi)

    if args.content in {"all", "faq"}:
        rows = client.list(collection_endpoint(settings, "faq_content"), page_size=100)
        with connect(settings) as conn:
            count = import_faq_rows(
                conn,
                rows,
                settings.embedding.storage_model,
                settings.database.embedding_dim,
                load_taxonomy(ROOT / "config" / "category_taxonomy.toml"),
            )
            conn.commit()
        print(f"faq_sync_ok rows={count}")

    if args.content in {"all", "skills"}:
        rows = client.list(collection_endpoint(settings, "skills_content"), page_size=100)
        print(f"skill_sync_ok rows={sync_skill_rows(rows, settings)}")


def sync_skill_rows(rows: list[dict[str, Any]], settings) -> int:
    normalized = [normalize_strapi_skill(row) for row in rows]
    errors = [
        f"{row.get('name')}: {error}"
        for row in normalized
        for error in (
            validate_skill(row, settings.skills.max_content_chars)
            + (agent_skill_promotion_errors(row, row.get("reviewed_by")) if row.get("status") == "active" else [])
        )
    ]
    if errors:
        raise ValueError("\n".join(errors))
    live_errors = procedural_skill_state_errors(normalized, settings)
    if live_errors:
        raise ValueError("\n".join(live_errors))
    with connect(settings) as conn:
        with conn.cursor() as cur:
            for row in normalized:
                row["content_hash"] = skill_content_hash(row)
                row["trigger_rules_json"] = json.dumps(row["trigger_rules"], ensure_ascii=False, sort_keys=True)
                row["metadata_json"] = json.dumps(row["metadata"], ensure_ascii=False, sort_keys=True)
                cur.execute(
                    """
                    INSERT INTO skills (
                        name, title, description, content, product_id, domain, skill_type, source,
                        status, priority, trigger_rules, metadata, content_hash, reviewed_by, reviewed_at, updated_at
                    )
                    VALUES (
                        %(name)s, %(title)s, %(description)s, %(content)s, %(product_id)s, %(domain)s,
                        %(skill_type)s, %(source)s, %(status)s, %(priority)s, %(trigger_rules_json)s::jsonb,
                        %(metadata_json)s::jsonb, %(content_hash)s, %(reviewed_by)s, %(reviewed_at)s, now()
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
                        status = excluded.status,
                        priority = excluded.priority,
                        trigger_rules = excluded.trigger_rules,
                        metadata = excluded.metadata,
                        reviewed_by = excluded.reviewed_by,
                        reviewed_at = excluded.reviewed_at,
                        version = CASE WHEN skills.content_hash <> excluded.content_hash THEN skills.version + 1 ELSE skills.version END,
                        content_hash = excluded.content_hash,
                        updated_at = now()
                    RETURNING id, version, content_hash
                    """,
                    row,
                )
                skill = cur.fetchone()
                ensure_embedding_row(cur, skill["id"], skill["version"], skill["content_hash"], settings)
        conn.commit()
    return len(normalized)


def procedural_skill_state_errors(
    rows: list[dict[str, Any]],
    settings,
) -> list[str]:
    candidates = [
        row
        for row in rows
        if row.get("status") == "active"
        and row.get("source") == "agent"
        and isinstance(row.get("metadata"), dict)
        and row["metadata"].get("source_kind") == "procedural_memory"
    ]
    if not candidates:
        return []

    ids = [
        int(row["metadata"]["procedural_memory_id"])
        for row in candidates
        if isinstance(row["metadata"].get("procedural_memory_id"), int)
    ]
    if not ids:
        return ["procedural active Skill 缺少可查询的 procedural_memory_id"]

    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    agent_id,
                    fingerprint,
                    status,
                    promoted_skill_name
                FROM procedural_memories
                WHERE id = ANY(%s)
                """,
                (ids,),
            )
            states = {int(row["id"]): dict(row) for row in cur.fetchall()}

    errors: list[str] = []
    for row in candidates:
        metadata = row["metadata"]
        memory_id = metadata.get("procedural_memory_id")
        if not isinstance(memory_id, int):
            errors.append(f"{row.get('name')}: procedural_memory_id 无效")
            continue
        state = states.get(memory_id)
        if state is None:
            errors.append(f"{row.get('name')}: procedural memory {memory_id} 不存在")
            continue
        if state.get("status") != "skill_drafted":
            errors.append(
                f"{row.get('name')}: procedural memory 当前状态为 {state.get('status')}，禁止晋级 active"
            )
        if str(state.get("fingerprint") or "") != str(metadata.get("procedural_fingerprint") or ""):
            errors.append(f"{row.get('name')}: procedural fingerprint 与当前治理事实不一致")
        if str(state.get("agent_id") or "") != str(metadata.get("source_agent_id") or ""):
            errors.append(f"{row.get('name')}: procedural source_agent_id 与当前治理事实不一致")
        promoted_name = str(state.get("promoted_skill_name") or "").strip()
        if promoted_name and promoted_name != str(row.get("name") or ""):
            errors.append(f"{row.get('name')}: procedural memory 已绑定其他 Skill {promoted_name}")
    return errors


def normalize_strapi_skill(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(row["name"]).strip(),
        "title": str(row["title"]).strip(),
        "description": str(row["description"]).strip(),
        "content": str(row["content"]).strip(),
        "product_id": clean(row.get("product_id")),
        "domain": clean(row.get("domain")),
        "skill_type": row.get("skill_type", "guidance"),
        "source": row.get("source", "manual"),
        "status": row.get("lifecycle_status", row.get("status", "draft")),
        "priority": int(row.get("priority", 0)),
        "trigger_rules": row.get("trigger_rules") or {},
        "metadata": {
            **(row.get("metadata") or {}),
            "strapi_id": row.get("id"),
            "strapi_document_id": row.get("documentId"),
        },
        "reviewed_by": clean(row.get("reviewed_by")),
        "reviewed_at": row.get("reviewed_at"),
    }


def clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


if __name__ == "__main__":
    main()
