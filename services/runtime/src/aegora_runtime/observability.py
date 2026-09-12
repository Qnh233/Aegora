from __future__ import annotations

import logging
import os
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from aegora_runtime.config import Settings
from aegora_runtime.logging import get_logger, log_event


LOGGER = get_logger("observability")
_LLM_METADATA: ContextVar[dict[str, Any]] = ContextVar("aegora_llm_metadata", default={})


def current_llm_metadata() -> dict[str, Any]:
    """Metadata forwarded to LiteLLM for cross-service trace correlation."""
    return dict(_LLM_METADATA.get())


def _credentials_present() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


@contextmanager
def agent_trace_scope(request: Any, settings: Settings) -> Iterator[Any | None]:
    """Create an optional Langfuse root span and expose its trace id to LiteLLM.

    Langfuse is deliberately non-critical: if tracing is disabled, credentials are
    absent, or SDK initialization fails, the Agent run continues unchanged.
    """
    stack = ExitStack()
    observation = None
    metadata: dict[str, Any] = {}

    if settings.observability.langfuse_tracing_enabled and _credentials_present():
        try:
            from langfuse import get_client, propagate_attributes

            langfuse = get_client()
            langfuse_trace_id = langfuse.create_trace_id(seed=str(request.trace_id))
            observation = stack.enter_context(
                langfuse.start_as_current_observation(
                    as_type="span",
                    name="aegora.agent.run",
                    trace_context={"trace_id": langfuse_trace_id},
                    input={"query": request.query},
                )
            )
            stack.enter_context(
                propagate_attributes(
                    trace_name="aegora-agent-run",
                    user_id=request.user_id,
                    session_id=request.session_id,
                    tags=["aegora", settings.app.env],
                    environment=settings.app.env,
                    metadata={
                        "aegora_trace_id": str(request.trace_id),
                        "product_id": str(request.product_id),
                        "instance_id": settings.observability.instance_id,
                    },
                )
            )
            metadata = {
                "trace_id": langfuse_trace_id,
                "session_id": request.session_id,
                "user_id": request.user_id,
                "tags": ["aegora", settings.app.env],
                "aegora_trace_id": str(request.trace_id),
                "product_id": str(request.product_id),
            }
        except Exception as exc:  # Observability must never break production traffic.
            stack.close()
            stack = ExitStack()
            log_event(
                LOGGER,
                logging.WARNING,
                "langfuse_init_failed",
                error_type=type(exc).__name__,
            )

    token = _LLM_METADATA.set(metadata)
    try:
        yield observation
    finally:
        _LLM_METADATA.reset(token)
        stack.close()


def finish_agent_trace(observation: Any | None, output: dict[str, Any]) -> None:
    if observation is None:
        return
    observation.update(
        output={
            "status": output.get("status"),
            "route": output.get("route"),
            "answer": output.get("answer"),
        },
        metadata={
            "intent": str((output.get("intent") or {}).get("category") or ""),
            "tool_call_count": str(len(output.get("tool_observations") or [])),
            "execution_engine": str(output.get("execution_engine") or ""),
            "engine_schema_version": str(output.get("engine_schema_version") or ""),
            "engine_thread_id": str(output.get("engine_thread_id") or ""),
            "checkpoint_id": str(output.get("checkpoint_id") or ""),
        },
    )
