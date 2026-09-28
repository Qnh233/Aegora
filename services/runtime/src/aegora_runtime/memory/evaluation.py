from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from aegora_runtime.memory.lifecycle import resolve_memory_candidate
from aegora_runtime.memory.models import MemoryCandidate, MemoryItem, MemoryScope, MemoryWrite
from aegora_runtime.memory.policy import MemoryGovernance, MemoryNamespacePolicy
from aegora_runtime.memory.service import MemoryService


@dataclass(frozen=True)
class MemoryEvalReport:
    metrics: dict[str, float | int]
    cases: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics, "cases": self.cases}


class InMemoryEvalProvider:
    """Small provider that mirrors Native PG identity/version behavior for offline eval."""

    def __init__(self, items: list[MemoryItem] | None = None) -> None:
        self.items: list[MemoryItem] = list(items or [])
        self._next_id = len(self.items) + 1

    def recall(self, *, scope: MemoryScope, query: str, limit: int) -> list[MemoryItem]:
        del query
        visible = [
            item
            for item in self.items
            if _same_user(item, scope)
            and item.metadata.get("status", "active") == "active"
            and (
                item.scope in {"user_global", "tenant_user"}
                or (
                    item.scope == "user_agent"
                    and str(item.metadata.get("agent_id") or "") == str(scope.agent_id or "")
                )
            )
        ]
        visible.sort(
            key=lambda item: (
                float(item.metadata.get("importance", 0.5)),
                item.version,
            ),
            reverse=True,
        )
        return visible[:limit]

    def get_current(self, *, scope: MemoryScope, key: str) -> MemoryItem | None:
        matches = [
            item
            for item in self.items
            if item.key == key
            and item.namespace == scope.namespace
            and item.scope == scope.memory_scope
            and _same_user(item, scope)
            and item.metadata.get("status", "active") == "active"
            and (
                item.scope != "user_agent"
                or str(item.metadata.get("agent_id") or "") == str(scope.agent_id or "")
            )
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    def remember(self, *, scope: MemoryScope, memory: MemoryWrite) -> MemoryItem:
        current = self.get_current(scope=scope, key=memory.key)
        if current is not None and current.content == memory.value:
            return current
        if current is not None:
            current.metadata["status"] = "superseded"
        item = MemoryItem(
            memory_id=str(self._next_id),
            memory_type=memory.memory_type,
            key=memory.key,
            content=memory.value,
            scope=scope.memory_scope,
            namespace=scope.namespace,
            version=(current.version + 1 if current else 1),
            source_agent_id=memory.source_agent_id,
            source_kind=memory.source_kind,
            metadata={
                "tenant_id": scope.tenant_id or "default",
                "user_id": scope.user_id,
                "agent_id": scope.agent_id if scope.memory_scope == "user_agent" else None,
                "source_session_id": memory.source_session_id,
                "source_trace_id": memory.source_trace_id,
                "confidence": memory.confidence,
                "importance": memory.importance,
                "status": "active",
            },
        )
        self._next_id += 1
        self.items.append(item)
        return item

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
        reason: str | None = None,
        source_trace_id: str | None = None,
        source_kind: str = "explicit",
    ) -> bool:
        del reason, source_trace_id, source_kind
        current = self.get_current(scope=scope, key=key)
        if current is None:
            return False
        current.metadata["status"] = "deleted"
        return True

    def expire_due(self, *, tenant_id: str | None = None, limit: int = 500) -> int:
        del tenant_id, limit
        return 0


def run_memory_eval(cases: list[dict[str, Any]]) -> MemoryEvalReport:
    reports = [run_memory_eval_case(raw) for raw in cases]
    return _score_reports(cases, reports)


def run_memory_provider_eval(
    cases: list[dict[str, Any]],
    *,
    provider_factory: Callable[[], Any],
    run_id: str,
) -> MemoryEvalReport:
    """Run the same governance/lifecycle corpus against a real provider contract."""
    reports = [
        run_memory_provider_eval_case(
            raw,
            provider=provider_factory(),
            run_id=run_id,
        )
        for raw in cases
    ]
    return _score_reports(cases, reports)


