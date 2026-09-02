from __future__ import annotations

import time
from typing import Any, Callable

from aegora_runtime.tools import safe_args


StreamHandler = Callable[[dict[str, Any]], None]


NODE_LABELS = {
    "LoadContextNode": "加载会话上下文",
    "LoadSkillsNode": "检索经验库",
    "PreGuardNode": "检查问题边界",
    "ThinkNode": "模型思考",
    "ExecuteToolNode": "调用工具",
    "SelfCheckNode": "校验回答",
}


ROUTE_LABELS = {
    "faq_answer": "基于知识库回答",
    "tool_call": "调用工具",
    "clarify": "追问补充信息",
    "handoff": "转人工",
    "chat": "直接对话",
}


def emit_stream_event(store: Any, event: dict[str, Any]) -> None:
    handler = None
    try:
        handler = store.get("stream_handler")
    except Exception:
        handler = None
    if not handler:
        return
    try:
        request = store.get("request")
        payload = normalize_stream_event(
            event,
            trace_id=getattr(request, "trace_id", None) or store.get("trace_id"),
            session_id=getattr(request, "session_id", None),
            user_id=getattr(request, "user_id", None),
        )
        handler(payload)
    except Exception:
        # Streaming is observational; a failed client must not break the agent turn.
        return


def normalize_stream_event(
    event: dict[str, Any],
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    event_type = str(event.get("event") or "event")
    message = event_to_message(event)
    return {
        "type": event_type,
        "node": event.get("node"),
        "trace_id": trace_id,
        "session_id": session_id,
        "user_id": user_id,
        "message": message,
        "payload": event,
        "created_at": time.time(),
    }


def event_to_message(event: dict[str, Any]) -> str:
    event_type = event.get("event")
    node = event.get("node")
    node_label = NODE_LABELS.get(str(node), str(node or "流程"))

    if event_type == "node_start":
        return f"{node_label}..."
    if event_type == "node_end":
        elapsed = event.get("elapsed_ms")
        suffix = f"（{elapsed}ms）" if elapsed is not None else ""
        return f"{node_label}完成{suffix}"
    if event_type == "skill_retrieval":
        names = event.get("injected_skill_names") or []
        if names:
            return "已加载经验：" + "、".join(str(item) for item in names)
        if event.get("candidate_count"):
            return f"检索到 {event.get('candidate_count')} 条经验候选，本轮未自动注入。"
        return "未加载额外经验。"
    if event_type in {"intent_classified", "pre_guard"}:
        route = event.get("route_hint")
        reason = event.get("reason")
        return f"路由判断：{route or '-'}" + (f"，原因：{reason}" if reason else "")
    if event_type == "retrieval":
        if event.get("skipped"):
            return "本轮不需要 FAQ 检索。"
        ids = [str(item) for item in (event.get("faq_ids") or []) if item is not None]
        return f"FAQ 检索完成：命中 {event.get('count', 0)} 条" + (f"，Top ID：{', '.join(ids)}" if ids else "")
    if event_type == "agent_decision":
        route = event.get("route")
        if route == "tool_call":
            calls = event.get("tool_calls") or []
            names = [str(item.get("tool_name")) for item in calls if item.get("tool_name")]
            if not names and event.get("tool_name"):
                names = [str(event.get("tool_name"))]
            return "模型决定调用工具：" + ("、".join(names) if names else "-")
        return f"模型决策：{ROUTE_LABELS.get(str(route), str(route or '-'))}"
    if event_type == "tool_batch":
        return f"工具批次完成：{event.get('call_count', 0)} 个调用，模式 {event.get('execution_mode', 'sequential')}。"
    if event_type == "tool_call":
        return tool_call_message(event)
    if event_type == "self_check":
        return "回答校验通过。" if event.get("passed") else f"回答校验未通过：{event.get('reason') or '-'}"
    if event_type == "flow_end":
        return f"流程结束：{event.get('route') or '-'} / {event.get('status') or '-'}"
    if event_type == "final_answer":
        return "最终回答已生成。"
    if event_type == "error":
        return f"处理失败：{event.get('error_type') or '-'}"
    return node_label


def tool_call_message(event: dict[str, Any]) -> str:
    name = event.get("tool_name") or "-"
    status = event.get("status") or "-"
    args = safe_args(event.get("args") or {})
    output = event.get("output") or {}
    if name == "search_faq" and isinstance(output, dict):
        results = output.get("results") or []
        faq_ids = [str(item.get("faq_id")) for item in results[:5] if item.get("faq_id") is not None]
        return f"工具返回：search_faq，状态 {status}，命中 {len(results)} 条" + (
            f"，Top ID：{', '.join(faq_ids)}" if faq_ids else ""
        )
    if name == "load_skill" and isinstance(output, dict):
        skill = output.get("skill") or {}
        return f"工具返回：load_skill，状态 {status}，经验：{skill.get('name') or output.get('name') or '-'}"
    if name == "record_handoff":
        return f"工具返回：record_handoff，状态 {status}。"
    return f"工具返回：{name}，状态 {status}，参数 {args}。"
