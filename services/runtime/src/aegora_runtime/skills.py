from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.embeddings import vector_literal
from aegora_runtime.sessions import load_assistant_metadata


SKILL_TYPES = {"guidance", "commercial", "workflow", "safety"}
SKILL_SOURCES = {"manual", "agent"}
SKILL_STATUSES = {"draft", "active", "rejected", "archived"}
RULE_LIST_FIELDS = {"include_terms", "exclude_terms", "explicit_intents", "strong_related_terms", "trigger_examples"}
COMMERCIAL_EXPLICIT_TERMS = {"购买", "价格", "多少钱", "会员", "权益", "开通", "续费", "升级", "套餐", "优惠", "折扣"}
SENSITIVE_TERMS = {"api_key", "api key", "password", "密码", "token", "secret", "身份证", "私钥"}


def build_skill_embedding_text(skill: dict[str, Any]) -> str:
    rules = skill.get("trigger_rules") or {}
    examples = rules.get("trigger_examples") if isinstance(rules, dict) else []
    parts = [
        f"标题：{str(skill.get('title') or '').strip()}",
        f"描述：{str(skill.get('description') or '').strip()}",
    ]
    if skill.get("domain"):
        parts.append(f"领域：{skill['domain']}")
    if isinstance(examples, list) and examples:
        parts.append("触发示例：" + "；".join(str(item) for item in examples))
    return "\n".join(parts)


