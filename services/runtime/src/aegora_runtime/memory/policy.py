from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegora_runtime.memory.models import MemoryItem


TENANT_REQUIRED = "tenant_required"
USER_CONTROLLED = "user_controlled"
AGENT_PRIVATE = "agent_private"
MEMORY_POLICY_MODES = {TENANT_REQUIRED, USER_CONTROLLED, AGENT_PRIVATE}


@dataclass(frozen=True)
class MemoryNamespacePolicy:
    namespace: str
    mode: str = USER_CONTROLLED
    allow_public_agents: bool = False

    def __post_init__(self) -> None:
        namespace = str(self.namespace or "").strip()
        if not namespace:
            raise ValueError("memory namespace is required")
        if self.mode not in MEMORY_POLICY_MODES:
            raise ValueError("unsupported memory policy mode")
        object.__setattr__(self, "namespace", namespace)


@dataclass(frozen=True)
class MemoryGovernance:
    tenant_id: str = "default"
    agent_id: str | None = None
    agent_visibility: str = "private"
    namespace_policies: dict[str, MemoryNamespacePolicy] = field(default_factory=dict)
    user_preferences: dict[str, bool] = field(default_factory=dict)

    def policy_for(self, namespace: str) -> MemoryNamespacePolicy:
        policy = self.namespace_policies.get(namespace)
        if policy is not None:
            return policy
        if namespace == "preferences":
            return MemoryNamespacePolicy(namespace="preferences", mode=USER_CONTROLLED)
        return MemoryNamespacePolicy(namespace=namespace, mode=AGENT_PRIVATE)

    def share_enabled(self, namespace: str) -> bool | None:
        return self.user_preferences.get(namespace)


def governance_from_runtime_context(
    runtime_context: dict[str, object] | None,
    *,
    agent_id: str | None = None,
) -> MemoryGovernance:
    context = runtime_context if isinstance(runtime_context, dict) else {}
    raw = context.get("memory_governance")
    raw = raw if isinstance(raw, dict) else {}
    release = context.get("release") if isinstance(context.get("release"), dict) else {}
    agent = context.get("agent") if isinstance(context.get("agent"), dict) else {}

    resolved_agent_id = (
        agent_id
        or str(agent.get("id") or "").strip()
        or str(release.get("agent_id") or "").strip()
        or None
    )
    visibility = str(
        release.get("visibility")
        or agent.get("visibility")
        or raw.get("agent_visibility")
        or "private"
    )
    policies: dict[str, MemoryNamespacePolicy] = {}
    for namespace, item in (raw.get("namespace_policies") or {}).items():
        if not isinstance(item, dict):
            continue
        try:
            policies[str(namespace)] = MemoryNamespacePolicy(
                namespace=str(namespace),
                mode=str(item.get("mode") or USER_CONTROLLED),
                allow_public_agents=bool(item.get("allow_public_agents", False)),
            )
        except ValueError:
            continue

    preferences = {
        str(namespace): bool(value)
        for namespace, value in (raw.get("user_preferences") or {}).items()
        if isinstance(namespace, str)
    }
    return MemoryGovernance(
        tenant_id=str(raw.get("tenant_id") or "default"),
        agent_id=resolved_agent_id,
        agent_visibility=visibility,
        namespace_policies=policies,
        user_preferences=preferences,
    )


def write_scope_for(
    governance: MemoryGovernance,
    namespace: str,
) -> str:
    policy = governance.policy_for(namespace)
    if policy.mode == TENANT_REQUIRED:
        return "tenant_user"
    if policy.mode == AGENT_PRIVATE:
        return "user_agent"
    return "user_global"


def can_recall(item: MemoryItem, governance: MemoryGovernance) -> bool:
    policy = governance.policy_for(item.namespace)

    if governance.agent_visibility == "public" and not policy.allow_public_agents:
        if item.source_agent_id != governance.agent_id:
            return False

    # Tenant-required namespaces must only trust tenant_user records. Existing
    # user/agent memories do not get promoted merely because policy changed.
    if policy.mode == TENANT_REQUIRED:
        return item.scope == "tenant_user"

    if item.scope == "tenant_user":
        return False

    if item.scope == "user_agent":
        if policy.mode != AGENT_PRIVATE:
            return False
        item_agent_id = str(item.metadata.get("agent_id") or "")
        return bool(governance.agent_id and item_agent_id == governance.agent_id)

    if item.scope != "user_global" or policy.mode == AGENT_PRIVATE:
        return False

    if item.source_agent_id and item.source_agent_id == governance.agent_id:
        return True

    explicit_share = governance.share_enabled(item.namespace)
    if explicit_share is not None:
        return explicit_share

    # Legacy user-global rows predate per-agent provenance. Keep prior behavior until
    # the user explicitly sets a sharing preference.
    return item.source_agent_id in {None, "", "legacy"}


def filter_recalled_items(
    items: list[MemoryItem],
    governance: MemoryGovernance,
) -> list[MemoryItem]:
    return [item for item in items if can_recall(item, governance)]


def governance_to_dict(governance: MemoryGovernance) -> dict[str, Any]:
    return {
        "tenant_id": governance.tenant_id,
        "agent_id": governance.agent_id,
        "agent_visibility": governance.agent_visibility,
        "namespace_policies": {
            namespace: {
                "mode": policy.mode,
                "allow_public_agents": policy.allow_public_agents,
            }
            for namespace, policy in governance.namespace_policies.items()
        },
        "user_preferences": dict(governance.user_preferences),
    }
