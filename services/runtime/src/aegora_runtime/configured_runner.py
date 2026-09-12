from __future__ import annotations

import hashlib
import json
from typing import Any

from aegora_runtime.agent_loop import (
    AgentDecision,
    AgentDependencies,
    AgentRequest,
    ToolCall,
    default_self_check,
)
from aegora_runtime.execution_engine import ensure_trace_id, get_execution_engine
from aegora_runtime.config import Settings, load_settings
from aegora_runtime.deepseek import ChatMessage, DeepSeekClient, DeepSeekError
from aegora_runtime.local_aicoin_tools import build_aicoin_tool_runtime, write_tool_log
from aegora_runtime.real_agent import LazyBgeM3Encoder, search_faq_tool
from aegora_runtime.runtime_cache import get_runtime_config_cache
from aegora_runtime.runtime_approvals import (
    APPROVAL_APPROVED,
    APPROVAL_EXECUTED,
    APPROVAL_REJECTED,
    ApprovalDecision,
    ApprovalError,
    ApprovalStore,
    approval_response,
    default_approval_store,
)
from aegora_runtime import runtime_context as runtime_context_resolver
from aegora_runtime.sessions import load_session_context_views
from aegora_runtime.skills import skill_index_for_scope
from aegora_runtime.tool_adapters import build_runtime_tool_executor, warmup_runtime_mcp_clients


CONFIGURED_PROTOCOL = """你是配置驱动 Runner Core。只输出 JSON 对象。
允许 route: tool_call, answer, clarify, handoff, chat, faq_answer。
规则：
1. 系统提示词定义当前 Agent 的业务身份和回答风格。
2. 只能调用 available_tools 中列出的工具，tool_name 必须等于其中的 name。
3. 工具结果是事实边界；没有工具或证据时不要编造外部事实。
4. route=tool_call 时输出 tool_name/tool_args，或 tool_calls 数组；其他终态必须输出非空 answer。
5. 工具未列出、参数不确定或权限不足时，不要尝试绕过，直接回答无法执行或澄清。"""
CONFIGURED_PROMPT_ARTIFACT_VERSION = "v1-" + hashlib.sha256(CONFIGURED_PROTOCOL.encode("utf-8")).hexdigest()[:16]