def skill_content_hash(skill: dict[str, Any]) -> str:
    payload = {
        key: skill.get(key)
        for key in [
            "name",
            "title",
            "description",
            "content",
            "product_id",
            "domain",
            "skill_type",
            "source",
            "priority",
            "trigger_rules",
        ]
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_skill(skill: dict[str, Any], max_content_chars: int) -> list[str]:
    errors = []
    for field in ["name", "title", "description", "content"]:
        if not isinstance(skill.get(field), str) or not skill[field].strip():
            errors.append(f"{field} 必须是非空字符串")
    if isinstance(skill.get("content"), str) and len(skill["content"]) > max_content_chars:
        errors.append(f"content 不能超过 {max_content_chars} 字")
    if skill.get("domain") and not skill.get("product_id"):
        errors.append("domain 有值时 product_id 不能为空")
    if skill.get("skill_type", "guidance") not in SKILL_TYPES:
        errors.append("skill_type 无效")
    if skill.get("source", "manual") not in SKILL_SOURCES:
        errors.append("source 无效")
    if skill.get("status", "draft") not in SKILL_STATUSES:
        errors.append("status 无效")
    priority = skill.get("priority", 0)
    if not isinstance(priority, int) or not -100 <= priority <= 100:
        errors.append("priority 必须是 -100 到 100 的整数")
    rules = skill.get("trigger_rules") or {}
    if not isinstance(rules, dict):
        errors.append("trigger_rules 必须是对象")
    else:
        for key, value in rules.items():
            if key not in RULE_LIST_FIELDS:
                errors.append(f"trigger_rules 不支持字段: {key}")
            elif not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                errors.append(f"trigger_rules.{key} 必须是非空字符串数组")
    lowered = f"{skill.get('content', '')} {skill.get('description', '')}".lower()
    if any(term in lowered for term in SENSITIVE_TERMS):
        errors.append("Skill 包含禁止保存的敏感信息")
    return errors


def agent_skill_promotion_errors(skill: dict[str, Any], reviewer: str | None = None) -> list[str]:
    """Agent 生成的 Skill 只有带可审计评测证据时才允许晋级。"""
    if skill.get("source", "manual") != "agent":
        return []

    metadata = skill.get("metadata") or {}
    if not isinstance(metadata, dict):
        return ["agent Skill metadata 必须是对象"]
    evaluation = metadata.get("evaluation") or {}
    if not isinstance(evaluation, dict):
        return ["agent Skill 晋级前必须提供 metadata.evaluation 对象"]

    errors = []
    if evaluation.get("status") != "passed":
        errors.append("agent Skill 晋级前必须通过评测（metadata.evaluation.status=passed）")
    if not isinstance(evaluation.get("dataset"), str) or not evaluation["dataset"].strip():
        errors.append("agent Skill 晋级前必须记录评测数据集 metadata.evaluation.dataset")
    metrics = evaluation.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        errors.append("agent Skill 晋级前必须记录非空 metadata.evaluation.metrics")
    if not isinstance(evaluation.get("evaluated_at"), str) or not evaluation["evaluated_at"].strip():
        errors.append("agent Skill 晋级前必须记录 metadata.evaluation.evaluated_at")
    expected_hash = str(skill.get("content_hash") or skill_content_hash(skill)).strip()
    actual_hash = str(evaluation.get("content_hash") or "").strip()
    if not actual_hash:
        errors.append("agent Skill 晋级前必须记录 metadata.evaluation.content_hash")
    elif actual_hash != expected_hash:
        errors.append("agent Skill 评测证据已过期：metadata.evaluation.content_hash 与当前内容不一致")
    reviewer_name = reviewer if reviewer is not None else skill.get("reviewed_by")
    if not isinstance(reviewer_name, str) or not reviewer_name.strip():
        errors.append("agent Skill 晋级前必须记录明确的人工审核者 reviewed_by")
    return errors


def retrieve_skills(
    query_vector: Sequence[float],
    *,
    query: str,
    product_id: str,
    domain_hint: str | None,
    session_id: str | None,
    settings: Settings,
) -> dict[str, Any]:
    if not settings.skills.enabled:
        return {"query": query, "candidates": [], "skills": [], "skipped": True, "reason": "skills_disabled"}
    if len(query_vector) != settings.database.embedding_dim:
        raise ValueError("Skill query vector dimension mismatch")
    candidates = skill_vector_candidates(query_vector, product_id, domain_hint, settings)
    repeated = commercial_skill_ids_in_session(session_id, settings)
    selected, decisions = select_skills(
        candidates,
        query=query,
        product_id=product_id,
        domain_hint=domain_hint,
        repeated_commercial_ids=repeated,
        settings=settings,
    )
    return {
        "query": query,
        "candidates": decisions,
        "skill_index": [skill_index_item(item) for item in candidates],
        "skills": selected,
        "skipped": False,
        "reason": "retrieved",
    }


def has_retrievable_skills(product_id: str, domain_hint: str | None, settings: Settings) -> bool:
    if not settings.skills.enabled:
        return False
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM skill_embeddings e
                    JOIN skills s ON s.id = e.skill_id
                    WHERE s.status = 'active'
                      AND e.status = 'completed'
                      AND e.embedding IS NOT NULL
                      AND (s.product_id IS NULL OR s.product_id = %s)
                      AND (
                          (%s::text IS NULL AND s.domain IS NULL)
                          OR (%s::text IS NOT NULL AND (s.domain IS NULL OR s.domain = %s))
                      )
                ) AS available
                """,
                (product_id, domain_hint, domain_hint, domain_hint),
            )
            return bool(cur.fetchone()["available"])


def skill_vector_candidates(
    query_vector: Sequence[float],
    product_id: str,
    domain_hint: str | None,
    settings: Settings,
) -> list[dict[str, Any]]:
    vector = vector_literal(query_vector)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.title,
                    s.description,
                    s.content,
                    s.product_id,
                    s.domain,
                    s.skill_type,
                    s.source,
                    s.priority,
                    s.trigger_rules,
                    s.metadata,
                    1 - (e.embedding <=> %s::vector) AS retrieval_score
                FROM skill_embeddings e
                JOIN skills s ON s.id = e.skill_id
                WHERE s.status = 'active'
                  AND e.status = 'completed'
                  AND e.embedding IS NOT NULL
                  AND (s.product_id IS NULL OR s.product_id = %s)
                  AND (
                      (%s::text IS NULL AND s.domain IS NULL)
                      OR (%s::text IS NOT NULL AND (s.domain IS NULL OR s.domain = %s))
                  )
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    vector,
                    product_id,
                    domain_hint,
                    domain_hint,
                    domain_hint,
                    vector,
                    settings.skills.candidate_k,
                ),
            )
            return [dict(row) for row in cur.fetchall()]


def commercial_skill_ids_in_session(session_id: str | None, settings: Settings) -> set[int]:
    if not session_id:
        return set()
    return {
        int(skill_id)
        for metadata in load_assistant_metadata(settings, session_id)
        for skill_id in (metadata.get("injected_skill_ids") or [])
        if isinstance(skill_id, int) or str(skill_id).isdigit()
    }


def session_loaded_skill_ids(session_id: str | None, settings: Settings) -> list[int]:
    """Return Skill IDs retained by the session, preserving first-load order."""
    if not session_id:
        return []
    result = []
    for metadata in load_assistant_metadata(settings, session_id):
        for skill_id in metadata.get("injected_skill_ids") or []:
            if (isinstance(skill_id, int) or str(skill_id).isdigit()) and int(skill_id) not in result:
                result.append(int(skill_id))
    return result


def skill_index_for_scope(
    scope: dict[str, object],
    *,
    settings: Settings,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return authorized Skill index entries for a runtime tool scope."""
    if not settings.skills.enabled:
        return []
    values = normalize_skill_scope(scope)
    if not values["skill_library_ids"]:
        return []
    where_sql, params = skill_scope_where(values)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    s.id,
                    s.name,
                    s.title,
                    s.description,
                    s.product_id,
                    s.domain,
                    s.skill_type,
                    s.source,
                    s.priority,
                    s.trigger_rules,
                    s.metadata,
                    0.0 AS retrieval_score
                FROM skill_embeddings e
                JOIN skills s ON s.id = e.skill_id
                WHERE s.status = 'active'
                  AND e.status = 'completed'
                  AND e.embedding IS NOT NULL
                  {where_sql}
                ORDER BY s.priority DESC, s.updated_at DESC, s.id DESC
                LIMIT %s
                """,
                (*params, limit),
            )
            return [skill_index_item(dict(row)) for row in cur.fetchall()]


def normalize_skill_scope(scope: dict[str, object] | None) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key in ("product_ids", "domains", "skill_library_ids"):
        raw = (scope or {}).get(key)
        if isinstance(raw, list):
            normalized[key] = sorted({str(item) for item in raw if isinstance(item, str) and item.strip()})
        else:
            normalized[key] = []
    return normalized


def skill_scope_where(values: dict[str, list[str]]) -> tuple[str, tuple[object, ...]]:
    clauses = []
    params: list[object] = []
    if values["product_ids"]:
        clauses.append("(s.product_id IS NULL OR s.product_id = ANY(%s))")
        params.append(values["product_ids"])
    if values["domains"]:
        clauses.append("(s.domain IS NULL OR s.domain = ANY(%s))")
        params.append(values["domains"])
    clauses.append(
        """
        COALESCE(
            s.metadata->>'skill_library_id',
            s.metadata->>'library_id'
        ) = ANY(%s)
        """
    )
    params.append(values["skill_library_ids"])
    return " AND " + " AND ".join(f"({clause})" for clause in clauses), tuple(params)


def skills_by_ids(
    skill_ids: Sequence[int],
    product_id: str,
    domain_hint: str | None,
    settings: Settings,
) -> list[dict[str, Any]]:
    ids = [int(skill_id) for skill_id in skill_ids]
    if not ids:
        return []
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.title,
                    s.description,
                    s.content,
                    s.product_id,
                    s.domain,
                    s.skill_type,
                    s.source,
                    s.priority,
                    s.trigger_rules,
                    s.metadata,
                    0.0 AS retrieval_score
                FROM skill_embeddings e
                JOIN skills s ON s.id = e.skill_id
                WHERE s.id = ANY(%s)
                  AND s.status = 'active'
                  AND e.status = 'completed'
                  AND e.embedding IS NOT NULL
                  AND (s.product_id IS NULL OR s.product_id = %s)
                  AND (
                      (%s::text IS NULL AND s.domain IS NULL)
                      OR (%s::text IS NOT NULL AND (s.domain IS NULL OR s.domain = %s))
                  )
                ORDER BY array_position(%s::int[], s.id)
                """,
                (ids, product_id, domain_hint, domain_hint, domain_hint, ids),
            )
            return [dict(row) for row in cur.fetchall()]


