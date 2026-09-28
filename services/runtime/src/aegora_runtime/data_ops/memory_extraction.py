from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aegora_runtime import runtime_context as runtime_context_resolver
from aegora_runtime.config import Settings
from aegora_runtime.deepseek import DeepSeekClient
from aegora_runtime.memory import MemoryScope, build_memory_service, governance_from_runtime_context
from aegora_runtime.memory.extractor import JsonMemoryExtractionClient, extract_memory_candidates
from aegora_runtime.memory.lifecycle import MemoryCandidateStore, resolve_memory_candidate
from aegora_runtime.memory.models import MemoryCandidateDecision
from aegora_runtime.memory.policy import write_scope_for
from aegora_runtime.sessions import SESSION_USER_PREFIX
from aegora_runtime.strapi import StrapiClient, collection_endpoint


@dataclass(frozen=True)
class MemoryExtractionResult:
    metrics: dict[str, Any]
    items: list[dict[str, Any]]
    period_start: datetime
    period_end: datetime


def run_memory_extraction(
    settings: Settings,
    *,
    hours: int = 24,
    limit: int = 200,
    dry_run: bool = False,
    auto_apply: bool = True,
    force: bool = False,
    client: JsonMemoryExtractionClient | None = None,
) -> MemoryExtractionResult:
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=max(1, hours))
    service = build_memory_service(settings)
    expired_count = service.expire_due(
        tenant_id=None,
        limit=max(100, limit * 2),
    )

    if not settings.memory.extraction_enabled and not force:
        return MemoryExtractionResult(
            metrics={
                "enabled": False,
                "expired_memories": expired_count,
                "messages_scanned": 0,
                "messages_processed": 0,
                "candidates": 0,
                "applied": 0,
                "needs_review": 0,
                "rejected": 0,
                "skipped": 0,
            },
            items=[],
            period_start=period_start,
            period_end=period_end,
        )

    messages = load_user_messages(settings, period_start, limit=limit)
    store = MemoryCandidateStore(settings)
    extraction_client = client or DeepSeekClient(settings.deepseek)
    items: list[dict[str, Any]] = []
    metrics = {
        "enabled": True,
        "expired_memories": expired_count,
        "messages_scanned": len(messages),
        "messages_processed": 0,
        "messages_already_processed": 0,
        "messages_skipped_anonymous": 0,
        "messages_skipped_policy_unavailable": 0,
        "messages_failed": 0,
        "candidates": 0,
        "applied": 0,
        "needs_review": 0,
        "rejected": 0,
        "skipped": 0,
        "dry_run": dry_run,
        "auto_apply": auto_apply,
    }

    for row in messages:
        trace_id = str(row.get("trace_id") or "").strip()
        user_id = str(row.get("user_id") or "").strip()
        session_id = str(row.get("session_id") or "").strip() or None
        if not trace_id or not user_id:
            continue
        if user_id.startswith(SESSION_USER_PREFIX):
            metrics["messages_skipped_anonymous"] += 1
            continue
        if not dry_run and store.extraction_is_completed(trace_id):
            metrics["messages_already_processed"] += 1
            continue

        agent_id = message_agent_id(row)
        governance_snapshot = runtime_context_resolver.fetch_memory_governance(
            user_id,
            agent_visibility="private",
        )
        if not bool(governance_snapshot.get("policy_available")):
            metrics["messages_skipped_policy_unavailable"] += 1
            continue
        runtime_context = {
            "agent": {"id": agent_id, "visibility": "private"},
            "memory_governance": governance_snapshot,
        }
        governance = governance_from_runtime_context(runtime_context, agent_id=agent_id)

        try:
            candidates = extract_memory_candidates(
                extraction_client,
                user_message=str(row.get("content") or ""),
                source_agent_id=agent_id,
                source_session_id=session_id,
                source_trace_id=trace_id,
                model=settings.deepseek.chat_model,
            )
            metrics["messages_processed"] += 1
            metrics["candidates"] += len(candidates)

            for candidate in candidates:
                proposed_scope = write_scope_for(governance, candidate.namespace)
                scope = MemoryScope(
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    tenant_id=governance.tenant_id,
                    namespace=candidate.namespace,
                    memory_scope=proposed_scope,
                )
                queued = candidate
                if not dry_run:
                    queued = store.enqueue(
                        scope=scope,
                        candidate=candidate,
                        proposed_scope=proposed_scope,
                    )

                decision = _candidate_decision(
                    service=service,
                    scope=scope,
                    governance=governance,
                    candidate=queued,
                    settings=settings,
                    store=None if dry_run else store,
                    auto_apply=auto_apply,
                )
                metrics[decision.status] += 1
                items.append(
                    {
                        "trace_id": trace_id,
                        "user_id": user_id,
                        "agent_id": agent_id,
                        "candidate_id": queued.candidate_id,
                        "namespace": queued.namespace,
                        "key": queued.key,
                        "operation": queued.operation,
                        "status": decision.status,
                        "reason": decision.reason,
                    }
                )

            if not dry_run:
                store.extraction_completed(
                    trace_id=trace_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    candidate_count=len(candidates),
                    model=settings.deepseek.chat_model,
                )
        except Exception as exc:
            metrics["messages_failed"] += 1
            if not dry_run:
                store.extraction_failed(
                    trace_id=trace_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    model=settings.deepseek.chat_model,
                    error=f"{type(exc).__name__}: {exc}",
                )

    return MemoryExtractionResult(
        metrics=metrics,
        items=items,
        period_start=period_start,
        period_end=period_end,
    )


def _candidate_decision(
    *,
    service: Any,
    scope: MemoryScope,
    governance: Any,
    candidate: Any,
    settings: Settings,
    store: MemoryCandidateStore | None,
    auto_apply: bool,
) -> MemoryCandidateDecision:
    if not auto_apply:
        decision = MemoryCandidateDecision(
            status="needs_review",
            reason="auto_apply_disabled",
            candidate_id=candidate.candidate_id,
        )
        if store is not None:
            store.mark_decision(scope=scope, candidate=candidate, decision=decision)
        return decision
    return resolve_memory_candidate(
        service=service,
        scope=scope,
        governance=governance,
        candidate=candidate,
        auto_apply_threshold=settings.memory.auto_apply_threshold,
        forget_threshold=settings.memory.forget_threshold,
        store=store,
    )


def load_user_messages(
    settings: Settings,
    period_start: datetime,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    client = StrapiClient(settings.strapi)
    rows = client.list(
        collection_endpoint(settings, "chat_messages"),
        filters={"filters[createdAt][$gte]": period_start.isoformat()},
        sort="createdAt:asc",
        page_size=min(100, max(1, limit)),
        max_items=max(1, limit * 2),
    )
    return [
        row
        for row in rows
        if row.get("role") == "user"
        and row.get("content")
        and row.get("trace_id")
        and row.get("user_id")
    ][:limit]


def message_agent_id(row: dict[str, Any]) -> str | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    request_metadata = (
        metadata.get("request_metadata")
        if isinstance(metadata.get("request_metadata"), dict)
        else {}
    )
    value = metadata.get("agent_id") or request_metadata.get("agent_id")
    text = str(value or "").strip()
    return text or None
