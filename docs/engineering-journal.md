# Aegora Engineering Journal

## 2026-09-04 — CI collection hardening for monorepo tests

- **Objective / roadmap item**: finish the pending Workflow Capability iteration by fixing its failing Python CI check instead of starting a conflicting roadmap branch.
- **Key design decision**: configure pytest once at the repository root with `--import-mode=importlib` so same-named test modules in `services/runtime/tests` and `apps/control-plane/backend/tests` can coexist in the monorepo without being imported as one global module name.
- **Tradeoff**: a repository-level pytest configuration changes collection semantics for all Python tests, but it is preferable to renaming mature test files or embedding CI-only flags that developers may forget locally.
- **Pitfall / root cause**: GitHub Actions ran `pytest -q` across both test trees. With pytest's default prepend import mode, duplicate basenames such as `test_config.py` and `test_runtime_context.py` collided in `sys.modules`, causing collection to stop before the actual Workflow tests ran. Reproducing from the repository root also exposed a second monorepo issue: the Control Plane `app` package and Runtime `aegora_runtime` / `scripts` packages were not on the root test import path.
- **Follow-up pitfall**: after fixing root test paths, CI exposed a namespace-package collision: repository root `apps/` and `services/runtime/apps/` both participated in resolving `from apps import data_ops_app`. Because the Runtime directory lacked `__init__.py`, Python could bind the wrong namespace. Marking Runtime `apps` as an explicit package makes the intended import deterministic.
- **Highlight / reusable pattern**: monorepos with multiple non-package test trees should make import semantics explicit. Keeping CI and local pytest behavior identical prevents a class of environment-only collection failures.
- **Important files**: `pytest.ini`, `services/runtime/apps/__init__.py`, `.github/workflows/ci.yml` (unchanged; it now automatically consumes the root config). The root pytest config explicitly includes both Python project roots plus Runtime `src`.
- **Verification**: root `python -m pytest -q` now gets past the previous duplicate-test-name and project-import-path failures; the remaining local collection errors are missing environment dependencies (`psycopg`, `pocoflow`) that CI installs from the repository requirements. `git diff --check` passes. Final verification is the GitHub Actions re-run after push.
- **Blockers**: the current Windows Python environment does not have the full repository requirements installed, so the local full suite cannot complete here. Email notification is unavailable in the current automation environment.
- **Next step**: push the CI fix to PR #1, confirm both checks pass, then continue the latest independent roadmap item only after the pending branch is stable.

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
