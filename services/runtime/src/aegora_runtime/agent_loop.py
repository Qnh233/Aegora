from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol
from uuid import uuid4

from pocoflow import Flow, Node, Store

from aegora_runtime.config import ConfigError, Settings, load_settings
from aegora_runtime.hooks import attach_default_hooks
from aegora_runtime.logging import get_logger, log_event
from aegora_runtime.observability import agent_trace_scope, finish_agent_trace
from aegora_runtime.retrieval import trusted_top1_images
from aegora_runtime.sessions import derive_user_id_from_session, normalize_session_id
from aegora_runtime.streaming import StreamHandler, emit_stream_event
from aegora_runtime.tools import safe_args


Route = Literal["faq_answer", "answer", "tool_call", "clarify", "handoff", "chat"]
LOGGER = get_logger("agent_loop")


@dataclass(frozen=True)
class AgentRequest:
    query: str
    product_id: str = "aicoin"
    domain_hint: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    stream_handler: StreamHandler | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        session_id = normalize_session_id(self.session_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "user_id", str(self.user_id).strip() if self.user_id else derive_user_id_from_session(session_id))


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class AgentDecision:
    route: Route
    answer: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class SelfCheckResult:
    passed: bool
    reason: str | None = None
    revised_answer: str | None = None


class ContextLoader(Protocol):
    def __call__(self, request: AgentRequest) -> dict[str, Any]: ...


class SkillLoader(Protocol):
    def __call__(self, request: AgentRequest, context: dict[str, Any]) -> dict[str, Any]: ...


class Thinker(Protocol):
    def __call__(self, state: dict[str, Any]) -> AgentDecision: ...


class ToolRunner(Protocol):
    def __call__(self, name: str, args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]: ...


class ToolBatchRunner(Protocol):
    def __call__(self, calls: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]: ...


class ToolCatalogProvider(Protocol):
    def __call__(self) -> list[dict[str, Any]]: ...


class SelfChecker(Protocol):
    def __call__(self, state: dict[str, Any]) -> SelfCheckResult: ...


class PreGuard(Protocol):
    def __call__(self, request: AgentRequest, context: dict[str, Any]) -> dict[str, Any]: ...


class ModelUsageProvider(Protocol):
    def __call__(self) -> dict[str, Any]: ...