def load_skill_by_name(
    name: str,
    *,
    query: str,
    product_id: str,
    domain_hint: str | None,
    allowed_names: set[str],
    already_loaded_ids: set[int],
    settings: Settings,
    allowed_scope: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Load one indexed Skill without bypassing scope and commercial hard gates."""
    if name not in allowed_names:
        return {"loaded": False, "reason": "skill_not_in_available_index", "name": name}
    scope_values = normalize_skill_scope(allowed_scope)
    scope_sql = ""
    scope_params: tuple[object, ...] = ()
    if scope_values["skill_library_ids"]:
        scope_sql, scope_params = skill_scope_where(scope_values)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    s.id, s.name, s.title, s.description, s.content, s.product_id, s.domain,
                    s.skill_type, s.source, s.priority, s.trigger_rules, s.metadata, s.version,
                    0.0 AS retrieval_score
                FROM skill_embeddings e
                JOIN skills s ON s.id = e.skill_id
                WHERE s.name = %s
                  AND s.status = 'active'
                  AND e.status = 'completed'
                  AND e.embedding IS NOT NULL
                  AND (s.product_id IS NULL OR s.product_id = %s)
                  AND (
                      (%s::text IS NULL AND s.domain IS NULL)
                      OR (%s::text IS NOT NULL AND (s.domain IS NULL OR s.domain = %s))
                  )
                  {scope_sql}
                """,
                (name, product_id, domain_hint, domain_hint, domain_hint, *scope_params),
            )
            row = cur.fetchone()
    if not row:
        return {"loaded": False, "reason": "skill_unavailable_or_out_of_scope", "name": name}
    item = dict(row)
    rules = item.get("trigger_rules") if isinstance(item.get("trigger_rules"), dict) else {}
    if contains_any(query, rules.get("exclude_terms") or []):
        return {"loaded": False, "reason": "excluded_by_term", "name": name}
    if item.get("skill_type") == "commercial":
        strong_related = contains_any(query, rules.get("strong_related_terms") or [])
        if not contains_any(query, COMMERCIAL_EXPLICIT_TERMS) and not strong_related:
            return {"loaded": False, "reason": "commercial_gate_not_matched", "name": name}
    if int(item["id"]) in already_loaded_ids:
        return {
            "loaded": True,
            "already_loaded": True,
            "reason": "already_loaded_in_session",
            "skill": skill_reference(item),
        }
    item["injection_reason"] = "loaded_by_agent_tool"
    return {
        "loaded": True,
        "already_loaded": False,
        "reason": "loaded_by_agent_tool",
        "skill": public_skill(item),
        "version": int(item.get("version") or 1),
    }


