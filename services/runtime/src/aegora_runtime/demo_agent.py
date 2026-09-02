from __future__ import annotations

import re
from typing import Any

from aegora_runtime.agent_loop import (
    AgentDecision,
    AgentDependencies,
    AgentRequest,
    SelfCheckResult,
)
from aegora_runtime.config import Settings, load_settings
from aegora_runtime.embeddings import Encoder, RemoteEmbeddingError, build_encoder
from aegora_runtime.retrieval import fulltext_retrieve, hybrid_retrieve, reciprocal_rank_fusion


AMBIGUOUS_QUERIES = {"下载", "解封", "投屏", "会员", "人工", "客服", "打不开", "不行", "怎么弄"}
SECURITY_PATTERNS = (
    r"<script",
    r"onerror\s*=",
    r"javascript:",
    r"忽略之前所有指令",
    r"系统提示词",
    r"api key",
)


def build_demo_dependencies(settings: Settings | None = None) -> AgentDependencies:
    cfg = settings or load_settings()
    encoder = build_encoder(cfg.embedding)
    return AgentDependencies(
        load_context=load_context,
        think=think,
        run_tool=lambda name, args, state: {"status": "error", "error": f"demo tool not implemented: {name}"},
        self_check=self_check,
    )


def load_context(request: AgentRequest) -> dict[str, Any]:
    return {
        "user_id": request.user_id,
        "session_id": request.session_id,
        "history": request.history[-6:],
    }


def classify_intent(query: str, context: dict[str, Any]) -> dict[str, Any]:
    normalized = query.strip()
    lower = normalized.lower()
    if any(re.search(pattern, lower, re.IGNORECASE) for pattern in SECURITY_PATTERNS):
        return {"route_hint": "safe", "reason": "unsafe_input"}
    if normalized in AMBIGUOUS_QUERIES or len(normalized) <= 1:
        return {"route_hint": "clarify", "reason": "insufficient_context"}
    return {"route_hint": "faq", "reason": "demo_rrf_search"}


def rrf_retrieve(query: str, top_k: int, encoder: Encoder, settings: Settings) -> list[dict[str, Any]]:
    if not query.strip() or query.strip() in AMBIGUOUS_QUERIES:
        return []
    try:
        query_vector = encoder.encode([query])[0]
    except RemoteEmbeddingError:
        return reciprocal_rank_fusion(
            {"fulltext": fulltext_retrieve(query, settings.retrieval.candidate_k, settings)},
            top_k=top_k,
            rrf_k=settings.retrieval.rrf_k,
            tie_break_source="fulltext",
        )
    return hybrid_retrieve(query, query_vector, top_k=top_k, settings=settings)


def think(state: dict[str, Any]) -> AgentDecision:
    route_hint = state.get("intent", {}).get("route_hint")
    if route_hint == "safe":
        return AgentDecision(
            route="clarify",
            answer="该输入包含无法安全处理的指令或代码。请描述具体的产品使用问题。",
            reason="unsafe_input",
        )
    if route_hint == "clarify":
        return AgentDecision(
            route="clarify",
            answer="请补充更具体的问题，例如使用的平台、功能名称和遇到的现象。",
            reason="insufficient_context",
        )

    faqs = state.get("retrieved_faqs") or []
    if not faqs:
        return AgentDecision(
            route="handoff",
            answer="当前知识库没有检索到可靠答案，建议补充问题细节或转人工处理。",
            reason="no_retrieval_result",
        )

    top = faqs[0]
    return AgentDecision(
        route="faq_answer",
        answer=top["response"],
        reason=f"faq_id={top['faq_id']}",
    )


def self_check(state: dict[str, Any]) -> SelfCheckResult:
    answer = state.get("answer")
    if not answer:
        return SelfCheckResult(passed=False, reason="missing_answer")
    if state.get("route") == "faq_answer" and not state.get("retrieved_faqs"):
        return SelfCheckResult(passed=False, reason="faq_answer_without_evidence")
    return SelfCheckResult(passed=True)
