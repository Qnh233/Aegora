from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.deepseek import ChatMessage, DeepSeekClient
from aegora_runtime.embeddings import build_encoder
from aegora_runtime.strapi import StrapiClient, collection_endpoint

from aegora_runtime.data_ops.skill_drafts import write_skill_draft


HANDOFF_TERMS = {"充值", "提现", "到账", "订单", "退款", "封号", "解封", "风控", "禁言", "账号"}
SAFETY_TERMS = {"密码", "token", "api key", "私钥", "身份证"}
PUNCT_TRANSLATION = str.maketrans("", "", "，。！？!?、,.；;：:（）()【】[]\"'`")


@dataclass
class ReflectionResult:
    metrics: dict[str, Any]
    items: list[dict[str, Any]]
    created_skill_draft_ids: list[Any]
    period_start: datetime
    period_end: datetime


def run_weekly_reflection(
    settings: Settings,
    *,
    days: int,
    dry_run: bool,
    write_drafts: bool,
    min_cluster_size: int,
    min_negative_feedback: int,
) -> ReflectionResult:
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=days)
    client = StrapiClient(settings.strapi)
    messages = load_messages(client, settings, period_start)
    feedback = load_feedback(client, settings, period_start)
    user_by_trace = {
        str(row.get("trace_id")): row
        for row in messages
        if row.get("role") == "user" and row.get("trace_id") and row.get("content")
    }
    negative_feedback = [row for row in feedback if row.get("rating") == "negative"]
    negative_trace_ids = {str(row.get("trace_id")) for row in negative_feedback if row.get("trace_id")}
    clusters = cluster_user_messages(messages, negative_trace_ids, settings)
    items = [
        build_report_item(cluster, min_negative_feedback)
        for cluster in clusters
        if cluster["count"] >= min_cluster_size or cluster["negative_count"] >= min_negative_feedback
    ]
    created_ids: list[Any] = []
    if write_drafts and not dry_run:
        examples = load_skill_examples(settings)
        for item in items:
            if item["type"] == "skill_candidate":
                draft = skill_draft_from_item(item, settings, period_start, period_end, examples)
                created = write_skill_draft(settings, draft)
                created_ids.append(created.get("documentId") or created.get("id") or created.get("name"))
    metrics = {
        "period_days": days,
        "messages": len(messages),
        "user_messages": sum(1 for row in messages if row.get("role") == "user"),
        "feedback": len(feedback),
        "negative_feedback": len(negative_feedback),
        "negative_traces_with_user_message": sum(1 for trace_id in negative_trace_ids if trace_id in user_by_trace),
        "clusters": len(clusters),
        "report_items": len(items),
        "skill_drafts": len(created_ids),
        "dry_run": dry_run,
    }
    return ReflectionResult(metrics, items, created_ids, period_start, period_end)


def load_messages(client: StrapiClient, settings: Settings, period_start: datetime) -> list[dict[str, Any]]:
    return client.list(
        collection_endpoint(settings, "chat_messages"),
        filters={"filters[createdAt][$gte]": period_start.isoformat()},
        sort="createdAt:desc",
        page_size=100,
        max_items=int(os.environ.get("DATA_OPS_REFLECTION_MAX_MESSAGES", "2000")),
    )


def load_feedback(client: StrapiClient, settings: Settings, period_start: datetime) -> list[dict[str, Any]]:
    return client.list(
        collection_endpoint(settings, "message_feedback"),
        filters={"filters[createdAt][$gte]": period_start.isoformat()},
        sort="createdAt:desc",
        page_size=100,
        max_items=int(os.environ.get("DATA_OPS_REFLECTION_MAX_FEEDBACK", "1000")),
    )