def select_skills(
    candidates: list[dict[str, Any]],
    *,
    query: str,
    product_id: str,
    domain_hint: str | None,
    repeated_commercial_ids: set[int],
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = []
    decisions = []
    explicit_commercial = contains_any(query, COMMERCIAL_EXPLICIT_TERMS)
    for candidate in candidates:
        item = dict(candidate)
        rules = item.get("trigger_rules") if isinstance(item.get("trigger_rules"), dict) else {}
        raw_score = float(item.get("retrieval_score") or 0.0)
        threshold = settings.skills.commercial_min_score if item.get("skill_type") == "commercial" else settings.skills.min_score
        reason = "eligible"
        if item.get("status") not in {None, "active"}:
            reason = "inactive_status"
        elif item.get("product_id") not in {None, product_id}:
            reason = "cross_product_scope"
        elif not domain_hint and item.get("domain") is not None:
            reason = "domain_hint_missing"
        elif domain_hint and item.get("domain") not in {None, domain_hint}:
            reason = "cross_domain_scope"
        elif raw_score < threshold:
            reason = "below_score_threshold"
        elif contains_any(query, rules.get("exclude_terms") or []):
            reason = "excluded_by_term"
        elif rules.get("include_terms") and not contains_any(query, rules["include_terms"]):
            reason = "missing_include_term"
        elif rules.get("explicit_intents") and not contains_any(query, rules["explicit_intents"]):
            reason = "missing_explicit_intent"
        elif item.get("skill_type") == "commercial":
            strong_related = contains_any(query, rules.get("strong_related_terms") or [])
            if not explicit_commercial and not strong_related:
                reason = "commercial_gate_not_matched"
            elif int(item["id"]) in repeated_commercial_ids and not explicit_commercial:
                reason = "commercial_frequency_limited"
        adjusted = adjusted_score(item, raw_score, product_id, domain_hint, settings)
        decisions.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "retrieval_score": round(raw_score, 6),
                "adjusted_score": round(adjusted, 6),
                "decision": reason,
            }
        )
        if reason == "eligible":
            item["adjusted_score"] = adjusted
            if item.get("skill_type") == "commercial" and explicit_commercial:
                item["injection_reason"] = "matched_explicit_commercial_intent"
            elif item.get("skill_type") == "commercial":
                item["injection_reason"] = "matched_strong_related_trigger"
            else:
                item["injection_reason"] = "matched_skill_gate"
            eligible.append(item)
    eligible.sort(key=lambda item: (-item["adjusted_score"], -int(item.get("priority") or 0), int(item["id"])))
    return [public_skill(item) for item in eligible[: settings.skills.max_injected]], decisions