def _score_reports(
    cases: list[dict[str, Any]],
    reports: list[dict[str, Any]],
) -> MemoryEvalReport:
    counts = {
        "expected_recall": 0,
        "recall_hits": 0,
        "distractors": 0,
        "distractors_blocked": 0,
        "privacy_forbidden": 0,
        "privacy_blocked": 0,
        "expected_updates": 0,
        "correct_updates": 0,
        "expected_forgets": 0,
        "correct_forgets": 0,
        "candidate_expectations": 0,
        "candidate_matches": 0,
        "context_budget_cases": 0,
        "context_budget_passes": 0,
        "context_chars": 0,
    }

    for raw, report in zip(cases, reports, strict=True):
        recalled_keys = set(report["recalled_keys"])

        expected_keys = set(raw.get("expected_keys") or [])
        counts["expected_recall"] += len(expected_keys)
        counts["recall_hits"] += len(expected_keys & recalled_keys)

        distractor_keys = set(raw.get("distractor_keys") or [])
        counts["distractors"] += len(distractor_keys)
        counts["distractors_blocked"] += len(distractor_keys - recalled_keys)

        privacy_keys = set(raw.get("privacy_forbidden_keys") or [])
        counts["privacy_forbidden"] += len(privacy_keys)
        counts["privacy_blocked"] += len(privacy_keys - recalled_keys)

        expected_values = raw.get("expected_values") or {}
        counts["expected_updates"] += len(expected_values)
        counts["correct_updates"] += sum(
            1
            for key, value in expected_values.items()
            if report["current_values"].get(key) == value
        )

        expected_absent = set(raw.get("expected_absent_keys") or [])
        counts["expected_forgets"] += len(expected_absent)
        counts["correct_forgets"] += sum(
            1 for key in expected_absent if key not in report["current_values"]
        )

        expected_status = raw.get("expected_candidate_status")
        if expected_status:
            counts["candidate_expectations"] += 1
            counts["candidate_matches"] += int(report.get("candidate_status") == expected_status)

        context_chars = int(report["context_chars"])
        counts["context_chars"] += context_chars
        max_context_chars = raw.get("max_context_chars")
        if max_context_chars is not None:
            counts["context_budget_cases"] += 1
            counts["context_budget_passes"] += int(context_chars <= int(max_context_chars))

    metrics: dict[str, float | int] = {
        "cases": len(cases),
        "recall_relevance": _ratio(counts["recall_hits"], counts["expected_recall"]),
        "distractor_rejection_rate": _ratio(
            counts["distractors_blocked"], counts["distractors"]
        ),
        "privacy_isolation_rate": _ratio(
            counts["privacy_blocked"], counts["privacy_forbidden"]
        ),
        "update_correctness": _ratio(
            counts["correct_updates"], counts["expected_updates"]
        ),
        "forget_correctness": _ratio(
            counts["correct_forgets"], counts["expected_forgets"]
        ),
        "candidate_decision_accuracy": _ratio(
            counts["candidate_matches"], counts["candidate_expectations"]
        ),
        "context_budget_pass_rate": _ratio(
            counts["context_budget_passes"], counts["context_budget_cases"]
        ),
        "avg_context_chars": round(
            counts["context_chars"] / max(1, len(cases)),
            2,
        ),
    }
    return MemoryEvalReport(metrics=metrics, cases=reports)


