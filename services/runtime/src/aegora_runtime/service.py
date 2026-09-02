from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aegora_runtime.agent_loop import AgentDependencies, AgentRequest, run_agent
from aegora_runtime.config import Settings
from aegora_runtime.sessions import (
    derive_user_id_from_session,
    load_recent_conversation,
    normalize_session_id,
    save_chat_turn,
    session_execution_lock,
)
from aegora_runtime.streaming import StreamHandler, normalize_stream_event


@dataclass(frozen=True)
class ChatTurnInput:
    message: str
    session_id: str
    product_id: str = "aicoin"
    domain_hint: str | None = None
    source: str = "api"
    metadata: dict[str, Any] = field(default_factory=dict)
    stream_handler: StreamHandler | None = None


def handle_chat_turn(
    request: ChatTurnInput,
    dependencies: AgentDependencies,
    settings: Settings,
) -> dict[str, Any]:
    """Run one session-scoped Agent turn and persist the result."""
    message = request.message.strip()
    if not message:
        raise ValueError("message is required")

    session_id = normalize_session_id(request.session_id)
    user_id = derive_user_id_from_session(session_id)
    product_id = request.product_id or settings.app.product_id
    started = time.perf_counter()

    with session_execution_lock(settings, session_id):
        history, _ = load_recent_conversation(settings, session_id, limit=20)
        result = run_agent(
            AgentRequest(
                query=message,
                product_id=product_id,
                domain_hint=request.domain_hint,
                session_id=session_id,
                history=history,
                stream_handler=request.stream_handler,
            ),
            dependencies,
            settings,
        )
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        result["total_latency_ms"] = total_ms
        answer = result.get("answer") or "未生成回答"
        assistant_message_id = save_chat_turn(
            settings,
            session_id=session_id,
            user_id=user_id,
            user_message=message,
            assistant_message=answer,
            result=result,
            source=request.source,
            request_metadata=request.metadata,
        )
        emit_final_stream_event(request.stream_handler, result, session_id, user_id, assistant_message_id, answer)

    return {
        "session_id": session_id,
        "user_id": user_id,
        "assistant_message_id": assistant_message_id,
        "answer": answer,
        "route": result.get("route"),
        "status": result.get("status"),
        "trace_id": result.get("trace_id"),
        "latency_ms": result.get("total_latency_ms"),
        "loop_mode": result.get("loop_mode"),
        "loop_count": result.get("loop_count"),
        "intent": result.get("intent") or {},
        "decision": result.get("decision") or {},
        "evidence": result.get("retrieved_faqs") or [],
        "assets": result.get("answer_assets") or [],
        "skills": result.get("skills") or [],
        "tool_observations": result.get("tool_observations") or [],
        "flow": result.get("observability") or [],
        "model_usage": result.get("model_usage") or {},
        "model_thinking_enabled": result.get("model_thinking_enabled"),
    }


def emit_final_stream_event(
    handler: StreamHandler | None,
    result: dict[str, Any],
    session_id: str,
    user_id: str,
    assistant_message_id: str,
    answer: str,
) -> None:
    if not handler:
        return
    event = normalize_stream_event(
        {
            "event": "final_answer",
            "route": result.get("route"),
            "status": result.get("status"),
            "assistant_message_id": assistant_message_id,
            "answer_preview": answer[:200],
            "latency_ms": result.get("total_latency_ms"),
        },
        trace_id=result.get("trace_id"),
        session_id=session_id,
        user_id=user_id,
    )
    try:
        handler(event)
    except Exception:
        return
