# Aegora Architecture

## Bounded contexts

### Control Plane

Owns configuration and governance facts:

- Agent drafts and immutable releases
- users, roles, tool permissions and scopes
- MCP connections and tool manifests
- publish/disable lifecycle and audit records

It must not become the production inference hot path.

### Runtime

Owns execution behavior:

- gateway/channel normalization
- release and current-policy resolution
- planner/agent loop
- MCP/tool adapters
- approval interrupts and resume
- RAG, Skill loading and session execution context
- process-local connection pools and disposable caches

The runtime must not treat request-supplied tools as an authorization source.

### Contracts

`packages/contracts` is the only intended cross-plane coupling point. Contracts should describe release/runtime context, capability manifests and gateway request/response shapes. Implementations remain independently deployable.

## Authorization invariant

```text
effective capability
  = published release ceiling
  ∩ current capability status
  ∩ current actor authorization/scope
```

Permission expansion requires a new release when it changes an Agent's configured ceiling. Permission revocation and safety-policy tightening must be able to take effect at runtime without republishing the Agent.

## Capability model

- **MCP Tool**: atomic or business-semantic external capability.
- **Workflow Capability**: deterministic/controlled SOP exposed through a stable input/output contract.
- **Agent**: role + model + knowledge + governed set of capabilities.
- **Skill**: reusable operating experience, versioned and governed separately from factual knowledge.

The planner may see tools and workflow capabilities through a unified callable catalog, while the control plane keeps their product/governance semantics distinct.