def adjusted_score(
    skill: dict[str, Any],
    raw_score: float,
    product_id: str,
    domain_hint: str | None,
    settings: Settings,
) -> float:
    score = raw_score + max(-100, min(100, int(skill.get("priority") or 0))) / 1000
    score += settings.skills.manual_boost if skill.get("source") == "manual" else -settings.skills.agent_penalty
    if skill.get("product_id") == product_id:
        score += settings.skills.product_boost
    if domain_hint and skill.get("domain") == domain_hint:
        score += settings.skills.domain_boost
    return score


def public_skill(skill: dict[str, Any]) -> dict[str, Any]:
    scope = "global"
    if skill.get("product_id"):
        scope = "product_domain" if skill.get("domain") else "product"
    return {
        "id": skill["id"],
        "name": skill["name"],
        "title": skill["title"],
        "content": skill["content"],
        "source": skill["source"],
        "skill_type": skill["skill_type"],
        "product_id": skill.get("product_id"),
        "domain": skill.get("domain"),
        "scope": scope,
        "retrieval_score": round(float(skill.get("retrieval_score") or 0.0), 6),
        "injection_reason": skill["injection_reason"],
    }


def skill_index_item(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": skill.get("id"),
        "name": skill.get("name"),
        "title": skill.get("title"),
        "description": skill.get("description"),
        "skill_type": skill.get("skill_type"),
        "scope": "product_domain" if skill.get("domain") else ("product" if skill.get("product_id") else "global"),
        "retrieval_score": round(float(skill.get("retrieval_score") or 0.0), 6),
    }


def skill_reference(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": skill.get("id"),
        "name": skill.get("name"),
        "title": skill.get("title"),
        "version": int(skill.get("version") or 1),
    }


def contains_any(text: str, terms: Sequence[str]) -> bool:
    lower = text.lower()
    return any(str(term).strip().lower() in lower for term in terms if str(term).strip())