def run_memory_provider_eval_case(
    raw: dict[str, Any],
    *,
    provider: Any,
    run_id: str,
) -> dict[str, Any]:
    governance = _governance(raw.get("governance") or {})
    case_id = str(raw.get("id") or "case")
    user_id = f"aegora-eval-{run_id}-{case_id}"[:180]
    scope = MemoryScope(
        user_id=user_id,
        session_id=f"eval-{run_id}",
        agent_id=governance.agent_id,
        tenant_id=governance.tenant_id,
        namespace="preferences",
    )
    service = MemoryService(
        provider,
        recall_enabled=True,
        recall_limit=int(raw.get("recall_limit") or 4),
        max_item_chars=int(raw.get("max_item_chars") or 400),
    )

    identities: list[MemoryScope] = []
    keys: set[str] = set()
    for seed in raw.get("memories") or []:
        if not isinstance(seed, dict):
            continue
        seed_scope = MemoryScope(
            user_id=user_id,
            session_id=scope.session_id,
            agent_id=(
                str(seed.get("agent_id") or governance.agent_id or "") or None
            ),
            tenant_id=str(seed.get("tenant_id") or governance.tenant_id),
            namespace=str(seed.get("namespace") or "preferences"),
            memory_scope=str(seed.get("scope") or "user_global"),
        )
        provider.remember(
            scope=seed_scope,
            memory=MemoryWrite(
                key=str(seed.get("key") or ""),
                value=str(seed.get("content") or ""),
                memory_type=str(seed.get("memory_type") or "semantic"),
                source_agent_id=seed.get("source_agent_id"),
                source_kind=str(seed.get("source_kind") or "automatic"),
                confidence=float(seed.get("confidence", 1.0)),
                importance=float(seed.get("importance", 0.5)),
                reason="memory_eval_seed",
            ),
        )
        identities.append(seed_scope)
        keys.add(str(seed.get("key") or ""))

    candidate_status = None
    candidate_reason = None
    if isinstance(raw.get("candidate"), dict):
        candidate = MemoryCandidate(**raw["candidate"])
        keys.add(candidate.key)
        candidate_scope = MemoryScope(
            user_id=user_id,
            session_id=scope.session_id,
            agent_id=governance.agent_id,
            tenant_id=governance.tenant_id,
            namespace=candidate.namespace,
        )
        decision = resolve_memory_candidate(
            service=service,
            scope=candidate_scope,
            governance=governance,
            candidate=candidate,
            auto_apply_threshold=float(raw.get("auto_apply_threshold") or 0.9),
            forget_threshold=float(raw.get("forget_threshold") or 0.98),
        )
        candidate_status = decision.status
        candidate_reason = decision.reason

    recalled = service.recall(
        scope=scope,
        query=str(raw.get("query") or ""),
        governance=governance,
    )
    current_values = _provider_current_values(
        service=service,
        keys=keys,
        identities=identities,
        default_scope=scope,
    )
    return {
        "id": case_id,
        "recalled_keys": [item.key for item in recalled],
        "context_chars": sum(len(item.content) for item in recalled),
        "current_values": current_values,
        "candidate_status": candidate_status,
        "candidate_reason": candidate_reason,
    }


def _provider_current_values(
    *,
    service: MemoryService,
    keys: set[str],
    identities: list[MemoryScope],
    default_scope: MemoryScope,
) -> dict[str, str]:
    scopes = list(identities)
    for memory_scope in ("user_global", "tenant_user"):
        scopes.append(
            MemoryScope(
                user_id=default_scope.user_id,
                session_id=default_scope.session_id,
                agent_id=default_scope.agent_id,
                tenant_id=default_scope.tenant_id,
                namespace="preferences",
                memory_scope=memory_scope,
            )
        )
    values: dict[str, str] = {}
    seen: set[tuple[str, str, str | None, str]] = set()
    for current_scope in scopes:
        identity = (
            current_scope.namespace,
            current_scope.memory_scope,
            current_scope.agent_id,
            current_scope.user_id,
        )
        if identity in seen:
            continue
        seen.add(identity)
        for key in keys:
            item = service.get_current(scope=current_scope, key=key)
            if item is not None:
                values[key] = item.content
    return values


