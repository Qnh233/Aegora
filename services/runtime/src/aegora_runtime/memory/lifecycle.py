from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect
from aegora_runtime.memory.models import (
    MemoryCandidate,
    MemoryCandidateDecision,
    MemoryScope,
    MemoryWrite,
)
from aegora_runtime.memory.policy import TENANT_REQUIRED, MemoryGovernance, write_scope_for
from aegora_runtime.memory.service import MemoryService


SENSITIVE_MEMORY_TERMS = {
    "password",
    "passwd",
    "api_key",
    "apikey",
    "token",
    "secret",
    "private_key",
    "otp",
    "cvv",
    "密码",
    "口令",
    "验证码",
    "私钥",
    "身份证",
    "银行卡",
    "信用卡",
    "病史",
    "疾病",
    "宗教",
    "政治",
    "性取向",
    "种族",
    "工会",
    "犯罪记录",
}


class MemoryCandidateStore:
    """Aegora-owned candidate queue. Providers only receive approved mutations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def enqueue(
        self,
        *,
        scope: MemoryScope,
        candidate: MemoryCandidate,
        proposed_scope: str,
    ) -> MemoryCandidate:
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_candidates (
                        tenant_id,
                        subject_user_id,
                        namespace,
                        proposed_scope,
                        agent_id,
                        operation,
                        memory_type,
                        memory_key,
                        content,
                        evidence_type,
                        update_intent,
                        source_agent_id,
                        source_session_id,
                        source_trace_id,
                        confidence,
                        importance,
                        ttl_days,
                        rationale,
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending'
                    )
                    ON CONFLICT (
                        tenant_id,
                        subject_user_id,
                        (COALESCE(source_trace_id, '')),
                        namespace,
                        memory_key,
                        operation
                    )
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        scope.tenant_id or "default",
                        scope.user_id,
                        candidate.namespace,
                        proposed_scope,
                        scope.agent_id if proposed_scope == "user_agent" else None,
                        candidate.operation,
                        candidate.memory_type,
                        candidate.key,
                        candidate.value,
                        candidate.evidence_type,
                        candidate.update_intent,
                        candidate.source_agent_id or scope.agent_id,
                        candidate.source_session_id or scope.session_id,
                        candidate.source_trace_id,
                        candidate.confidence,
                        candidate.importance,
                        candidate.ttl_days,
                        candidate.rationale,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        SELECT id
                        FROM memory_candidates
                        WHERE tenant_id = %s
                          AND subject_user_id = %s
                          AND COALESCE(source_trace_id, '') = COALESCE(%s, '')
                          AND namespace = %s
                          AND memory_key = %s
                          AND operation = %s
                        """,
                        (
                            scope.tenant_id or "default",
                            scope.user_id,
                            candidate.source_trace_id,
                            candidate.namespace,
                            candidate.key,
                            candidate.operation,
                        ),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise RuntimeError("memory candidate enqueue did not return an id")
                candidate_id = str(row["id"])
                _record_candidate_event(
                    cur,
                    scope=scope,
                    candidate=replace(candidate, candidate_id=candidate_id),
                    event_type="candidate_proposed",
                    reason="automatic_extraction",
                    candidate_id=int(candidate_id),
                    detail={"proposed_scope": proposed_scope},
                )
            conn.commit()
        return replace(candidate, candidate_id=candidate_id)

    def mark_decision(
        self,
        *,
        scope: MemoryScope,
        candidate: MemoryCandidate,
        decision: MemoryCandidateDecision,
    ) -> None:
        if not candidate.candidate_id:
            return
        try:
            candidate_id = int(candidate.candidate_id)
        except ValueError:
            return
        memory_id: int | None = None
        if decision.item is not None:
            try:
                memory_id = int(decision.item.memory_id)
            except ValueError:
                memory_id = None
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_candidates
                    SET status = %s,
                        decision_reason = %s,
                        applied_memory_id = %s,
                        decided_at = now()
                    WHERE id = %s
                    """,
                    (
                        decision.status,
                        decision.reason,
                        memory_id,
                        candidate_id,
                    ),
                )
                _record_candidate_event(
                    cur,
                    scope=scope,
                    candidate=candidate,
                    event_type=f"candidate_{decision.status}",
                    reason=decision.reason,
                    candidate_id=candidate_id,
                    memory_id=memory_id,
                    detail={},
                )
            conn.commit()

    def extraction_completed(
        self,
        *,
        trace_id: str,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        candidate_count: int,
        model: str,
    ) -> None:
        self._record_extraction(
            trace_id=trace_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            status="completed",
            candidate_count=candidate_count,
            model=model,
            error=None,
        )

    def extraction_failed(
        self,
        *,
        trace_id: str,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        model: str,
        error: str,
    ) -> None:
        self._record_extraction(
            trace_id=trace_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            status="failed",
            candidate_count=0,
            model=model,
            error=error[:2000],
        )

    def extraction_is_completed(self, trace_id: str) -> bool:
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status
                    FROM memory_extraction_messages
                    WHERE trace_id = %s
                    """,
                    (trace_id,),
                )
                row = cur.fetchone()
        return bool(row and row["status"] == "completed")

    def _record_extraction(
        self,
        *,
        trace_id: str,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        status: str,
        candidate_count: int,
        model: str,
        error: str | None,
    ) -> None:
        with connect(self.settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_extraction_messages (
                        trace_id,
                        user_id,
                        session_id,
                        agent_id,
                        status,
                        candidate_count,
                        attempt_count,
                        model,
                        error,
                        processed_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, now(), now())
                    ON CONFLICT (trace_id)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        candidate_count = EXCLUDED.candidate_count,
                        attempt_count = memory_extraction_messages.attempt_count + 1,
                        model = EXCLUDED.model,
                        error = EXCLUDED.error,
                        processed_at = now(),
                        updated_at = now()
                    """,
                    (
                        trace_id,
                        user_id,
                        session_id,
                        agent_id,
                        status,
                        candidate_count,
                        model,
                        error,
                    ),
                )
            conn.commit()


def resolve_memory_candidate(
    *,
    service: MemoryService,
    scope: MemoryScope,
    governance: MemoryGovernance,
    candidate: MemoryCandidate,
    auto_apply_threshold: float,
    forget_threshold: float,
    store: MemoryCandidateStore | None = None,
) -> MemoryCandidateDecision:
    policy = governance.policy_for(candidate.namespace)
    proposed_scope = write_scope_for(governance, candidate.namespace)
    if proposed_scope == "user_agent" and not governance.agent_id:
        decision = MemoryCandidateDecision(
            status="rejected",
            reason="agent_private_memory_requires_agent_id",
            candidate_id=candidate.candidate_id,
        )
        if store is not None:
            store.mark_decision(scope=scope, candidate=candidate, decision=decision)
        return decision

    effective_scope = MemoryScope(
        user_id=scope.user_id,
        session_id=scope.session_id,
        agent_id=governance.agent_id or scope.agent_id,
        tenant_id=governance.tenant_id,
        namespace=candidate.namespace,
        memory_scope=proposed_scope,
    )

    decision: MemoryCandidateDecision
    if policy.mode == TENANT_REQUIRED:
        decision = MemoryCandidateDecision(
            status="rejected",
            reason="tenant_managed_namespace",
            candidate_id=candidate.candidate_id,
        )
    elif candidate.evidence_type == "inferred":
        decision = MemoryCandidateDecision(
            status="rejected",
            reason="inferred_memory_not_auto_applied",
            candidate_id=candidate.candidate_id,
        )
    elif contains_sensitive_memory(candidate):
        decision = MemoryCandidateDecision(
            status="rejected",
            reason="sensitive_memory_blocked",
            candidate_id=candidate.candidate_id,
        )
    elif candidate.operation == "forget":
        decision = _resolve_forget_candidate(
            service=service,
            scope=effective_scope,
            candidate=candidate,
            forget_threshold=forget_threshold,
        )
    else:
        decision = _resolve_upsert_candidate(
            service=service,
            scope=effective_scope,
            candidate=candidate,
            auto_apply_threshold=auto_apply_threshold,
        )

    if store is not None:
        store.mark_decision(scope=effective_scope, candidate=candidate, decision=decision)
    return decision


def _resolve_upsert_candidate(
    *,
    service: MemoryService,
    scope: MemoryScope,
    candidate: MemoryCandidate,
    auto_apply_threshold: float,
) -> MemoryCandidateDecision:
    if candidate.confidence < auto_apply_threshold:
        return MemoryCandidateDecision(
            status="needs_review",
            reason="below_auto_apply_threshold",
            candidate_id=candidate.candidate_id,
        )

    current = service.get_current(scope=scope, key=candidate.key)
    if current is not None and current.content == candidate.value:
        return MemoryCandidateDecision(
            status="skipped",
            reason="duplicate_current_value",
            candidate_id=candidate.candidate_id,
            item=current,
        )
    if current is not None and current.source_kind == "tenant":
        return MemoryCandidateDecision(
            status="rejected",
            reason="cannot_override_tenant_memory",
            candidate_id=candidate.candidate_id,
            item=current,
        )

    current_confidence = _metadata_float(current, "confidence", 1.0) if current else 0.0
    if current is not None and current.source_kind == "explicit":
        if not candidate.update_intent:
            return MemoryCandidateDecision(
                status="needs_review",
                reason="conflict_with_explicit_memory",
                candidate_id=candidate.candidate_id,
                item=current,
            )
        if candidate.confidence < max(auto_apply_threshold, 0.95):
            return MemoryCandidateDecision(
                status="needs_review",
                reason="explicit_memory_update_requires_high_confidence",
                candidate_id=candidate.candidate_id,
                item=current,
            )
    if (
        current is not None
        and current.source_kind == "automatic"
        and candidate.confidence + 0.05 < current_confidence
        and not candidate.update_intent
    ):
        return MemoryCandidateDecision(
            status="needs_review",
            reason="lower_confidence_than_current_memory",
            candidate_id=candidate.candidate_id,
            item=current,
        )

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=candidate.ttl_days)
        if candidate.ttl_days is not None
        else None
    )
    result = service.remember(
        scope=scope,
        memory=MemoryWrite(
            key=candidate.key,
            value=str(candidate.value or ""),
            memory_type=candidate.memory_type,
            source_agent_id=candidate.source_agent_id or scope.agent_id,
            source_session_id=candidate.source_session_id or scope.session_id,
            source_trace_id=candidate.source_trace_id,
            source_kind="automatic",
            confidence=candidate.confidence,
            importance=candidate.importance,
            expires_at=expires_at,
            reason="automatic_candidate_applied",
        ),
    )
    if not result.saved or result.item is None:
        return MemoryCandidateDecision(
            status="needs_review",
            reason=result.reason or "provider_write_failed",
            candidate_id=candidate.candidate_id,
        )
    return MemoryCandidateDecision(
        status="applied",
        reason="automatic_candidate_applied",
        candidate_id=candidate.candidate_id,
        item=result.item,
    )


def _resolve_forget_candidate(
    *,
    service: MemoryService,
    scope: MemoryScope,
    candidate: MemoryCandidate,
    forget_threshold: float,
) -> MemoryCandidateDecision:
    if candidate.evidence_type != "explicit_instruction":
        return MemoryCandidateDecision(
            status="rejected",
            reason="forget_requires_explicit_instruction",
            candidate_id=candidate.candidate_id,
        )
    if candidate.confidence < forget_threshold:
        return MemoryCandidateDecision(
            status="needs_review",
            reason="forget_below_threshold",
            candidate_id=candidate.candidate_id,
        )

    current = service.get_current(scope=scope, key=candidate.key)
    if current is None:
        return MemoryCandidateDecision(
            status="skipped",
            reason="memory_already_absent",
            candidate_id=candidate.candidate_id,
        )
    if current.source_kind == "tenant":
        return MemoryCandidateDecision(
            status="rejected",
            reason="cannot_forget_tenant_memory",
            candidate_id=candidate.candidate_id,
            item=current,
        )
    changed = service.forget(
        scope=scope,
        key=candidate.key,
        reason="automatic_explicit_forget",
        source_trace_id=candidate.source_trace_id,
        source_kind="automatic",
    )
    return MemoryCandidateDecision(
        status="applied" if changed else "needs_review",
        reason="automatic_explicit_forget" if changed else "provider_forget_failed",
        candidate_id=candidate.candidate_id,
    )


def contains_sensitive_memory(candidate: MemoryCandidate) -> bool:
    haystack = " ".join(
        str(value or "").lower()
        for value in (candidate.namespace, candidate.key, candidate.value, candidate.rationale)
    )
    return any(term in haystack for term in SENSITIVE_MEMORY_TERMS)


def _metadata_float(item: Any, key: str, default: float) -> float:
    if item is None:
        return default
    try:
        return float(item.metadata.get(key, default))
    except (TypeError, ValueError):
        return default


def _record_candidate_event(
    cur: Any,
    *,
    scope: MemoryScope,
    candidate: MemoryCandidate,
    event_type: str,
    reason: str,
    candidate_id: int,
    detail: dict[str, Any],
    memory_id: int | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO memory_events (
            tenant_id,
            subject_user_id,
            namespace,
            memory_key,
            memory_id,
            candidate_id,
            event_type,
            source_kind,
            source_agent_id,
            source_session_id,
            source_trace_id,
            reason,
            detail
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'automatic', %s, %s, %s, %s, %s::jsonb)
        """,
        (
            scope.tenant_id or "default",
            scope.user_id,
            candidate.namespace,
            candidate.key,
            memory_id,
            candidate_id,
            event_type,
            candidate.source_agent_id or scope.agent_id,
            candidate.source_session_id or scope.session_id,
            candidate.source_trace_id,
            reason,
            json.dumps(detail, ensure_ascii=False, default=str),
        ),
    )
