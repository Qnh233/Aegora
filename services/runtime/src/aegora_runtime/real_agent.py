from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from aegora_runtime.agent_loop import (
    AgentDecision,
    AgentDependencies,
    AgentRequest,
    SelfCheckResult,
    ToolCall,
)
from aegora_runtime.config import Settings, load_settings
from aegora_runtime.deepseek import ChatMessage, DeepSeekClient, DeepSeekError
from aegora_runtime.demo_agent import AMBIGUOUS_QUERIES, SECURITY_PATTERNS, rrf_retrieve
from aegora_runtime.embeddings import Encoder, RemoteEmbeddingError, build_encoder
from aegora_runtime.local_aicoin_tools import build_aicoin_tool_executor
from aegora_runtime.skills import (
    has_retrievable_skills,
    public_skill,
    retrieve_skills,
    session_loaded_skill_ids,
    skills_by_ids,
)


ROUTES = {"faq_answer", "tool_call", "clarify", "handoff", "chat"}
TOOL_NAMES = {"search_faq", "lookup_faq_detail", "load_skill", "save_user_memory", "record_handoff"}


def build_real_dependencies(settings: Settings | None = None) -> AgentDependencies:
    cfg = settings or load_settings(validate_secrets=True)
    client = DeepSeekClient(cfg.deepseek)
    encoder = LazyBgeM3Encoder(cfg)
    tools = build_aicoin_tool_executor(
        cfg,
        search_faq_handler=lambda args, state: search_faq_tool(args, state, encoder, cfg),
    )
    return AgentDependencies(
        load_context=lambda request: load_context(request, cfg),
        load_skills=lambda request, context: load_skills(request, context, encoder, cfg),
        think=lambda state: think(state, client, cfg),
        run_tool=lambda name, args, state: tools.run(name, args, state),
        run_tools=lambda calls, state: tools.run_many(calls, state, max_parallel=cfg.agent.max_parallel_tool_calls),
        tool_catalog=tools.catalog,
        self_check=lambda state: self_check(state, client, cfg),
        pre_guard=pre_guard,
        model_usage=client.usage_snapshot,
        warmup=encoder.warmup,
    )


class LazyBgeM3Encoder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._encoder: Encoder | None = None
        self._lock = threading.Lock()

    def encode(self, texts):
        with self._lock:
            if self._encoder is None:
                self._encoder = build_encoder(self.settings.embedding)
            return self._encoder.encode(texts)

    def warmup(self) -> dict[str, Any]:
        with self._lock:
            if self._encoder is not None:
                return {"warmed": False, "reason": "already_loaded", "elapsed_ms": 0.0}
            started = time.perf_counter()
            self._encoder = build_encoder(self.settings.embedding)
            self._encoder.encode(["AiCoin FAQ 检索启动预热"])
            return {
                "warmed": True,
                "reason": f"{self.settings.embedding.provider}_startup_warmup",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }


def load_context(request: AgentRequest, settings: Settings | None = None) -> dict[str, Any]:
    session_skills = []
    if settings and settings.skills.enabled and request.session_id:
        rows = skills_by_ids(
            session_loaded_skill_ids(request.session_id, settings),
            request.product_id,
            request.domain_hint,
            settings,
        )
        for row in rows:
            item = dict(row)
            item["injection_reason"] = "available_from_session_context"
            item = public_skill(item)
            item["availability"] = "session_context"
            session_skills.append(item)
    return {
        "user_id": request.user_id,
        "session_id": request.session_id,
        "product_id": request.product_id,
        "domain_hint": request.domain_hint,
        "history": request.history[-8:],
        "session_skills": session_skills,
    }