def run_memory_eval_case(raw: dict[str, Any]) -> dict[str, Any]:
    governance = _governance(raw.get("governance") or {})
    scope = MemoryScope(
        user_id=str(raw.get("user_id") or "eval-user"),
        session_id=str(raw.get("session_id") or "eval-session"),
        agent_id=governance.agent_id,
        tenant_id=governance.tenant_id,
        namespace="preferences",
    )
    provider = InMemoryEvalProvider(
        [_item_from_dict(item, user_id=scope.user_id) for item in raw.get("memories") or []]
    )
    service = MemoryService(
        provider,
        recall_enabled=True,
        recall_limit=int(raw.get("recall_limit") or 4),
        max_item_chars=int(raw.get("max_item_chars") or 400),
    )

    candidate_status = None
    candidate_reason = None
    if isinstance(raw.get("candidate"), dict):
        candidate = MemoryCandidate(**raw["candidate"])
        candidate_scope = MemoryScope(
            user_id=scope.user_id,
            session_id=scope.session_id,
            agent_id=governance.agent_id,
            tenant_id=governance.tenant_id,
            namespace=candidate.namespace,
        )
        decision = resolve_memory_candidate(
            service=service,
            scope=candidate_scope,
            governance=governance,
            candidate=candidate,
            auto_apply_threshold=float(raw.get("auto_apply_threshold") or 0.9),
            forget_threshold=float(raw.get("forget_threshold") or 0.98),
        )
        candidate_status = decision.status
        candidate_reason = decision.reason

    recalled = service.recall(
        scope=scope,
        query=str(raw.get("query") or ""),
        governance=governance,
    )
    current_values = {
        item.key: item.content
        for item in provider.items
        if item.metadata.get("status", "active") == "active"
        and _same_user(item, scope)
    }
    return {
        "id": str(raw.get("id") or ""),
        "recalled_keys": [item.key for item in recalled],
        "context_chars": sum(len(item.content) for item in recalled),
        "current_values": current_values,
        "candidate_status": candidate_status,
        "candidate_reason": candidate_reason,
    }


def load_memory_eval_cases(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _governance(raw: dict[str, Any]) -> MemoryGovernance:
    policies = {
        str(namespace): MemoryNamespacePolicy(
            namespace=str(namespace),
            mode=str(item.get("mode") or "user_controlled"),
            allow_public_agents=bool(item.get("allow_public_agents", False)),
        )
        for namespace, item in (raw.get("namespace_policies") or {}).items()
        if isinstance(item, dict)
    }
    return MemoryGovernance(
        tenant_id=str(raw.get("tenant_id") or "default"),
        agent_id=str(raw.get("agent_id") or "agent-a"),
        agent_visibility=str(raw.get("agent_visibility") or "private"),
        namespace_policies=policies,
        user_preferences={
            str(key): bool(value)
            for key, value in (raw.get("user_preferences") or {}).items()
        },
    )


def _item_from_dict(raw: dict[str, Any], *, user_id: str) -> MemoryItem:
    return MemoryItem(
        memory_id=str(raw.get("memory_id") or raw.get("id") or raw.get("key") or "memory"),
        memory_type=str(raw.get("memory_type") or "semantic"),
        key=str(raw.get("key") or ""),
        content=str(raw.get("content") or ""),
        scope=str(raw.get("scope") or "user_global"),
        namespace=str(raw.get("namespace") or "preferences"),
        version=int(raw.get("version") or 1),
        source_agent_id=raw.get("source_agent_id"),
        source_kind=str(raw.get("source_kind") or "automatic"),
        metadata={
            "tenant_id": str(raw.get("tenant_id") or "default"),
            "user_id": user_id,
            "agent_id": raw.get("agent_id"),
            "confidence": float(raw.get("confidence", 1.0)),
            "importance": float(raw.get("importance", 0.5)),
            "status": str(raw.get("status") or "active"),
        },
    )


def _same_user(item: MemoryItem, scope: MemoryScope) -> bool:
    return (
        str(item.metadata.get("tenant_id") or "default") == str(scope.tenant_id or "default")
        and str(item.metadata.get("user_id") or "") == scope.user_id
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 4)
