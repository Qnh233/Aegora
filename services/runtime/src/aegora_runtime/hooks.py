from __future__ import annotations

import logging
from typing import Any

from pocoflow import Flow, Store

from aegora_runtime.logging import get_logger, log_event
from aegora_runtime.streaming import emit_stream_event


LOGGER = get_logger("hooks")


def attach_default_hooks(flow: Flow) -> Flow:
    flow.on("node_start", on_node_start)
    flow.on("node_end", on_node_end)
    flow.on("node_error", on_node_error)
    flow.on("flow_end", on_flow_end)
    return flow


def on_node_start(node_name: str, store: Store) -> None:
    _remember_node_usage(node_name, store)
    _append_observability(store, {"event": "node_start", "node": node_name})
    log_event(LOGGER, logging.INFO, "node_start", node=node_name, **_log_context(store))


def on_node_end(node_name: str, action: str, elapsed_s: float, store: Store) -> None:
    event = {
        "event": "node_end",
        "node": node_name,
        "action": action,
        "elapsed_ms": round(elapsed_s * 1000, 2),
    }
    usage = _node_usage_delta(node_name, store)
    if usage is not None:
        event["model_usage"] = usage
    _append_observability(store, event)
    log_event(LOGGER, logging.INFO, "node_end", **_log_context(store), **_without_event(event))


def on_node_error(node_name: str, exc: Exception, store: Store) -> None:
    event = {
        "event": "node_error",
        "node": node_name,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    _append_observability(store, event)
    log_event(LOGGER, logging.ERROR, "node_error", **_log_context(store), **_without_event(event))


def on_flow_end(total_steps: int, store: Store) -> None:
    event = {
        "event": "flow_end",
        "total_steps": total_steps,
        "status": store.get("status"),
        "route": store.get("route"),
    }
    usage = _flow_usage_delta(store)
    if usage is not None:
        event["model_usage"] = usage
    _append_observability(store, event)
    log_event(LOGGER, logging.INFO, "flow_end", **_log_context(store), **_without_event(event))


def _append_observability(store: Store, event: dict[str, Any]) -> None:
    events = list(store.get("observability") or [])
    events.append(event)
    store["observability"] = events
    emit_stream_event(store, event)


def _trace_id(store: Store) -> str | None:
    request = store.get("request")
    return (
        getattr(request, "trace_id", None)
        or getattr(request, "session_id", None)
        or getattr(request, "user_id", None)
    )


def _log_context(store: Store) -> dict[str, Any]:
    request = store.get("request")
    return {
        "trace_id": _trace_id(store),
        "session_id": getattr(request, "session_id", None),
        "user_id": getattr(request, "user_id", None),
        "query": getattr(request, "query", None),
    }


def _remember_node_usage(node_name: str, store: Store) -> None:
    usage = _usage_snapshot(store)
    if usage is None:
        return
    snapshots = dict(store.get("_node_usage_before") or {})
    snapshots[node_name] = usage
    store["_node_usage_before"] = snapshots


def _node_usage_delta(node_name: str, store: Store) -> dict[str, Any] | None:
    before = (store.get("_node_usage_before") or {}).get(node_name)
    after = _usage_snapshot(store)
    if before is None or after is None:
        return None
    return _usage_delta(before, after)


def _flow_usage_delta(store: Store) -> dict[str, Any] | None:
    before = store.get("_flow_usage_before")
    after = _usage_snapshot(store)
    if before is None or after is None:
        return None
    return _usage_delta(before, after)


def _usage_snapshot(store: Store) -> dict[str, Any] | None:
    deps = store.get("deps")
    provider = getattr(deps, "model_usage", None)
    if not provider:
        return None
    try:
        return provider()
    except Exception:
        return None


def _usage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    by_model = {}
    models = set((before.get("by_model") or {}).keys()) | set((after.get("by_model") or {}).keys())
    for model in sorted(models):
        old = (before.get("by_model") or {}).get(model, {})
        new = (after.get("by_model") or {}).get(model, {})
        delta = {
            key: int(new.get(key) or 0) - int(old.get(key) or 0)
            for key in ["calls", "failed_calls", "prompt_tokens", "completion_tokens", "total_tokens"]
        }
        by_model[model] = delta
    return {
        "total_calls": int(after.get("total_calls") or 0) - int(before.get("total_calls") or 0),
        "failed_calls": int(after.get("failed_calls") or 0) - int(before.get("failed_calls") or 0),
        "by_model": by_model,
    }


def _without_event(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event"}