def load_skills(
    request: AgentRequest,
    context: dict[str, Any],
    encoder: LazyBgeM3Encoder,
    settings: Settings,
) -> dict[str, Any]:
    query = fallback_standalone_query(request.query, context) if is_context_dependent_query(request.query) else request.query
    session_skills = list(context.get("session_skills") or [])
    if not settings.skills.enabled:
        return {"query": query, "candidates": [], "skills": [], "skipped": True, "reason": "skills_disabled"}
    if not has_retrievable_skills(request.product_id, request.domain_hint, settings):
        return {
            "query": query,
            "candidates": [],
            "skill_index": [],
            "skills": session_skills,
            "auto_skills": [],
            "session_skills": session_skills,
            "skipped": True,
            "reason": "no_retrievable_skills",
        }
    try:
        vector = encoder.encode([query])[0]
    except RemoteEmbeddingError as exc:
        return {
            "query": query,
            "candidates": [],
            "skill_index": [],
            "skills": session_skills,
            "auto_skills": [],
            "session_skills": session_skills,
            "skipped": True,
            "reason": f"embedding_unavailable:{type(exc).__name__}",
        }
    result = retrieve_skills(
        vector,
        query=query,
        product_id=request.product_id,
        domain_hint=request.domain_hint,
        session_id=request.session_id,
        settings=settings,
    )
    auto_skills = []
    for skill in result.get("skills") or []:
        item = dict(skill)
        item["availability"] = "current_auto"
        auto_skills.append(item)
    combined = auto_skills + [item for item in session_skills if item.get("id") not in {skill.get("id") for skill in auto_skills}]
    return {
        **result,
        "skills": combined,
        "auto_skills": auto_skills,
        "session_skills": session_skills,
    }