class WarmupProvider(Protocol):
    def __call__(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentDependencies:
    load_context: ContextLoader
    think: Thinker
    run_tool: ToolRunner
    self_check: SelfChecker
    load_skills: SkillLoader | None = None
    run_tools: ToolBatchRunner | None = None
    tool_catalog: ToolCatalogProvider | None = None
    pre_guard: PreGuard | None = None
    model_usage: ModelUsageProvider | None = None
    warmup: WarmupProvider | None = None


def build_agent_flow(
    settings: Settings | None = None,
    *,
    attach_hooks: bool = True,
    enable_pocoflow_db: bool = True,
) -> Flow:
    return build_planner_flow(
        settings,
        attach_hooks=attach_hooks,
        enable_pocoflow_db=enable_pocoflow_db,
    )


def build_planner_flow(
    settings: Settings | None = None,
    *,
    attach_hooks: bool = True,
    enable_pocoflow_db: bool = True,
) -> Flow:
    cfg = settings or load_settings()

    load_context = LoadContextNode()
    load_skills = LoadSkillsNode()
    pre_guard = PreGuardNode()
    think = ThinkNode(max_iterations=cfg.agent.max_iterations)
    execute_tool = ExecuteToolNode()
    self_check = SelfCheckNode()

    load_context.then("ok", load_skills)
    load_skills.then("ok", pre_guard)
    pre_guard.then("ok", think)
    think.then("tool_call", execute_tool)
    think.then("answer", self_check)
    think.then("clarify", self_check)
    think.then("handoff", self_check)
    think.then("chat", self_check)
    execute_tool.then("ok", think)
    execute_tool.then("error", think)
    execute_tool.then("pending_approval", self_check)

    db_path = (
        cfg.observability.pocoflow_db_path
        if enable_pocoflow_db and cfg.observability.pocoflow_db_enabled
        else None
    )
    if db_path:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    flow = Flow(
        start=load_context,
        max_steps=max(8, cfg.agent.max_iterations * 4 + 8),
        db_path=db_path,
        flow_name="aegora_runtime_planner_loop",
    )
    if attach_hooks:
        attach_default_hooks(flow)
    return flow


def run_agent(
    request: AgentRequest,
    dependencies: AgentDependencies,
    settings: Settings | None = None,
    *,
    loop_mode: str | None = None,
    attach_hooks: bool = True,
    enable_pocoflow_db: bool = True,
    initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = settings or load_settings()
    selected_loop_mode = loop_mode or cfg.agent.loop_mode
    if selected_loop_mode != "planner":
        raise ConfigError("agent.loop_mode 当前仅支持 planner")
    if not request.trace_id:
        request = replace(request, trace_id=f"turn-{uuid4().hex}")
    usage_before = dependencies.model_usage() if dependencies.model_usage else None
    store = make_store(request, dependencies, selected_loop_mode)
    if usage_before is not None:
        store["_flow_usage_before"] = usage_before
    for key, value in (initial_state or {}).items():
        if key not in {"request", "deps"}:
            store[key] = value
    flow = build_planner_flow(
        cfg,
        attach_hooks=attach_hooks,
        enable_pocoflow_db=enable_pocoflow_db,
    )
    flow.run_id = request.trace_id
    with agent_trace_scope(request, cfg) as langfuse_span:
        result = flow.run(store)
        output = result.as_dict()
        finish_agent_trace(langfuse_span, output)
    output["trace_id"] = request.trace_id
    output["model_thinking_enabled"] = cfg.deepseek.enable_thinking
    output["answer_assets"] = trusted_top1_images(output.get("retrieved_faqs") or [], output.get("route"))
    if dependencies.model_usage and usage_before is not None:
        output["model_usage"] = diff_model_usage(usage_before, dependencies.model_usage())
    log_event(
        LOGGER,
        logging.INFO,
        "flow_snapshot",
        trace_id=request.trace_id,
        session_id=request.session_id,
        user_id=request.user_id,
        query=request.query,
        loop_mode=selected_loop_mode,
        model_thinking_enabled=cfg.deepseek.enable_thinking,
        status=output.get("status"),
        route=output.get("route"),
        intent=output.get("intent") or {},
        decision=output.get("decision") or {},
        loaded_skills=[skill_observation_summary(item) for item in output.get("skills") or []],
        skill_retrieval=skill_retrieval_log_summary(output.get("skill_retrieval") or {}),
        tool_observations=output.get("tool_observations") or [],
        observability=output.get("observability") or [],
    )
    return output


def make_store(request: AgentRequest, dependencies: AgentDependencies, loop_mode: str = "planner") -> Store:
    return Store(
        data={
            "request": request,
            "deps": dependencies,
            "tool_catalog": dependencies.tool_catalog() if dependencies.tool_catalog else [],
            "loop_mode": loop_mode,
            "trace_id": request.trace_id,
            "run_context": {
                "trace_id": request.trace_id,
                "session_id": request.session_id,
                "user_id": request.user_id,
                "query": request.query,
            },
            "trace": [],
            "observability": [],
            "stream_handler": request.stream_handler,
            "context": {},
            "skills": [],
            "auto_skills": [],
            "session_skills": [],
            "skill_retrieval": {},
            "intent": {},
            "retrieved_faqs": [],
            "tool_observations": [],
            "loop_count": 0,
            "route": None,
            "answer": None,
            "status": "running",
            "errors": [],
        },
        name="aegora_runtime_store",
    )


class LoadContextNode(Node):
    def prep(self, store: Store) -> tuple[AgentDependencies, AgentRequest]:
        return store["deps"], store["request"]

    def exec(self, prep_result: tuple[AgentDependencies, AgentRequest]) -> dict[str, Any]:
        deps, request = prep_result
        return deps.load_context(request)

    def post(self, store: Store, prep_result: Any, exec_result: dict[str, Any]) -> str:
        store["context"] = exec_result
        append_trace(store, self.name, "ok")
        return "ok"


class LoadSkillsNode(Node):
    def prep(self, store: Store) -> tuple[AgentDependencies, AgentRequest, dict[str, Any]]:
        return store["deps"], store["request"], store["context"]

    def exec(self, prep_result: tuple[AgentDependencies, AgentRequest, dict[str, Any]]) -> dict[str, Any]:
        deps, request, context = prep_result
        started = time.perf_counter()
        if not deps.load_skills:
            result = {"query": request.query, "candidates": [], "skills": [], "skipped": True, "reason": "no_skill_loader"}
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return result
        try:
            result = deps.load_skills(request, context)
        except Exception as exc:
            result = {
                "query": request.query,
                "candidates": [],
                "skills": [],
                "skipped": True,
                "reason": "skill_loader_error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    def post(self, store: Store, prep_result: Any, exec_result: dict[str, Any]) -> str:
        _, request, _ = prep_result
        skills = exec_result.get("skills") if isinstance(exec_result.get("skills"), list) else []
        store["skills"] = skills
        store["context"]["skills"] = skills
        store["auto_skills"] = exec_result.get("auto_skills") or []
        store["session_skills"] = exec_result.get("session_skills") or []
        store["context"]["auto_skills"] = store["auto_skills"]
        store["context"]["session_skills"] = store["session_skills"]
        store["context"]["skill_index"] = exec_result.get("skill_index") or []
        store["skill_retrieval"] = exec_result
        loaded_skills = [skill_observation_summary(item) for item in skills]
        payload = {
            "event": "skill_retrieval",
            "node": self.name,
            "query": exec_result.get("query") or request.query,
            "product_id": request.product_id,
            "domain_hint": request.domain_hint,
            "skipped": bool(exec_result.get("skipped")),
            "reason": exec_result.get("reason"),
            "error_type": exec_result.get("error_type"),
            "error": exec_result.get("error"),
            "candidate_count": len(exec_result.get("candidates") or []),
            "candidates": exec_result.get("candidates") or [],
            "skill_index": exec_result.get("skill_index") or [],
            "previous_skill_ids": exec_result.get("previous_skill_ids") or [],
            "injected_skill_ids": [item.get("id") for item in skills],
            "injected_skill_names": [item.get("name") for item in skills],
            "loaded_skills": loaded_skills,
            "elapsed_ms": exec_result.get("elapsed_ms"),
        }
        append_trace(store, self.name, "ok", payload)
        append_observability(store, payload)
        log_event(
            LOGGER,
            logging.INFO,
            "skill_retrieval",
            trace_id=request.trace_id,
            session_id=request.session_id,
            user_id=request.user_id,
            query=payload["query"],
            product_id=request.product_id,
            domain_hint=request.domain_hint,
            skipped=payload["skipped"],
            reason=payload["reason"],
            previous_skill_ids=payload["previous_skill_ids"],
            candidates=payload["candidates"],
            skill_index=payload["skill_index"],
            loaded_skills=loaded_skills,
            elapsed_ms=payload["elapsed_ms"],
        )
        return "ok"


class PreGuardNode(Node):
    def prep(self, store: Store) -> tuple[AgentDependencies, AgentRequest, dict[str, Any]]:
        return store["deps"], store["request"], store["context"]

    def exec(self, prep_result: tuple[AgentDependencies, AgentRequest, dict[str, Any]]) -> dict[str, Any]:
        deps, request, context = prep_result
        if deps.pre_guard:
            return deps.pre_guard(request, context)
        return {"route_hint": "planner", "reason": "no_pre_guard"}

    def post(self, store: Store, prep_result: Any, exec_result: dict[str, Any]) -> str:
        _, request, context = prep_result
        store["intent"] = exec_result
        payload = {
            "event": "pre_guard",
            "node": self.name,
            "query": request.query,
            "history_count": len(context.get("history") or []),
            "route_hint": exec_result.get("route_hint"),
            "reason": exec_result.get("reason"),
        }
        append_trace(store, self.name, "ok", payload)
        append_observability(store, payload)
        return "ok"


class ThinkNode(Node):
    def __init__(self, max_iterations: int):
        super().__init__()
        self.max_iterations = max_iterations

    def prep(self, store: Store) -> dict[str, Any]:
        return store.as_dict()

    def exec(self, prep_result: dict[str, Any]) -> AgentDecision:
        loop_count = int(prep_result.get("loop_count") or 0) + 1
        if loop_count > self.max_iterations:
            return AgentDecision(
                route="handoff",
                answer="当前问题需要人工进一步处理。",
                reason="max_iterations_exceeded",
            )
        return prep_result["deps"].think(prep_result)

    def post(self, store: Store, prep_result: Any, exec_result: AgentDecision) -> str:
        store["loop_count"] = int(store.get("loop_count") or 0) + 1
        store["decision"] = {
            "route": exec_result.route,
            "answer": exec_result.answer,
            "tool_name": exec_result.tool_name,
            "tool_args": exec_result.tool_args,
            "reason": exec_result.reason,
            "tool_calls": [tool_call_as_dict(item) for item in decision_tool_calls(exec_result)],
        }
        store["route"] = exec_result.route
        if exec_result.answer:
            store["answer"] = exec_result.answer
        append_trace(store, self.name, exec_result.route, store["decision"])
        append_observability(
            store,
            {
                "event": "agent_decision",
                "node": self.name,
                "loop_count": store["loop_count"],
                "route": exec_result.route,
                "reason": exec_result.reason,
                "tool_name": exec_result.tool_name,
                "tool_args": safe_args(exec_result.tool_args),
                "tool_calls": [
                    {**tool_call_as_dict(item), "tool_args": safe_args(item.tool_args)}
                    for item in decision_tool_calls(exec_result)
                ],
                "answer_preview": preview(exec_result.answer),
            },
        )
        if exec_result.route == "tool_call":
            return "tool_call"
        if exec_result.route in {"faq_answer", "answer"}:
            return "answer"
        return exec_result.route


class ExecuteToolNode(Node):
    def prep(self, store: Store) -> tuple[AgentDependencies, dict[str, Any], dict[str, Any]]:
        return store["deps"], store["decision"], store.as_dict()

    def exec(self, prep_result: tuple[AgentDependencies, dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        deps, decision, state = prep_result
        calls = decision.get("tool_calls") or []
        if not calls and decision.get("tool_name"):
            calls = [
                {
                    "call_id": "tool-call-1",
                    "tool_name": decision["tool_name"],
                    "tool_args": decision.get("tool_args") or {},
                    "reason": decision.get("reason"),
                }
            ]
        if not calls:
            raise ValueError("tool_call decision missing tool call")
        if deps.run_tools:
            return deps.run_tools(calls, state)
        return {
            "execution_mode": "sequential",
            "results": [run_tool_safely(deps, call, state) for call in calls],
        }

    def post(self, store: Store, prep_result: Any, exec_result: dict[str, Any]) -> str:
        decision = store.get("decision") or {}
        observations = list(store.get("tool_observations") or [])
        results = exec_result.get("results") if isinstance(exec_result.get("results"), list) else [exec_result]
        observations.extend(results)
        store["tool_observations"] = observations
        if exec_result.get("status") == "pending_approval":
            store["status"] = "pending_approval"
            store["route"] = "approval_required"
            store["answer"] = exec_result.get("answer") or "该操作需要审批后继续执行。"
            store["approval_requests"] = exec_result.get("approval_requests") or []
            payload = {
                "tool_observation_count": len(observations),
                "reason": decision.get("reason"),
                "execution_mode": exec_result.get("execution_mode") or "approval_required",
                "call_count": len(results),
                "latency_ms": exec_result.get("latency_ms"),
                "status": "pending_approval",
                "call_ids": [item.get("call_id") for item in results],
                "approval_ids": [
                    item.get("approval_id")
                    for item in store["approval_requests"]
                    if isinstance(item, dict)
                ],
            }
            append_trace(store, self.name, "pending_approval", payload)
            append_observability(store, {"event": "approval_required", "node": self.name, **payload})
            return "pending_approval"
        faq_result_sets = []
        faq_queries = []
        for result in results:
            if is_load_skill_tool(result.get("tool_name")):
                merge_tool_loaded_skill(store, result)
            if not is_search_faq_tool(result.get("tool_name")):
                continue
            output = result.get("output") or {}
            if isinstance(output, dict) and isinstance(output.get("results"), list):
                faq_queries.append(str(output.get("query") or (result.get("args") or {}).get("query") or ""))
                faq_result_sets.append(output["results"])
        if faq_result_sets:
            store["retrieved_faqs"] = merge_ranked_results(faq_queries, faq_result_sets)
        action = "error" if any(item.get("status") == "error" for item in results) else "ok"
        batch_payload = {
            "tool_observation_count": len(observations),
            "reason": decision.get("reason"),
            "execution_mode": exec_result.get("execution_mode") or "sequential",
            "call_count": len(results),
            "latency_ms": exec_result.get("latency_ms"),
            "status": action,
            "call_ids": [item.get("call_id") for item in results],
        }
        append_trace(store, self.name, action, batch_payload)
        append_observability(store, {"event": "tool_batch", "node": self.name, **batch_payload})
        for result in results:
            output = (
                result.get("output")
                if "output" in result
                else {key: value for key, value in result.items() if key not in {"call_id", "tool_name"}}
            )
            payload = {
                "call_id": result.get("call_id"),
                "reason": decision.get("reason"),
                "tool_name": result.get("tool_name"),
                "args": safe_args(result.get("args") or {}),
                "status": result.get("status"),
                "tool_metadata": result.get("tool_metadata") or {},
                "latency_ms": result.get("latency_ms"),
                "output": output,
                "error_type": result.get("error_type"),
                "error": result.get("error"),
            }
            append_observability(store, {"event": "tool_call", "node": self.name, **payload})
        return action


class SelfCheckNode(Node):
    def prep(self, store: Store) -> dict[str, Any]:
        return store.as_dict()

    def exec(self, prep_result: dict[str, Any]) -> SelfCheckResult:
        if prep_result.get("status") == "pending_approval":
            return SelfCheckResult(passed=True, reason="pending_approval")
        return prep_result["deps"].self_check(prep_result)

    def post(self, store: Store, prep_result: Any, exec_result: SelfCheckResult) -> str:
        if store.get("status") == "pending_approval":
            store["self_check"] = {
                "passed": True,
                "reason": "pending_approval",
            }
            append_trace(store, self.name, "pending_approval", store["self_check"])
            append_observability(store, {"event": "self_check", "node": self.name, **store["self_check"]})
            return "done"
        if exec_result.revised_answer:
            store["answer"] = exec_result.revised_answer
        store["self_check"] = {
            "passed": exec_result.passed,
            "reason": exec_result.reason,
        }
        if exec_result.passed:
            store["status"] = "completed"
        else:
            store["status"] = "failed"
            errors = list(store.get("errors") or [])
            errors.append(exec_result.reason or "self_check_failed")
            store["errors"] = errors
        append_trace(store, self.name, "done", store["self_check"])
        append_observability(store, {"event": "self_check", "node": self.name, **store["self_check"]})
        return "done"


def append_trace(store: Store, node: str, action: str, payload: dict[str, Any] | None = None) -> None:
    trace = list(store.get("trace") or [])
    trace.append({"node": node, "action": action, "payload": payload or {}})
    store["trace"] = trace


def append_observability(store: Store, event: dict[str, Any]) -> None:
    events = list(store.get("observability") or [])
    events.append(event)
    store["observability"] = events
    emit_stream_event(store, event)


def preview(value: str | None, limit: int = 120) -> str | None:
    if value is None:
        return None
    return value[:limit]


def skill_observation_summary(skill: dict[str, Any], content_limit: int = 180) -> dict[str, Any]:
    content = str(skill.get("content") or "").strip()
    return {
        "id": skill.get("id"),
        "name": skill.get("name"),
        "title": skill.get("title"),
        "skill_type": skill.get("skill_type"),
        "scope": skill.get("scope"),
        "availability": skill.get("availability"),
        "retrieval_score": skill.get("retrieval_score"),
        "injection_reason": skill.get("injection_reason"),
        "content_preview": content[:content_limit] + ("..." if len(content) > content_limit else ""),
    }


def skill_retrieval_log_summary(retrieval: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in retrieval.items()
        if key != "skills"
    } | {
        "loaded_skills": [skill_observation_summary(item) for item in retrieval.get("skills") or []],
    }


def merge_tool_loaded_skill(store: Store, result: dict[str, Any]) -> None:
    output = result.get("output") or {}
    skill = output.get("skill") if isinstance(output, dict) else None
    if not isinstance(skill, dict) or not output.get("loaded") or output.get("already_loaded"):
        return
    item = dict(skill)
    item["availability"] = "tool_loaded"
    skills = [existing for existing in (store.get("skills") or []) if existing.get("id") != item.get("id")]
    skills.append(item)
    store["skills"] = skills
    store["context"]["skills"] = skills


def is_load_skill_tool(tool_name: object) -> bool:
    return isinstance(tool_name, str) and (tool_name == "load_skill" or tool_name.endswith(".load_skill"))


def is_search_faq_tool(tool_name: object) -> bool:
    return isinstance(tool_name, str) and (tool_name == "search_faq" or tool_name.endswith(".search_faq"))


def decision_tool_calls(decision: AgentDecision) -> tuple[ToolCall, ...]:
    if decision.tool_calls:
        return decision.tool_calls
    if decision.tool_name:
        return (
            ToolCall(
                call_id="tool-call-1",
                tool_name=decision.tool_name,
                tool_args=decision.tool_args,
                reason=decision.reason,
            ),
        )
    return ()


def tool_call_as_dict(call: ToolCall) -> dict[str, Any]:
    return {
        "call_id": call.call_id,
        "tool_name": call.tool_name,
        "tool_args": call.tool_args,
        "reason": call.reason,
    }


def run_tool_safely(deps: AgentDependencies, call: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    try:
        result = deps.run_tool(call["tool_name"], call.get("tool_args") or {}, state)
        return {
            "call_id": call.get("call_id"),
            "tool_name": call["tool_name"],
            "args": call.get("tool_args") or {},
            **result,
        }
    except Exception as exc:
        return {
            "call_id": call.get("call_id"),
            "tool_name": call.get("tool_name"),
            "args": call.get("tool_args") or {},
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def merge_ranked_results(
    queries: list[str],
    result_sets: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[Any, int] = {}
    max_length = max((len(items) for items in result_sets), default=0)
    for rank in range(max_length):
        for query, items in zip(queries, result_sets):
            if rank >= len(items):
                continue
            item = dict(items[rank])
            key = item.get("faq_id") if item.get("faq_id") is not None else (item.get("title"), item.get("response"))
            if key in positions:
                existing = merged[positions[key]]
                matched = list(existing.get("matched_queries") or [])
                if query and query not in matched:
                    matched.append(query)
                existing["matched_queries"] = matched
                continue
            item["matched_queries"] = [query] if query else []
            positions[key] = len(merged)
            merged.append(item)
    return merged


def diff_model_usage(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    by_model = {}
    models = set((before.get("by_model") or {}).keys()) | set((after.get("by_model") or {}).keys())
    for model in sorted(models):
        old = (before.get("by_model") or {}).get(model, {})
        new = (after.get("by_model") or {}).get(model, {})
        delta = {
            key: int(new.get(key) or 0) - int(old.get(key) or 0)
            for key in ["calls", "failed_calls", "prompt_tokens", "completion_tokens", "total_tokens"]
        }
        if any(delta.values()):
            by_model[model] = delta
    return {
        "total_calls": int(after.get("total_calls") or 0) - int(before.get("total_calls") or 0),
        "failed_calls": int(after.get("failed_calls") or 0) - int(before.get("failed_calls") or 0),
        "by_model": by_model,
    }


def default_self_check(state: dict[str, Any]) -> SelfCheckResult:
    if state.get("answer"):
        return SelfCheckResult(passed=True)
    return SelfCheckResult(passed=False, reason="missing_answer")


def static_dependencies(answer: str) -> AgentDependencies:
    return AgentDependencies(
        load_context=lambda request: {"history": request.history},
        think=lambda state: AgentDecision(route="faq_answer", answer=answer),
        run_tool=lambda name, args, state: {"tool": name, "args": args},
        self_check=default_self_check,
    )