def cluster_user_messages(
    messages: list[dict[str, Any]],
    negative_trace_ids: set[str],
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    if settings and os.environ.get("DATA_OPS_REFLECTION_CLUSTER_MODE", "embedding") == "embedding":
        try:
            return cluster_user_messages_by_embedding(messages, negative_trace_ids, settings)
        except Exception:
            pass
    return cluster_user_messages_by_rule(messages, negative_trace_ids)


def cluster_user_messages_by_rule(messages: list[dict[str, Any]], negative_trace_ids: set[str]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in messages:
        if row.get("role") != "user" or not row.get("content") or not evidence_capture_allowed(row):
            continue
        buckets[(message_agent_id(row), cluster_key(str(row["content"])))].append(row)
    return build_clusters(buckets.values(), negative_trace_ids)


def cluster_user_messages_by_embedding(
    messages: list[dict[str, Any]],
    negative_trace_ids: set[str],
    settings: Settings,
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in messages
        if row.get("role") == "user" and row.get("content") and evidence_capture_allowed(row)
    ]
    if not rows:
        return []
    threshold = float(os.environ.get("DATA_OPS_REFLECTION_CLUSTER_THRESHOLD", "0.82"))
    rows_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_agent[message_agent_id(row)].append(row)
    groups: list[list[dict[str, Any]]] = []
    encoder = build_encoder(settings.embedding)
    for agent_rows in rows_by_agent.values():
        vectors = encoder.encode([str(row["content"]) for row in agent_rows])
        agent_groups: list[list[dict[str, Any]]] = []
        centroids: list[list[float]] = []
        for row, vector in zip(agent_rows, vectors, strict=True):
            best_index = -1
            best_score = -1.0
            for index, centroid in enumerate(centroids):
                score = cosine(vector, centroid)
                if score > best_score:
                    best_score = score
                    best_index = index
            if best_index >= 0 and best_score >= threshold:
                agent_groups[best_index].append(row)
                centroids[best_index] = mean_vector([centroids[best_index], vector])
            else:
                agent_groups.append([row])
                centroids.append(vector)
        groups.extend(agent_groups)
    return build_clusters(groups, negative_trace_ids)


def build_clusters(groups, negative_trace_ids: set[str]) -> list[dict[str, Any]]:
    clusters = []
    for rows in groups:
        traces = [str(row.get("trace_id")) for row in rows if row.get("trace_id")]
        negative_count = sum(1 for trace_id in traces if trace_id in negative_trace_ids)
        examples = [str(row.get("content") or "").strip() for row in rows[:5]]
        agent_id = message_agent_id(rows[0]) if rows else "legacy"
        policies = [message_learning_policy(row) for row in rows]
        clusters.append(
            {
                "agent_id": agent_id,
                "key": cluster_key(examples[0] if examples else ""),
                "title": examples[0][:80] if examples else "empty",
                "count": len(rows),
                "negative_count": negative_count,
                "examples": examples,
                "source_trace_ids": traces[:20],
                "learning_policy": {
                    "propose_skills": bool(policies) and all(bool(policy["propose_skills"]) for policy in policies),
                    "requires_human_review": True
                    if not policies
                    else any(bool(policy["requires_human_review"]) for policy in policies),
                },
            }
        )
    return sorted(clusters, key=lambda item: (item["negative_count"], item["count"]), reverse=True)


def message_agent_id(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    request_metadata = (
        metadata.get("request_metadata") if isinstance(metadata.get("request_metadata"), dict) else {}
    )
    value = metadata.get("agent_id") or request_metadata.get("agent_id")
    return str(value).strip() if value not in (None, "") else "legacy"


def message_learning_policy(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    request_metadata = metadata.get("request_metadata") if isinstance(metadata.get("request_metadata"), dict) else {}
    raw = request_metadata.get("learning_policy")
    if not isinstance(raw, dict):
        return {"capture_evidence": True, "propose_skills": False, "requires_human_review": True}
    return {
        "capture_evidence": bool(raw.get("capture_evidence", True)),
        "propose_skills": bool(raw.get("propose_skills", False)),
        "requires_human_review": bool(raw.get("requires_human_review", True)),
    }


def evidence_capture_allowed(row: dict[str, Any]) -> bool:
    return bool(message_learning_policy(row)["capture_evidence"])


def cluster_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", text.lower())
    normalized = normalized.translate(PUNCT_TRANSLATION)
    return normalized[:48] or "empty"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    return [sum(vector[i] for vector in vectors) / len(vectors) for i in range(size)]


def build_report_item(cluster: dict[str, Any], min_negative_feedback: int) -> dict[str, Any]:
    text = " ".join(cluster["examples"])
    if any(term in text.lower() for term in SAFETY_TERMS):
        item_type = "safety_review"
        suggestion = "疑似敏感信息或安全边界问题，只进入报告，不自动沉淀经验。"
    elif any(term in text for term in HANDOFF_TERMS):
        item_type = "handoff_rule"
        suggestion = "建议人工确认是否需要明确转人工边界。"
    elif cluster["negative_count"] >= min_negative_feedback and bool(
        (cluster.get("learning_policy") or {}).get("propose_skills")
    ):
        item_type = "skill_candidate"
        suggestion = "建议生成 Skill 草稿，沉淀客服处理策略。"
    elif cluster["negative_count"] >= min_negative_feedback:
        item_type = "learning_review"
        suggestion = "达到候选阈值，但该 Agent 未授权生成 Skill 草稿，仅进入人工学习复核。"
    else:
        item_type = "faq_gap"
        suggestion = "建议人工检查是否缺少 FAQ 或现有 FAQ 表达不完整。"
    return {
        "type": item_type,
        "agent_id": cluster.get("agent_id") or "legacy",
        "cluster_title": cluster["title"],
        "count": cluster["count"],
        "negative_count": cluster["negative_count"],
        "suggestion": suggestion,
        "examples": cluster["examples"],
        "source_trace_ids": cluster["source_trace_ids"],
        "learning_policy": cluster.get("learning_policy") or {},
    }


def skill_draft_from_item(
    item: dict[str, Any],
    settings: Settings,
    period_start: datetime,
    period_end: datetime,
    examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    skill_examples = examples or []
    try:
        return llm_skill_draft_from_item(item, settings, skill_examples)
    except Exception:
        return fallback_skill_draft_from_item(item, settings)


def llm_skill_draft_from_item(
    item: dict[str, Any],
    settings: Settings,
    skill_examples: list[dict[str, Any]],
) -> dict[str, Any]:
    client = DeepSeekClient(settings.deepseek)
    data = client.chat_json(
        [
            ChatMessage(
                role="system",
                content=(
                    "你是客服经验库编辑，只输出 JSON。根据用户问题聚类生成一条 Skill 草稿。"
                    "字段：name,title,description,content,skill_type,trigger_rules。"
                    "name 用英文小写 snake_case，必须有业务语义，不要日期、hash、reflection。"
                    "title 用中文概括场景，不超过 24 字，不要写“反思经验”。"
                    "description 只描述适用问题，不要写来源、负反馈、近7天、自动生成、草稿。"
                    "content 写客服处理策略，不要写生成原因、证据来源、反思过程。"
                    "skill_type 只能是 guidance/commercial/workflow/safety。"
                    "trigger_rules 只能使用 include_terms、exclude_terms、explicit_intents、strong_related_terms、trigger_examples。"
                    "不要编造价格、权益数量、链接或产品事实；事实必须要求继续检索 FAQ/RAG。"
                ),
            ),
            ChatMessage(
                role="user",
                content=(
                    "现有 Skill 样例：\n"
                    f"{skill_examples}\n\n"
                    "待沉淀问题聚类：\n"
                    f"{item}\n\n"
                    "请生成可直接给运营审核的 Skill 草稿 JSON。"
                ),
            ),
        ],
        model=settings.deepseek.fast_model,
        temperature=0.2,
    )
    return normalize_llm_draft(data, item, settings)


def normalize_llm_draft(data: dict[str, Any], item: dict[str, Any], settings: Settings) -> dict[str, Any]:
    examples = [str(text).strip() for text in item.get("examples", []) if str(text).strip()]
    name = semantic_name(str(data.get("name") or ""), item)
    title = clean_generated_text(str(data.get("title") or item["cluster_title"]))[:40]
    description = clean_generated_text(str(data.get("description") or title))[:160]
    content = clean_generated_text(str(data.get("content") or build_skill_content(item)))
    rules = data.get("trigger_rules") if isinstance(data.get("trigger_rules"), dict) else {}
    rules["trigger_examples"] = [str(text) for text in rules.get("trigger_examples") or examples[:5]][:5]
    return {
        "name": name,
        "title": title,
        "description": description,
        "content": content[: settings.skills.max_content_chars],
        "product_id": settings.app.product_id,
        "skill_type": str(data.get("skill_type") or "guidance")
        if data.get("skill_type") in {"guidance", "commercial", "workflow", "safety"}
        else "guidance",
        "source": "agent",
        "priority": 0,
        "trigger_rules": clean_trigger_rules(rules),
        "metadata": skill_draft_lineage(item),
    }


def fallback_skill_draft_from_item(item: dict[str, Any], settings: Settings) -> dict[str, Any]:
    examples = [str(text).strip() for text in item.get("examples", []) if str(text).strip()]
    title = summarize_title(item["cluster_title"])
    return {
        "name": semantic_name("", item),
        "title": title,
        "description": f"用户咨询{title}时的客服处理策略。",
        "content": build_skill_content(item),
        "product_id": settings.app.product_id,
        "skill_type": "guidance",
        "source": "agent",
        "priority": 0,
        "trigger_rules": {"trigger_examples": examples[:5]},
        "metadata": skill_draft_lineage(item),
    }


def skill_draft_lineage(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_agent_id": str(item.get("agent_id") or "legacy"),
        "source_trace_ids": [str(value) for value in item.get("source_trace_ids", [])][:20],
    }


def build_skill_content(item: dict[str, Any]) -> str:
    examples = "\n".join(f"- {text}" for text in item.get("examples", [])[:5])
    return (
        "适用场景：用户提出与以下样例相近的问题，且普通 FAQ 回答可能无法解决。\n"
        f"{examples}\n\n"
        "处理建议：先识别用户的具体诉求和前置条件；如果缺少关键信息，先澄清；"
        "如果涉及账号、订单、资金、风控等需要后台核验的问题，应引导转人工；"
        "如果是产品使用策略问题，结合已召回 FAQ 给出有依据的步骤化回答。"
    )


def load_skill_examples(settings: Settings, limit: int = 5) -> list[dict[str, Any]]:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, title, description, content, skill_type, trigger_rules
                FROM skills
                WHERE status = 'active'
                ORDER BY source = 'manual' DESC, priority DESC, id
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]


def clean_generated_text(text: str) -> str:
    blocked = ["反思经验：", "反思经验", "生成原因：", "生成原因", "近 7 天", "近7天", "自动生成", "草稿"]
    result = text.strip()
    for item in blocked:
        result = result.replace(item, "")
    return result.strip(" ：:\n")


def clean_trigger_rules(rules: dict[str, Any]) -> dict[str, list[str]]:
    allowed = {"include_terms", "exclude_terms", "explicit_intents", "strong_related_terms", "trigger_examples"}
    result = {}
    for key, value in rules.items():
        if key in allowed and isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            if cleaned:
                result[key] = cleaned[:10]
    return result


def semantic_name(value: str, item: dict[str, Any]) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if cleaned and not cleaned.startswith("reflection") and len(cleaned) >= 4:
        return cleaned[:64]
    key = cluster_key(item.get("cluster_title") or "")
    if "指标胜率" in key:
        return "strategy_membership_guidance"
    if "会员" in key:
        return "membership_guidance"
    if "授权" in key:
        return "exchange_auth_guidance"
    return "customer_service_guidance"


def summarize_title(text: str) -> str:
    cleaned = clean_generated_text(text)
    for prefix in ["你好", "您好", "请问", "我想问一下"]:
        cleaned = cleaned.removeprefix(prefix)
    cleaned = cleaned.strip("，。！？!?、,.；;：: ")
    if "指标胜率" in cleaned:
        return "指标胜率会员功能咨询"
    if "会员" in cleaned:
        return "会员功能咨询"
    if "授权" in cleaned:
        return "交易所授权咨询"
    return cleaned[:24] or "客服处理策略"
