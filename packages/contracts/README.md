# Aegora Contracts

This directory contains versioned contracts shared by the Control Plane and Runtime.

Rules:

1. Control Plane and Runtime must not import each other's implementation modules.
2. Cross-plane communication depends on explicit, versioned schemas/contracts.
3. Security-sensitive fields distinguish immutable release ceilings from runtime-refreshed governance facts.
4. Backward-compatible changes are preferred; breaking changes require a contract version bump.

Initial contract families to extract from the migrated code:

- `agent-release`
- `runtime-context`
- `capability-manifest`
- `gateway-run`
- `approval`

