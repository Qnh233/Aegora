# Aegora Engineering Journal

## 2026-09-04 — Workflow Capability phase 1

- **Objective / roadmap item**: land the first slice of Workflow Capability without coupling workflow engines into Runtime Core.
- **Key design decision**: represent a workflow as a governed capability with `source=workflow`, but bind execution to an existing MCP connection and remote tool name. This keeps Release/RBAC/policy enforcement unchanged and reuses the proven MCP session lifecycle instead of inventing a second workflow transport.
- **Tradeoff**: phase 1 intentionally does not model workflow DAGs, orchestration state, retries or version lifecycle inside Aegora. Those remain the responsibility of the workflow provider until a dedicated lifecycle model is justified.
- **Pitfall / root cause**: the shared contract already allowed `source=workflow`, but Control Plane validation and the DB compatibility migration silently rejected/rewrote that source. The contract and implementation had drifted.
- **Highlight / reusable pattern**: keep semantic capability type separate from transport. `workflow` describes governance/UX semantics; MCP remains the execution protocol. This preserves Runtime Core boundaries and lets future workflow engines plug in without provider-specific code.
- **Important files**: `apps/control-plane/backend/app/models.py`, `apps/control-plane/backend/app/api.py`, `apps/control-plane/backend/app/db.py`, `apps/control-plane/frontend/src/App.tsx`, `services/runtime/tests/test_tool_adapters.py`.
- **Verification**: `test_workflow_capability.py` 1/1 passed; `test_tool_adapters.py` 6 passed / 4 dependency-gated skipped; Python `compileall` passed; frontend Vite production build passed; `git diff --check` passed.
- **Pitfall / environment**: the first frontend build failed because `node_modules` was absent, so `vite` was unavailable. `npm ci` restored the lockfile-defined environment and the build then passed. `npm ci` also reported 2 pre-existing high-severity audit findings; they were not auto-fixed in this feature branch to avoid an unrelated dependency upgrade.
- **Blockers**: none for phase 1. Email notification is unavailable in the current automation environment.
- **Next step**: add workflow version/lifecycle management and richer Scope Schema editing, then proceed to LiteLLM/Langfuse production hardening or Redis L2 according to the latest roadmap priority.