def classify_intent(
    query: str,
    context: dict[str, Any],
    client: DeepSeekClient,
    settings: Settings,
) -> dict[str, Any]:
    rule_result = rule_classify(query, context)
    skills = context.get("skills") or []
    auto_skills = context.get("auto_skills")
    if auto_skills is None:
        auto_skills = [item for item in skills if item.get("availability") != "session_context"]
    if rule_result and (not auto_skills or rule_result.get("route_hint") in {"safe", "handoff"}):
        return enrich_search_queries(rule_result, query)

    try:
        data = client.chat_json(
            [
                ChatMessage("system", CLASSIFY_SYSTEM),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "query": query,
                            "history": context.get("history") or [],
                            "rule_suggestion": rule_result or {},
                            "skills": skills,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            model=settings.deepseek.fast_model,
        )
        route_hint = str(data.get("route_hint") or "faq")
        if route_hint not in {"faq", "clarify", "handoff", "safe", "chat"}:
            route_hint = "faq"
        reason = str(data.get("reason") or "deepseek_classify")
        if route_hint == "handoff" and not requires_handoff(query, context):
            route_hint = "faq"
            reason = f"calibrated_from_handoff:{reason}"
        result = {
            "route_hint": route_hint,
            "category": data.get("category"),
            "confidence": float(data.get("confidence") or 0.0),
            "reason": reason,
            "standalone_query": normalize_standalone_query(data.get("standalone_query"), query),
            "standalone_queries": normalize_standalone_queries(data.get("standalone_queries")),
        }
        return enrich_search_queries(result, query)
    except (DeepSeekError, ValueError) as exc:
        return enrich_search_queries({
            "route_hint": "faq",
            "confidence": 0.0,
            "reason": f"classifier_fallback:{type(exc).__name__}",
            "standalone_query": fallback_standalone_query(query, context),
        }, query)


def retrieve(
    query: str,
    intent: dict[str, Any],
    top_k: int,
    encoder: Encoder,
    settings: Settings,
) -> list[dict[str, Any]]:
    if intent.get("route_hint") in {"clarify", "safe", "chat"}:
        return []
    return rrf_retrieve(query, top_k, encoder, settings)


def think(state: dict[str, Any], client: DeepSeekClient, settings: Settings) -> AgentDecision:
    if state.get("loop_mode") == "planner":
        return planner_think(state, client, settings)

    intent = state.get("intent") or {}
    route_hint = intent.get("route_hint")
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
    if route_hint == "chat":
        return chat_decision(state, client, settings)
    if route_hint == "handoff":
        return handoff_decision(state, client, settings)

    observations = state.get("tool_observations") or []
    if observations and observations[-1].get("tool_name") == "record_handoff":
        return AgentDecision(
            route="handoff",
            answer="这个问题需要人工进一步核实。我已记录处理摘要，请补充账号、订单或截图等必要信息后交由人工处理。",
            reason="handoff_recorded",
        )

    faqs = state.get("retrieved_faqs") or []
    if not faqs:
        return AgentDecision(
            route="clarify",
            answer="当前问题还不够具体，请补充产品端、功能名称和遇到的现象。",
            reason="no_retrieval_result",
        )

    try:
        data = client.chat_json(
            [
                ChatMessage("system", THINK_SYSTEM),
                ChatMessage("user", build_think_payload(state)),
            ],
            model=settings.deepseek.chat_model,
        )
        return calibrate_decision(parse_decision(data), state)
    except DeepSeekError:
        top = faqs[0]
        return AgentDecision(route="faq_answer", answer=str(top.get("response") or ""), reason=f"fallback_faq_id={top.get('faq_id')}")


def self_check(state: dict[str, Any], client: DeepSeekClient, settings: Settings) -> SelfCheckResult:
    answer = state.get("answer") or ""
    route = state.get("route")
    if not answer:
        return SelfCheckResult(passed=False, reason="missing_answer")
    if route == "faq_answer" and not state.get("retrieved_faqs") and not has_search_faq_evidence(state):
        return SelfCheckResult(passed=False, reason="faq_answer_without_evidence")
    if route == "faq_answer" and unsafe_answer(answer):
        return SelfCheckResult(passed=False, reason="unsafe_answer")
    if route in {"clarify", "handoff", "chat"}:
        return SelfCheckResult(passed=True, reason="local_non_faq_check")
    if not settings.agent.enable_llm_self_check:
        return SelfCheckResult(passed=True, reason="llm_self_check_disabled")

    try:
        data = client.chat_json(
            [
                ChatMessage("system", SELF_CHECK_SYSTEM),
                ChatMessage("user", build_self_check_payload(state)),
            ],
            model=settings.deepseek.fast_model,
        )
    except DeepSeekError:
        return SelfCheckResult(passed=True, reason="self_check_fallback")

    passed = bool(data.get("passed", True))
    return SelfCheckResult(
        passed=passed,
        reason=str(data.get("reason") or "deepseek_self_check"),
        revised_answer=data.get("revised_answer") if isinstance(data.get("revised_answer"), str) else None,
    )


def pre_guard(request: AgentRequest, context: dict[str, Any]) -> dict[str, Any]:
    normalized = request.query.strip()
    lower = normalized.lower()
    if any(re.search(pattern, lower, re.IGNORECASE) for pattern in SECURITY_PATTERNS):
        return {"route_hint": "safe", "confidence": 1.0, "reason": "unsafe_input"}
    if not normalized or len(normalized) <= 1:
        return {"route_hint": "clarify", "confidence": 1.0, "reason": "ultra_low_information"}
    if any(term in normalized for term in ["投诉", "充值没到账", "订单没到账", "风控", "解封", "禁言", "资金异常", "账号被封"]):
        return {"route_hint": "handoff", "confidence": 0.95, "reason": "guarded_handoff_boundary"}
    return {"route_hint": "planner", "confidence": 1.0, "reason": "planner_allowed"}


def planner_think(state: dict[str, Any], client: DeepSeekClient, settings: Settings) -> AgentDecision:
    intent = state.get("intent") or {}
    route_hint = intent.get("route_hint")
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
    if route_hint == "handoff":
        return handoff_decision(state, client, settings)

    observations = state.get("tool_observations") or []
    if observations and observations[-1].get("tool_name") == "record_handoff":
        return AgentDecision(
            route="handoff",
            answer="这个问题需要人工进一步核实。我已记录处理摘要，请补充账号、订单或截图等必要信息后交由人工处理。",
            reason="handoff_recorded",
        )

    try:
        data = client.chat_json(
            [
                ChatMessage("system", PLANNER_SYSTEM),
                ChatMessage("user", build_planner_payload(state)),
            ],
            model=settings.deepseek.chat_model,
        )
        decision = calibrate_planner_decision(parse_decision(data), state)
        return decision
    except DeepSeekError:
        return AgentDecision(
            route="clarify",
            answer="我暂时无法稳定处理这个问题。请补充产品端、功能名称和遇到的现象，我再继续帮你查。",
            reason="planner_fallback",
        )


def search_faq_tool(
    args: dict[str, Any],
    state: dict[str, Any],
    encoder: LazyBgeM3Encoder,
    settings: Settings,
) -> dict[str, Any]:
    query = str(args["query"]).strip()
    top_k = int(args.get("top_k") or settings.retrieval.top_k)
    top_k = max(1, min(top_k, 10))
    results = rrf_retrieve(query, top_k, encoder, settings)
    return {
        "query": query,
        "top_k": top_k,
        "results": [
            {
                "faq_id": item.get("faq_id"),
                "title": item.get("title"),
                "response": item.get("response"),
                "response_pic_app_url": item.get("response_pic_app_url"),
                "response_pic_pc_url": item.get("response_pic_pc_url"),
                "source_ranks": item.get("source_ranks"),
            }
            for item in results
        ],
    }


def rule_classify(query: str, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    normalized = query.strip()
    lower = normalized.lower()
    has_history = bool((context or {}).get("history"))
    if any(re.search(pattern, lower, re.IGNORECASE) for pattern in SECURITY_PATTERNS):
        return {"route_hint": "safe", "confidence": 1.0, "reason": "unsafe_input"}
    if is_chat_query(normalized):
        return {"route_hint": "chat", "confidence": 1.0, "reason": "smalltalk_or_identity"}
    if has_history and is_context_dependent_query(normalized):
        return None
    if normalized in AMBIGUOUS_QUERIES or len(normalized) <= 1:
        return {"route_hint": "clarify", "confidence": 1.0, "reason": "insufficient_context"}
    if is_auth_howto_query(normalized):
        return {"route_hint": "faq", "confidence": 0.95, "reason": "authorization_howto"}
    if any(term in normalized for term in ["投诉", "充值没到账", "订单没到账", "风控", "解封", "禁言"]):
        return {"route_hint": "handoff", "confidence": 0.9, "reason": "account_or_service_sensitive"}
    if is_product_howto_query(normalized):
        return {"route_hint": "faq", "confidence": 0.9, "reason": "product_howto"}
    return None


def handoff_decision(state: dict[str, Any], client: DeepSeekClient, settings: Settings) -> AgentDecision:
    if not state.get("tool_observations"):
        query = state["request"].query
        return AgentDecision(
            route="tool_call",
            tool_name="record_handoff",
            tool_args={"reason": "handoff_route", "summary": query[:300]},
            reason="record_handoff_before_answer",
        )
    return AgentDecision(
        route="handoff",
        answer="这个问题需要人工进一步处理。请补充账号、订单号、截图或报错信息，人工会据此核实。",
        reason="handoff_required",
    )


def chat_decision(state: dict[str, Any], client: DeepSeekClient, settings: Settings) -> AgentDecision:
    request = state["request"]
    history = (state.get("context") or {}).get("history") or []
    try:
        answer = client.chat_text(
            build_chat_messages(request.query, history, state.get("skills") or []),
            model=settings.deepseek.chat_model,
            temperature=0.2,
        )
        return AgentDecision(route="chat", answer=answer.strip(), reason="llm_chat_without_rag")
    except DeepSeekError:
        return AgentDecision(route="chat", answer=chat_fallback_answer(), reason="llm_chat_fallback")


def build_chat_messages(
    query: str,
    history: list[dict[str, Any]],
    skills: list[dict[str, Any]] | None = None,
) -> list[ChatMessage]:
    messages = [ChatMessage("system", CHAT_SYSTEM)]
    if skills:
        messages.append(ChatMessage("system", skill_context_text(skills)))
    for item in history[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append(ChatMessage(role, content))
    messages.append(ChatMessage("user", query))
    return messages


def parse_decision(data: dict[str, Any]) -> AgentDecision:
    route = str(data.get("route") or "faq_answer")
    if route not in ROUTES:
        route = "faq_answer"
    tool_name = data.get("tool_name")
    if tool_name is not None and tool_name not in TOOL_NAMES:
        route = "handoff"
        tool_name = None
    tool_args = data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {}
    tool_calls = parse_tool_calls(data.get("tool_calls"))
    if data.get("tool_calls") is not None and not tool_calls:
        route = "handoff"
    if tool_calls:
        tool_name = tool_calls[0].tool_name
        tool_args = tool_calls[0].tool_args
    return AgentDecision(
        route=route,  # type: ignore[arg-type]
        answer=data.get("answer") if isinstance(data.get("answer"), str) else None,
        tool_name=tool_name,
        tool_args=tool_args,
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        tool_calls=tool_calls,
    )


def calibrate_decision(decision: AgentDecision, state: dict[str, Any]) -> AgentDecision:
    intent = state.get("intent") or {}
    faqs = state.get("retrieved_faqs") or []
    if (
        intent.get("route_hint") == "faq"
        and decision.route == "handoff"
        and decision.answer
        and faqs
        and not state.get("tool_observations")
    ):
        return AgentDecision(
            route="faq_answer",
            answer=decision.answer,
            reason=f"calibrated_from_handoff:{decision.reason or 'faq_evidence_present'}",
        )
    return decision


def calibrate_planner_decision(decision: AgentDecision, state: dict[str, Any]) -> AgentDecision:
    if decision.route == "tool_call" and decision.tool_calls:
        calls = []
        for call in decision.tool_calls:
            args = dict(call.tool_args)
            if call.tool_name == "search_faq":
                if not str(args.get("query") or "").strip():
                    args["query"] = standalone_query_for_planner(state)
                if not isinstance(args.get("top_k"), int):
                    args["top_k"] = 5
            calls.append(ToolCall(call.call_id, call.tool_name, args, call.reason))
        first = calls[0]
        return AgentDecision(
            route="tool_call",
            tool_name=first.tool_name,
            tool_args=first.tool_args,
            tool_calls=tuple(calls),
            reason=decision.reason,
        )
    if decision.route == "tool_call" and decision.tool_name == "search_faq":
        args = dict(decision.tool_args or {})
        query = str(args.get("query") or "").strip()
        if not query:
            args["query"] = standalone_query_for_planner(state)
        if not isinstance(args.get("top_k"), int):
            args["top_k"] = 5
        return AgentDecision(
            route="tool_call",
            tool_name="search_faq",
            tool_args=args,
            reason=decision.reason or "planner_search_faq",
        )
    if decision.route != "tool_call" and not has_answer(decision):
        return AgentDecision(
            route="clarify",
            answer="我暂时无法生成完整回答，请补充具体问题后再试。",
            reason="planner_invalid_terminal_missing_answer",
        )
    return decision


def has_answer(decision: AgentDecision) -> bool:
    return bool(decision.answer and decision.answer.strip())


def build_think_payload(state: dict[str, Any]) -> str:
    request = state["request"]
    intent = state.get("intent") or {}
    evidence = [
        {
            "faq_id": item.get("faq_id"),
            "title": item.get("title"),
            "response": item.get("response"),
            "source_ranks": item.get("source_ranks"),
        }
        for item in (state.get("retrieved_faqs") or [])[:5]
    ]
    payload = {
        "query": request.query,
        "standalone_query": intent.get("standalone_query") or request.query,
        "history": (state.get("context") or {}).get("history") or [],
        "intent": intent,
        "retrieved_faqs": evidence,
        "tool_observations": state.get("tool_observations") or [],
        "skills": state.get("skills") or [],
        "skill_index": (state.get("context") or {}).get("skill_index") or [],
        "answer_contract": answer_contract(state),
        "available_tools": state.get("tool_catalog") or sorted(TOOL_NAMES),
    }
    return json.dumps(payload, ensure_ascii=False)


def build_planner_payload(state: dict[str, Any]) -> str:
    request = state["request"]
    observations = state.get("tool_observations") or []
    payload = {
        "query": request.query,
        "standalone_query": standalone_query_for_planner(state),
        "history": (state.get("context") or {}).get("history") or [],
        "pre_guard": state.get("intent") or {},
        "tool_observations": observations,
        "retrieved_faqs": [
            {
                "faq_id": item.get("faq_id"),
                "title": item.get("title"),
                "response": item.get("response"),
                "response_pic_app_url": item.get("response_pic_app_url"),
                "response_pic_pc_url": item.get("response_pic_pc_url"),
                "source_ranks": item.get("source_ranks"),
            }
            for item in (state.get("retrieved_faqs") or [])[:5]
        ],
        "skills": state.get("skills") or [],
        "skill_index": (state.get("context") or {}).get("skill_index") or [],
        "answer_contract": answer_contract(state),
        "available_tools": state.get("tool_catalog") or sorted(TOOL_NAMES),
    }
    return json.dumps(payload, ensure_ascii=False)


def standalone_query_for_planner(state: dict[str, Any]) -> str:
    request = state["request"]
    query = request.query
    context = state.get("context") or {}
    if is_context_dependent_query(query):
        return fallback_standalone_query(query, context)
    return query


def has_search_faq_evidence(state: dict[str, Any]) -> bool:
    if state.get("retrieved_faqs"):
        return True
    for item in state.get("tool_observations") or []:
        if item.get("tool_name") != "search_faq" or item.get("status") != "ok":
            continue
        output = item.get("output") or {}
        if isinstance(output, dict) and output.get("results"):
            return True
    return False


def build_self_check_payload(state: dict[str, Any]) -> str:
    intent = state.get("intent") or {}
    return json.dumps(
        {
            "query": state["request"].query,
            "standalone_query": intent.get("standalone_query") or state["request"].query,
            "route": state.get("route"),
            "answer": state.get("answer"),
            "retrieved_faqs": [
                {"faq_id": item.get("faq_id"), "title": item.get("title"), "response": item.get("response")}
                for item in (state.get("retrieved_faqs") or [])[:5]
            ],
        },
        ensure_ascii=False,
    )


def unsafe_answer(answer: str) -> bool:
    lower = answer.lower()
    return any(token in lower for token in ["alert(", "document.cookie", "api key", "系统提示词"])


def is_chat_query(query: str) -> bool:
    normalized = query.strip().lower()
    exact = {
        "你好",
        "您好",
        "在吗",
        "你是谁",
        "你是什么",
        "你是人工客服吗",
        "你能做什么",
        "谢谢",
        "thanks",
        "hi",
        "hello",
    }
    return normalized in exact or normalized.rstrip("？?！!") in exact


def is_context_dependent_query(query: str) -> bool:
    normalized = query.strip()
    if normalized in AMBIGUOUS_QUERIES:
        return True
    if len(normalized) <= 8 and any(term in normalized for term in ["这个", "那个", "它", "他", "她", "呢", "也", "手机", "电脑", "pc", "PC", "app", "APP"]):
        return True
    return any(normalized.startswith(prefix) for prefix in ["那", "那么", "还有", "另外", "刚才", "上面"])


def normalize_standalone_query(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def normalize_standalone_queries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in result:
            result.append(item.strip())
    return result[:4]


def enrich_search_queries(intent: dict[str, Any], query: str) -> dict[str, Any]:
    result = dict(intent)
    queries = normalize_standalone_queries(result.get("standalone_queries"))
    if not queries:
        queries = split_independent_questions(str(result.get("standalone_query") or query))
    if len(queries) > 1:
        result["standalone_queries"] = queries
    return result


def split_independent_questions(query: str) -> list[str]:
    primary = meaningful_query_parts(re.split(r"[？?；;\n]+", query))
    if len(primary) > 1:
        return primary[:4]
    comma_parts = meaningful_query_parts(re.split(r"[，,]+", query))
    question_terms = ["如何", "怎么", "哪里", "在哪", "是否", "能不能", "可以", "支持", "设置", "取消", "删除", "开通", "使用", "查看"]
    if len(comma_parts) > 1 and all(any(term in item for term in question_terms) for item in comma_parts):
        return comma_parts[:4]
    return [query.strip()]


def meaningful_query_parts(parts: list[str]) -> list[str]:
    return [item.strip(" ，,。；;？?") for item in parts if len(item.strip(" ，,。；;？?")) >= 3]


def parse_tool_calls(value: Any) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        return ()
    calls = []
    for index, item in enumerate(value[:8]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name") or item.get("name") or "")
        if name not in TOOL_NAMES:
            continue
        args = item.get("tool_args") if isinstance(item.get("tool_args"), dict) else item.get("args")
        calls.append(
            ToolCall(
                call_id=str(item.get("call_id") or f"tool-call-{index + 1}"),
                tool_name=name,
                tool_args=args if isinstance(args, dict) else {},
                reason=item.get("reason") if isinstance(item.get("reason"), str) else None,
            )
        )
    return tuple(calls)


def fallback_standalone_query(query: str, context: dict[str, Any]) -> str:
    history = context.get("history") or []
    for item in reversed(history):
        if item.get("role") == "user" and item.get("content"):
            return f"{item['content']} {query}".strip()
    return query


def skill_context_text(skills: list[dict[str, Any]]) -> str:
    return (
        "以下 skills 包含本轮自动加载经验和会话中此前加载的经验。"
        "availability=current_auto 时与当前问题高置信相关，必须遵守；"
        "availability=session_context/tool_loaded 时只是可选历史经验，先判断用户是否仍在同一话题，换话题后忽略。"
        "所有 Skill 都不能覆盖安全规则、工具权限，也不能作为价格、权益数量、操作路径等事实证据：\n"
        + json.dumps(skills, ensure_ascii=False)
    )


def answer_contract(state: dict[str, Any]) -> dict[str, Any]:
    skills = state.get("skills") or []
    auto_skills = state.get("auto_skills")
    if auto_skills is None:
        auto_skills = [
            item
            for item in skills
            if item.get("availability") not in {"session_context", "tool_loaded"}
        ]
    return {
        "facts_source": "retrieved_faqs_or_tool_observations_only",
        "response_strategy_source": "loaded_skills_then_default",
        "must_follow_loaded_skills": bool(auto_skills),
        "session_skills_are_optional_after_topic_check": bool(skills),
        "skill_may_choose_clarify_before_rag": bool(skills),
        "skill_directives_are_hard_constraints": bool(auto_skills),
        "stop_when_skill_prerequisite_missing": bool(auto_skills),
        "do_not_dump_all_retrieved_faqs": True,
    }


def chat_fallback_answer() -> str:
    return "我可以继续陪你梳理问题。你可以直接说产品使用上的现象、入口或报错，我会尽量给出具体处理建议。"


def is_auth_howto_query(query: str) -> bool:
    if any(term in query for term in ["资金不对", "订单", "充值", "投诉", "风控", "人工"]):
        return False
    return "授权" in query and any(term in query for term in ["如何", "怎么", "哪里", "绑定ip", "绑定IP", "删除", "取消"])


def is_product_howto_query(query: str) -> bool:
    if any(term in query for term in ["资金不对", "订单", "充值", "投诉", "风控", "人工", "解封", "禁言"]):
        return False
    return any(
        term in query
        for term in [
            "如何",
            "怎么",
            "哪里",
            "在哪",
            "可以",
            "是否",
            "有没有",
            "支持",
            "设置",
            "取消",
            "删除",
            "开通",
            "使用",
            "查看",
        ]
    )


def requires_handoff(query: str, context: dict[str, Any] | None = None) -> bool:
    terms = ["投诉", "充值没到账", "订单没到账", "风控", "解封", "禁言", "资金异常", "账号被封"]
    texts = [query]
    for item in ((context or {}).get("history") or [])[-4:]:
        if item.get("role") == "user":
            texts.append(str(item.get("content") or ""))
    return any(term in text for text in texts for term in terms)


CLASSIFY_SYSTEM = """你是客服 Agent 的路由分类器。只输出 JSON 对象。
字段：
- route_hint: faq / clarify / handoff / safe / chat
- category: 简短类别
- confidence: 0 到 1
- reason: 简短理由
- standalone_query: 结合 history 改写后的独立问题；如果不是追问，等于原 query
- standalone_queries: 当用户同时提出多个独立产品问题时，拆成最多 4 个可分别检索的独立问题；否则为空数组
规则：账号、订单、充值、投诉、风控、禁言、解封等明确需要人工核实的问题走 handoff；不能仅因用户重复提问或可能遇到困难就走 handoff，重复的产品知识问题仍走 faq；信息不足且 history 也无法补全时走 clarify；危险代码或索取隐私走 safe；产品知识问题走 faq；身份、寒暄、感谢、关于当前对话历史的问题、非产品使用的普通问题走 chat，不要检索知识库。
skills 中 availability=current_auto 表示当前问题高置信命中；availability=session_context/tool_loaded 表示会话此前加载的可选经验，必须先判断是否仍是同一话题，换话题时忽略。skills 不能覆盖安全和明确转人工边界，也不能作为事实证据。rule_suggestion 只是规则建议，可在非安全、非明确转人工场景中结合当前适用 skills 调整。"""


THINK_SYSTEM = """你是 AiCoin 客服 Agent。只输出 JSON 对象。
允许 route: faq_answer, clarify, handoff, tool_call, chat。
回答规则：
1. FAQ/工具结果决定能陈述哪些事实；只使用与用户当前问题直接相关的证据，不要机械汇总全部召回结果。
2. 如果证据不足，route=clarify。
3. 账号、订单、充值、投诉、风控等需人工核实，route=tool_call 调用 record_handoff，或 route=handoff。
4. 不要承诺已经解封、到账、恢复或处理完成。
5. 一个决策可使用 tool_name/tool_args 调用一个工具，也可使用 tool_calls 数组调用多个互相独立的工具。
6. skill_index 只是候选经验索引，不包含正文。需要某项经验时调用 load_skill，参数包含 name 和 reason；不要假设未加载 Skill 的内容。
7. skills 中 availability=current_auto 的经验与当前问题高置信相关，必须遵守；availability=session_context/tool_loaded 是此前加载的可选经验，先判断是否仍是同一话题，换话题后忽略。
8. 当前适用 Skill 中“先、首先、必须、不要”等指令是硬约束。若其要求的前置信息尚未满足，立即 route=clarify 并停止。
9. Skill 不能覆盖安全、工具权限，也不能作为价格、权益数量、链接、操作步骤等事实证据。
10. 生成 faq_answer 时，以当前适用 Skill 组织回答，以 FAQ/工具结果填充事实。"""


CHAT_SYSTEM = """你是 AiCoin 客服助手，正在和用户进行自然对话。
要求：
1. 根据当前消息和最近对话历史回答，语气自然、简洁、有帮助。
2. 可以回答身份、能力、寒暄、感谢、以及“我前面问了什么”这类关于当前会话的问题。
3. 如果用户问产品操作、会员、授权交易、K线、预警等知识问题，而当前没有 FAQ 证据，不要编造具体路径或规则；请引导用户明确问题后由知识库回答。
4. 涉及账号、订单、充值、风控、禁言、解封等需要核实的问题，不要承诺处理结果，只引导补充必要信息或转人工。
5. 若提供 skills，可按其经验组织回答，但不能把 skills 当作事实证据。"""


PLANNER_SYSTEM = """你是 AiCoin 客服 Planner Agent。只输出 JSON 对象。
允许 route: tool_call, faq_answer, clarify, handoff, chat。
可用工具：
- search_faq: 检索产品 FAQ，参数 {"query": string, "top_k": number}。
- lookup_faq_detail: 按 FAQ ID 查询完整详情，参数 {"faq_id": number}。
- load_skill: 从 skill_index 按名称加载完整经验，参数 {"name": string, "reason": string}。
- record_handoff: 记录人工处理摘要，参数 {"reason": string, "summary": string}。
- save_user_memory: 保存本会话内用户明确表达的偏好或稳定事实，参数 {"key": string, "value": string}。

决策规则：
1. 产品事实、功能入口、价格、权益、链接和操作步骤若没有证据，应 route=tool_call 调用 search_faq。
2. skill_index 只是候选经验索引。需要经验正文时调用 load_skill；不得猜测未加载 Skill 的内容。load_skill 是只读并行工具，可与其他互相独立的只读工具同批调用。
3. skills 中 availability=current_auto 的经验必须遵守；availability=session_context/tool_loaded 是此前加载的可选经验，先判断当前是否仍是同一话题，换话题后忽略。
4. 当前适用 Skill 中“先、首先、必须、不要”等指令是硬约束。若要求先确认需求，而用户尚未提供，必须立即 route=clarify。
5. 已有 search_faq 结果后，只使用与当前问题直接相关的证据，不要机械汇总全部召回结果。
6. 生成 faq_answer 时，FAQ/工具结果决定事实边界，当前适用 Skill 决定回答结构、澄清顺序和下一步引导。
7. 证据不足或问题仍不明确，route=clarify。
8. 账号、订单、充值、投诉、风控、禁言、解封等需要人工核实的问题，route=tool_call 调用 record_handoff 或 route=handoff；不要承诺已经处理完成。
9. 身份、寒暄、感谢、当前对话历史、非产品普通交流，route=chat，不要调用 search_faq。
10. 用户同时提出多个独立产品问题时，拆成多个 search_faq 调用并放入同一个 tool_calls 数组，不要用整段原文只检索一次。
11. 只有 available_tools 中 read_only=true 且 parallel_safe=true 的工具才允许放入同一批并行调用；写工具单独调用。
12. 单工具输出字段：route, reason, tool_name, tool_args。多工具输出字段：route, reason, tool_calls，其中每项包含 call_id, tool_name, tool_args, reason。
13. save_user_memory 只写本会话记忆，不代表跨系统用户画像；不要保存隐私、账号状态或未经确认的推断。
14. route 为 faq_answer、clarify、handoff 或 chat 时，answer 必须是非空字符串；只有 tool_call 可以不返回 answer。
15. 不要假设存在未提供的经验或外部信息。"""


SELF_CHECK_SYSTEM = """你是回答自检器。只输出 JSON 对象。
字段：passed(boolean), reason(string), revised_answer(string|null)。
检查回答是否基于证据、是否包含危险内容、是否越权承诺人工处理结果。"""