class ApprovalGate:
    def __init__(
        self,
        runtime_context: dict[str, object],
        approval_store: ApprovalStore | None,
        *,
        run_metadata: dict[str, Any],
    ) -> None:
        self.runtime_context = runtime_context
        self.approval_store = approval_store
        self.run_metadata = run_metadata
        self.tool_by_id = {
            str(tool.get("tool_id")): dict(tool)
            for tool in runtime_context.get("tools") or []
            if isinstance(tool, dict) and tool.get("tool_id")
        }

    def run_one(
        self,
        name: str,
        args: dict[str, Any],
        state: dict[str, Any],
        execute: Any,
    ) -> dict[str, Any]:
        call = {
            "call_id": "tool-call-1",
            "tool_name": name,
            "tool_args": args,
        }
        pending = self.pending_result([call], state)
        if pending:
            return pending["results"][0]
        return execute(call)

    def run_many(
        self,
        calls: list[dict[str, Any]],
        state: dict[str, Any],
        execute: Any,
    ) -> dict[str, Any]:
        pending = self.pending_result(calls, state)
        if pending:
            return pending
        return execute(calls)

    def pending_result(self, calls: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any] | None:
        if not self.approval_store:
            return None
        for call in calls:
            tool = self.tool_by_id.get(str(call.get("tool_name") or ""))
            if tool and tool_requires_approval(tool):
                approval = self.create_approval(call, tool, state)
                return {
                    "status": "pending_approval",
                    "execution_mode": "approval_required",
                    "answer": "该操作需要审批后继续执行。",
                    "approval_requests": [approval],
                    "results": [
                        {
                            "call_id": call.get("call_id"),
                            "tool_name": call.get("tool_name"),
                            "args": call.get("tool_args") or {},
                            "status": "pending_approval",
                            "approval_id": approval["approval_id"],
                            "approval": approval,
                            "tool_metadata": approval.get("policy") or {},
                        }
                    ],
                }
        return None

    def create_approval(
        self,
        call: dict[str, Any],
        tool: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = build_approval_snapshot(
            state,
            runtime_context=self.runtime_context,
            tool_call=call,
            run_metadata=self.run_metadata,
        )
        approval = build_approval_record(snapshot, call, tool)
        return self.approval_store.create_pending(snapshot=snapshot, approval=approval)


def tool_requires_approval(tool: dict[str, Any]) -> bool:
    if bool(tool.get("requires_approval")):
        return True
    return str(tool.get("side_effect_level") or "none") in {"internal_write", "external_write", "destructive"}


def build_approval_snapshot(
    state: dict[str, Any],
    *,
    runtime_context: dict[str, object],
    tool_call: dict[str, Any],
    run_metadata: dict[str, Any],
) -> dict[str, Any]:
    request = state.get("request")
    return {
        "run_id": str(run_metadata.get("gateway_run_id") or run_metadata.get("run_id") or ""),
        "session_id": str(getattr(request, "session_id", "") or ""),
        "user_id": str(getattr(request, "user_id", "") or ""),
        "trace_id": str(getattr(request, "trace_id", "") or state.get("trace_id") or ""),
        "message": str(getattr(request, "query", "") or ""),
        "history": list(getattr(request, "history", []) or []),
        "runtime_context": runtime_context,
        "decision": state.get("decision") or {},
        "tool_call": dict(tool_call),
        "tool_observations": list(state.get("tool_observations") or []),
        "observability": list(state.get("observability") or []),
        "loop_count": int(state.get("loop_count") or 0),
        "metadata": dict(run_metadata),
    }


def build_approval_record(
    snapshot: dict[str, Any],
    call: dict[str, Any],
    tool: dict[str, Any],
) -> dict[str, Any]:
    release = snapshot["runtime_context"].get("release") or {}
    actor = snapshot["runtime_context"].get("actor") or {}
    return {
        "run_id": snapshot["run_id"] or snapshot["trace_id"],
        "session_id": snapshot["session_id"],
        "trace_id": snapshot["trace_id"],
        "agent_id": release.get("agent_id"),
        "release_id": release.get("release_id"),
        "actor_id": actor.get("actor_id"),
        "channel": snapshot["runtime_context"].get("channel"),
        "tool_id": tool.get("tool_id"),
        "runner_tool_id": tool.get("runner_tool_id"),
        "runner_name": tool.get("runner_name"),
        "tool_args": call.get("tool_args") or {},
        "policy": {
            "requires_approval": bool(tool.get("requires_approval")),
            "side_effect_level": tool.get("side_effect_level") or "none",
            "data_sensitivity": tool.get("data_sensitivity") or "internal",
            "network_access": tool.get("network_access") or "none",
            "read_only": bool(tool.get("read_only", True)),
        },
    }


def run_configured_turn(
    runtime_context: dict[str, object],
    *,
    message: str,
    session_id: str | None,
    user_id: str | None = None,
    history: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
    metadata: dict[str, Any] | None = None,
    approval_store: ApprovalStore | None = None,
    initial_tool_observations: list[dict[str, Any]] | None = None,
    initial_loop_count: int = 0,
    execution_engine: str | None = None,
) -> dict[str, Any]:
    cfg = settings or load_settings()
    engine = get_execution_engine(cfg, execution_engine)
    request = ensure_trace_id(
        AgentRequest(
            query=message.strip(),
            user_id=user_id,
            session_id=session_id,
            history=history or [],
        )
    )
    effective_metadata = {
        **(metadata or {}),
        "execution_engine": engine.name,
        "engine_schema_version": engine.schema_version,
        "engine_thread_id": str(request.trace_id),
    }
    dependencies = build_configured_dependencies(
        runtime_context,
        cfg,
        approval_store=approval_store,
        run_metadata=effective_metadata,
    )
    initial_state: dict[str, Any] = {}
    if initial_tool_observations:
        initial_state["tool_observations"] = initial_tool_observations
    if initial_loop_count:
        initial_state["loop_count"] = initial_loop_count
    result = engine.start(
        request,
        dependencies,
        cfg,
        initial_state=initial_state,
        metadata=effective_metadata,
        runtime_context=runtime_context,
    )
    result["runtime_context"] = summarize_runtime_context(runtime_context)
    result["request_metadata"] = effective_metadata
    return result


def build_configured_dependencies(
    runtime_context: dict[str, object],
    settings: Settings,
    *,
    approval_store: ApprovalStore | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> AgentDependencies:
    client = DeepSeekClient(settings.deepseek)
    encoder = LazyBgeM3Encoder(settings)
    record_mcp_warmup(runtime_context)
    tool_executor = build_runtime_tool_executor(
        runtime_context,
        tool_state=build_aicoin_tool_runtime(
            settings,
            lambda args, state: search_faq_tool(args, state, encoder, settings),
        ),
    )
    approval_gate = ApprovalGate(
        runtime_context,
        approval_store,
        run_metadata=run_metadata or {},
    )
    if settings.observability.tool_log_db_enabled:
        tool_executor.hooks.after_call.append(lambda event: safe_write_tool_log(settings, event))
        tool_executor.hooks.on_error.append(lambda event: safe_write_tool_log(settings, event))
    return AgentDependencies(
        load_context=lambda request: load_configured_context(request, runtime_context, settings),
        load_skills=load_configured_skills,
        think=lambda state: configured_planner_think(state, client, settings),
        run_tool=lambda name, args, state: approval_gate.run_one(
            name,
            args,
            state,
            # 审批通过后才进入统一工具执行器。
            lambda call: tool_executor.run(name, args, state, call_id=call.get("call_id")),
        ),
        run_tools=lambda calls, state: approval_gate.run_many(
            calls,
            state,
            lambda approved_calls: tool_executor.run_many(
                approved_calls,
                state,
                max_parallel=settings.agent.max_parallel_tool_calls,
            ),
        ),
        tool_catalog=tool_executor.catalog,
        self_check=default_self_check,
        pre_guard=lambda request, context: {"route_hint": "planner", "reason": "configured_runner"},
        model_usage=client.usage_snapshot,
    )


def load_configured_context(
    request: AgentRequest,
    runtime_context: dict[str, object],
    settings: Settings | None = None,
) -> dict[str, Any]:
    active_settings = settings or load_settings()
    skill_index = configured_skill_index(runtime_context, active_settings)
    try:
        context_views = load_session_context_views(
            active_settings,
            request.session_id,
            request.user_id,
        )
    except Exception:
        # 短期上下文读取失败时降级为空，避免历史存储问题阻断整轮执行。
        context_views = {"session_context": [], "session_user_context": []}
    return {
        "history": request.history[-8:],
        "session_context": context_views["session_context"],
        "session_user_context": context_views["session_user_context"],
        "runtime_context": runtime_context,
        "active_tool_ids": list(runtime_context.get("tool_ids") or []),
        "tool_scopes": runtime_context.get("tool_scopes") or {},
        "skill_index": skill_index,
        "skill_index_source": "runtime_tool_scope" if skill_index else "none",
    }


def load_configured_skills(request: AgentRequest, context: dict[str, Any]) -> dict[str, Any]:
    skill_index = context.get("skill_index") if isinstance(context.get("skill_index"), list) else []
    return {
        "query": request.query,
        "candidates": skill_index,
        "skills": [],
        "auto_skills": [],
        "session_skills": [],
        "skill_index": skill_index,
        "skipped": True,
        "reason": context.get("skill_index_source") or "configured_scope_index",
    }


def configured_skill_index(runtime_context: dict[str, object], settings: Settings) -> list[dict[str, Any]]:
    scope = load_skill_scope(runtime_context)
    if not scope:
        return []
    try:
        return skill_index_for_scope(scope, settings=settings)
    except Exception:
        return []


def load_skill_scope(runtime_context: dict[str, object]) -> dict[str, object]:
    tool_scopes = runtime_context.get("tool_scopes") if isinstance(runtime_context.get("tool_scopes"), dict) else {}
    tools = runtime_context.get("tools") if isinstance(runtime_context.get("tools"), list) else []
    candidates = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_id = str(tool.get("tool_id") or "")
        runner_name = str(tool.get("runner_name") or "")
        if tool_id == "runtime.load_skill" or tool_id.endswith(".load_skill") or runner_name == "load_skill":
            candidates.append(tool_id)
    candidates.extend(["runtime.load_skill", "load_skill"])
    for tool_id in candidates:
        scope = tool_scopes.get(tool_id)
        if isinstance(scope, dict):
            return dict(scope)
    return {}


def configured_planner_think(state: dict[str, Any], client: DeepSeekClient, settings: Settings) -> AgentDecision:
    context = state.get("context") or {}
    runtime_context = context.get("runtime_context") if isinstance(context, dict) else {}
    if not isinstance(runtime_context, dict):
        return AgentDecision(route="answer", answer="运行上下文不合法，无法执行。", reason="invalid_runtime_context")
    tool_guard = stop_replanning_after_unusable_tools(state)
    if tool_guard is not None:
        return tool_guard
    agent = runtime_context.get("agent") if isinstance(runtime_context.get("agent"), dict) else {}
    allowed_tools = {str(item.get("name")) for item in state.get("tool_catalog") or [] if item.get("name")}
    try:
        data = client.chat_json(
            build_configured_messages(state, runtime_context),
            model=agent.get("model") if isinstance(agent.get("model"), str) else settings.deepseek.chat_model,
        )
    except DeepSeekError:
        return AgentDecision(route="answer", answer="模型暂时无法完成本次请求。", reason="configured_llm_fallback")
    return parse_configured_decision(data, allowed_tools)


def configured_prompt_artifact(runtime_context: dict[str, object]) -> dict[str, object]:
    release = runtime_context.get("release") if isinstance(runtime_context.get("release"), dict) else {}
    agent = runtime_context.get("agent") if isinstance(runtime_context.get("agent"), dict) else {}
    system_prompt = str(agent.get("system_prompt") or "你是一个内部助手。").strip()
    agent_id = str(release.get("agent_id") or agent.get("id") or "")
    release_id = str(release.get("release_id") or "")
    try:
        release_version = int(release.get("version") or 0)
    except (TypeError, ValueError):
        release_version = 0

    cache = get_runtime_config_cache()
    if agent_id and release_id and release_version > 0:
        cached = cache.get_artifact(
            artifact_type="configured-prompt",
            artifact_version=CONFIGURED_PROMPT_ARTIFACT_VERSION,
            agent_id=agent_id,
            release_id=release_id,
            version=release_version,
        )
        if isinstance(cached, dict) and isinstance(cached.get("system_messages"), list):
            return cached

    artifact: dict[str, object] = {
        "system_messages": [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": CONFIGURED_PROTOCOL},
        ],
        "runtime_static": {
            "release": {
                "release_id": release.get("release_id"),
                "agent_id": release.get("agent_id"),
                "version": release.get("version"),
            },
            "agent": {
                "id": agent.get("id"),
                "name": agent.get("name"),
                "model": agent.get("model"),
            },
        },
    }
    if agent_id and release_id and release_version > 0:
        cache.set_artifact(
            artifact,
            artifact_type="configured-prompt",
            artifact_version=CONFIGURED_PROMPT_ARTIFACT_VERSION,
            agent_id=agent_id,
            release_id=release_id,
            version=release_version,
        )
    return artifact


def build_configured_messages(state: dict[str, Any], runtime_context: dict[str, object]) -> list[ChatMessage]:
    request = state["request"]
    agent = runtime_context.get("agent") if isinstance(runtime_context.get("agent"), dict) else {}
    artifact = configured_prompt_artifact(runtime_context)
    runtime_static = artifact.get("runtime_static") if isinstance(artifact.get("runtime_static"), dict) else {}
    payload = {
        "message": request.query,
        "history": (state.get("context") or {}).get("history") or [],
        "session_context": (state.get("context") or {}).get("session_context") or [],
        "session_user_context": (state.get("context") or {}).get("session_user_context") or [],
        "tool_observations": state.get("tool_observations") or [],
        "available_tools": state.get("tool_catalog") or [],
        "skill_index": (state.get("context") or {}).get("skill_index") or [],
        "runtime": {
            "release": runtime_static.get("release") or runtime_context.get("release") or {},
            "agent": runtime_static.get("agent") or {
                "id": agent.get("id"),
                "name": agent.get("name"),
                "model": agent.get("model"),
            },
            "channel": runtime_context.get("channel"),
            "actor": runtime_context.get("actor") or {},
        },
    }
    system_messages = artifact.get("system_messages") if isinstance(artifact.get("system_messages"), list) else []
    prefix = [
        ChatMessage(str(item.get("role") or "system"), str(item.get("content") or ""))
        for item in system_messages
        if isinstance(item, dict)
    ]
    if not prefix:
        prefix = [
            ChatMessage("system", str(agent.get("system_prompt") or "你是一个内部助手。").strip()),
            ChatMessage("system", CONFIGURED_PROTOCOL),
        ]
    return [*prefix, ChatMessage("user", json.dumps(payload, ensure_ascii=False))]


def parse_configured_decision(data: dict[str, Any], allowed_tools: set[str]) -> AgentDecision:
    route = str(data.get("route") or "answer")
    if route == "faq_answer":
        route = "answer"
    if route not in {"tool_call", "answer", "clarify", "handoff", "chat"}:
        route = "answer"
    tool_calls = parse_runtime_tool_calls(data.get("tool_calls"), allowed_tools)
    tool_name = data.get("tool_name")
    if route == "tool_call" and not tool_calls and isinstance(tool_name, str):
        if tool_name not in allowed_tools:
            return AgentDecision(
                route="answer",
                answer=f"工具 {tool_name} 未获授权，无法执行该操作。",
                reason=f"unknown_tool_requested:{tool_name}",
            )
        args = data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {}
        tool_calls = (ToolCall("tool-call-1", tool_name, args, data.get("reason")),)
    if route == "tool_call":
        if not tool_calls:
            return AgentDecision(route="answer", answer="未提供可执行工具调用。", reason="empty_tool_call")
        first = tool_calls[0]
        return AgentDecision(
            route="tool_call",
            tool_name=first.tool_name,
            tool_args=first.tool_args,
            tool_calls=tool_calls,
            reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        )
    answer = data.get("answer") if isinstance(data.get("answer"), str) else None
    if not answer or not answer.strip():
        answer = "我需要更多信息才能继续处理。"
        route = "clarify"
    return AgentDecision(
        route=route,  # type: ignore[arg-type]
        answer=answer.strip(),
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
    )


def parse_runtime_tool_calls(value: Any, allowed_tools: set[str]) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        return ()
    calls = []
    for index, item in enumerate(value[:8]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name") or item.get("name") or "")
        if name not in allowed_tools:
            return ()
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


def stop_replanning_after_unusable_tools(state: dict[str, Any]) -> AgentDecision | None:
    observations = state.get("tool_observations") if isinstance(state.get("tool_observations"), list) else []
    loop_count = int(state.get("loop_count") or 0)
    if loop_count < 2 or trailing_unusable_tool_count(observations) < 2:
        return None
    tool_names = sorted(
        {
            str(item.get("tool_name"))
            for item in observations[-4:]
            if isinstance(item, dict) and item.get("tool_name")
        }
    )
    joined = "、".join(tool_names) if tool_names else "外部工具"
    return AgentDecision(
        route="answer",
        answer=f"{joined} 连续没有返回可用结果，暂时无法继续完成该操作。请稍后重试或补充更精确的信息。",
        reason="tool_unusable_replanning_stopped",
    )


def trailing_unusable_tool_count(observations: list[Any]) -> int:
    count = 0
    for item in reversed(observations):
        if not isinstance(item, dict):
            break
        if tool_observation_has_usable_output(item):
            break
        count += 1
    return count


def tool_observation_has_usable_output(observation: dict[str, Any]) -> bool:
    if observation.get("status") != "ok":
        return False
    output = observation.get("output")
    return output_has_content(output)


def output_has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, list):
        return any(output_has_content(item) for item in value)
    if isinstance(value, dict):
        if "result" in value and output_has_content(value.get("result")):
            return True
        if "results" in value and output_has_content(value.get("results")):
            return True
        return any(output_has_content(item) for key, item in value.items() if key not in {"elapsed_ms", "mcp_tool"})
    return True


def decide_approval(
    approval_id: str,
    decision: ApprovalDecision,
    *,
    approval_store: ApprovalStore | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if decision.decision not in {APPROVAL_APPROVED, APPROVAL_REJECTED}:
        raise ApprovalError("审批决定只支持 approved 或 rejected")
    store = approval_store or default_approval_store
    try:
        record = store.decide(approval_id, decision)
    except ApprovalError as exc:
        if decision.decision != APPROVAL_APPROVED or str(exc) != "审批请求已处理":
            raise
        existing = store.get(approval_id)
        if existing and existing.get("status") == APPROVAL_APPROVED and not existing.get("result"):
            record = existing
        elif existing and existing.get("status") == APPROVAL_EXECUTED and isinstance(existing.get("result"), dict):
            cached = dict(existing["result"])
            cached["approval"] = approval_response(existing)
            return cached
        else:
            raise
    if decision.decision == APPROVAL_REJECTED:
        if approval_execution_engine(record) == "langgraph":
            return resume_rejected_langgraph_approval(record, store=store, settings=settings)
        return rejected_approval_response(record)
    return resume_approved_approval(record, store=store, settings=settings)


def rejected_approval_response(record: dict[str, Any]) -> dict[str, Any]:
    observation = approval_observation(record, "rejected")
    return {
        "run_id": record.get("run_id"),
        "session_id": record.get("session_id"),
        "agent_id": record.get("agent_id"),
        "release_id": record.get("release_id"),
        "actor_id": record.get("actor_id"),
        "channel": record.get("channel"),
        "answer": "审批已拒绝，本次工具调用不会执行。",
        "route": "answer",
        "status": "completed",
        "trace_id": record.get("trace_id"),
        "approval": approval_response(record),
        "tool_observations": [observation],
        "model_usage": {},
        "flow": [{"event": "approval_rejected", "approval_id": record.get("approval_id")}],
    }


def resume_approved_approval(
    record: dict[str, Any],
    *,
    store: ApprovalStore,
    settings: Settings | None = None,
) -> dict[str, Any]:
    snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    refreshed_context = runtime_context_resolver.resolve_runtime_context(
        str(record.get("agent_id")),
        str(record.get("release_id")),
        str(record.get("actor_id")),
        str(record.get("channel")),
    )
    call = snapshot.get("tool_call") if isinstance(snapshot.get("tool_call"), dict) else {}
    tool_id = str(call.get("tool_name") or record.get("tool_id") or "")
    ensure_tool_still_allowed(refreshed_context, tool_id)

    cfg = settings or load_settings()
    snapshot_metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    bypass_deps = build_configured_dependencies(
        refreshed_context,
        cfg,
        approval_store=None,
        run_metadata=snapshot_metadata,
    )
    state = build_resume_state(snapshot, refreshed_context)
    tool_batch = bypass_deps.run_tools([call], state) if bypass_deps.run_tools else {
        "execution_mode": "sequential",
        "results": [bypass_deps.run_tool(tool_id, call.get("tool_args") or {}, state)],
    }
    results = tool_batch.get("results") if isinstance(tool_batch.get("results"), list) else [tool_batch]
    approval_obs = approval_observation(record, "approved")
    if approval_execution_engine(record) == "langgraph":
        resumed = resume_langgraph_approval(
            record,
            refreshed_context=refreshed_context,
            store=store,
            settings=cfg,
            payload={
                "decision": "approved",
                "approval_observation": approval_obs,
                "tool_results": results,
            },
        )
    else:
        observations = list(snapshot.get("tool_observations") or []) + [approval_obs] + results
        resumed = run_configured_turn(
            refreshed_context,
            message=str(snapshot.get("message") or ""),
            session_id=str(snapshot.get("session_id") or record.get("session_id") or ""),
            user_id=str(snapshot.get("user_id") or "") or None,
            history=snapshot.get("history") if isinstance(snapshot.get("history"), list) else [],
            settings=cfg,
            metadata={
                **snapshot_metadata,
                "approval_id": record.get("approval_id"),
                "approval_decision": "approved",
            },
            approval_store=store,
            initial_tool_observations=observations,
            initial_loop_count=int(snapshot.get("loop_count") or 0),
            execution_engine="pocoflow",
        )
    store.mark_executed(str(record["approval_id"]), resumed)
    resumed["approval"] = approval_response({**record, "status": "executed"})
    return resumed


def approval_execution_engine(record: dict[str, Any]) -> str:
    snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    return str(metadata.get("execution_engine") or "pocoflow")


def resume_langgraph_approval(
    record: dict[str, Any],
    *,
    refreshed_context: dict[str, object],
    store: ApprovalStore,
    settings: Settings,
    payload: dict[str, Any],
) -> dict[str, Any]:
    snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    resume_metadata = {
        **metadata,
        "approval_id": record.get("approval_id"),
        "approval_decision": payload.get("decision"),
    }
    dependencies = build_configured_dependencies(
        refreshed_context,
        settings,
        approval_store=store,
        run_metadata=resume_metadata,
    )
    request = AgentRequest(
        query=str(snapshot.get("message") or ""),
        user_id=str(snapshot.get("user_id") or "") or None,
        session_id=str(snapshot.get("session_id") or record.get("session_id") or ""),
        trace_id=str(snapshot.get("trace_id") or record.get("trace_id") or "") or None,
        history=snapshot.get("history") if isinstance(snapshot.get("history"), list) else [],
    )
    engine = get_execution_engine(settings, "langgraph")
    resumed = engine.resume(
        thread_id=str(metadata.get("engine_thread_id") or request.trace_id),
        payload=payload,
        request=request,
        dependencies=dependencies,
        settings=settings,
        metadata=resume_metadata,
        runtime_context=refreshed_context,
    )
    resumed["runtime_context"] = summarize_runtime_context(refreshed_context)
    resumed["request_metadata"] = resume_metadata
    return resumed


def resume_rejected_langgraph_approval(
    record: dict[str, Any],
    *,
    store: ApprovalStore,
    settings: Settings | None = None,
) -> dict[str, Any]:
    cfg = settings or load_settings()
    try:
        refreshed_context = runtime_context_resolver.resolve_runtime_context(
            str(record.get("agent_id")),
            str(record.get("release_id")),
            str(record.get("actor_id")),
            str(record.get("channel")),
        )
    except Exception:
        return rejected_approval_response(record)
    resumed = resume_langgraph_approval(
        record,
        refreshed_context=refreshed_context,
        store=store,
        settings=cfg,
        payload={
            "decision": "rejected",
            "approval_observation": approval_observation(record, "rejected"),
            "answer": "审批已拒绝，本次工具调用不会执行。",
        },
    )
    resumed["approval"] = approval_response(record)
    return resumed


def build_resume_state(snapshot: dict[str, Any], refreshed_context: dict[str, object]) -> dict[str, Any]:
    request = type(
        "ResumeRequest",
        (),
        {
            "query": snapshot.get("message") or "",
            "session_id": snapshot.get("session_id"),
            "trace_id": snapshot.get("trace_id"),
            "history": snapshot.get("history") or [],
            "user_id": snapshot.get("user_id") or None,
        },
    )()
    return {
        "request": request,
        "context": {"runtime_context": refreshed_context, "history": snapshot.get("history") or []},
        "tool_observations": list(snapshot.get("tool_observations") or []),
        "decision": snapshot.get("decision") or {},
        "loop_count": int(snapshot.get("loop_count") or 0),
        "trace_id": snapshot.get("trace_id"),
    }


def ensure_tool_still_allowed(runtime_context: dict[str, object], tool_id: str) -> None:
    allowed = {str(item) for item in runtime_context.get("tool_ids") or []}
    if tool_id not in allowed:
        raise ApprovalError("审批通过后工具已不可用或无权限")


def approval_observation(record: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "call_id": (record.get("snapshot") or {}).get("tool_call", {}).get("call_id"),
        "tool_name": record.get("tool_id"),
        "args": record.get("tool_args") or {},
        "status": f"approval_{status}",
        "approval_id": record.get("approval_id"),
        "decided_by": record.get("decided_by"),
        "comment": record.get("decision_comment"),
        "tool_metadata": record.get("policy") or {},
    }


def safe_write_tool_log(settings: Settings, event: dict[str, Any]) -> None:
    try:
        write_tool_log(settings, event)
    except Exception:
        return


def record_mcp_warmup(runtime_context: dict[str, object]) -> None:
    results = warmup_runtime_mcp_clients(runtime_context)
    if not results:
        return
    policy = runtime_context.get("policy") if isinstance(runtime_context.get("policy"), dict) else {}
    runtime_context["policy"] = {**policy, "mcp_warmup": results}


def summarize_runtime_context(runtime_context: dict[str, object]) -> dict[str, object]:
    release = runtime_context.get("release") if isinstance(runtime_context.get("release"), dict) else {}
    agent = runtime_context.get("agent") if isinstance(runtime_context.get("agent"), dict) else {}
    return {
        "release": release,
        "agent": {
            "id": agent.get("id"),
            "name": agent.get("name"),
            "model": agent.get("model"),
        },
        "channel": runtime_context.get("channel"),
        "actor": runtime_context.get("actor") or {},
        "tool_ids": runtime_context.get("tool_ids") or [],
        "policy": runtime_context.get("policy") or {},
    }
