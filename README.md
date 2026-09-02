# Aegora

**Enterprise Agent Control Plane & Stateless Runtime**

Aegora is an enterprise-oriented platform for registering, governing, composing and operating AI agents, MCP tools, workflows and organizational skills. The repository is organized as a monorepo while keeping the control plane and data plane independently deployable.

## Architecture

```text
                         Aegora Control Plane
                 Agent / Release / RBAC / MCP Registry
                                |
                         PostgreSQL truth
                                |
                    Runtime Context Resolver
                                |
                 Aegora Stateless Runtime Fleet
                 /            |             \
              MCP          Workflow       LLM Gateway
                                           (LiteLLM)
```

### Design principles

- **Control plane / runtime separation**: configuration and governance live in the control plane; production execution lives in the runtime.
- **Stateless business execution**: runtime instances may keep disposable process-local caches and MCP sessions, but persistent business/configuration truth remains external.
- **Release-driven execution**: immutable Agent Releases define the capability ceiling; runtime authorization can only reduce that ceiling.
- **Dynamic authorization**: every run intersects release capabilities with current actor permissions, tool status and MCP connection state.
- **Capability-oriented integration**: MCP is the primary tool integration protocol; complex SOP/workflows are exposed as governed capabilities rather than leaking low-level APIs to the model.
- **Centralized configuration**: both applications read the repository-root `.env` by default; real secrets must never be committed.

## Repository layout

```text
Aegora/
├─ apps/
│  ├─ control-plane/
│  │  ├─ backend/          # FastAPI governance API
│  │  ├─ frontend/         # React/Vite console
│  │  └─ deploy/           # control-plane deployment assets
├─ services/
│  └─ runtime/
│     ├─ apps/             # API / worker entrypoints
│     ├─ src/aegora_runtime/ # stateless Agent Runtime core
│     ├─ config/           # non-secret runtime defaults
│     ├─ scripts/          # migrations, eval and operations
│     ├─ tests/            # runtime regression tests
│     └─ evals/            # evaluation datasets/results
├─ packages/
│  ├─ config/              # shared configuration conventions
│  └─ contracts/           # versioned cross-plane contracts
├─ deploy/                 # repository-level deployment boundary
├─ docs/
│  ├─ architecture.md
│  └─ migration.md
├─ .env.example            # canonical configuration template
└─ README.md
```

## Local development

1. Copy the root configuration:

```bash
cp .env.example .env
```

2. Start the control-plane backend:

```bash
cd apps/control-plane/backend
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. Start the control-plane frontend:

```bash
cd apps/control-plane/frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

4. Start the runtime:

```bash
cd services/runtime
python -m pip install -r requirements.txt
PYTHONPATH=src uvicorn apps.api_app:app --host 127.0.0.1 --port 5000
```

## Migration status

This workspace is the consolidated successor of the existing Agent Platform and Agentic RAG Runtime repositories. The first migration keeps their proven module/package boundaries intact to reduce regression risk. New cross-plane capabilities should depend on versioned contracts under `packages/contracts` instead of importing implementation code across applications.

Planned next steps:

1. Add a Workflow Capability adapter and workflow registration UX.
2. Introduce LiteLLM Proxy as the central LLM gateway and Langfuse tracing.
3. Add L1/L2 configuration caching with versioned keys and event-driven invalidation only after measuring resolver/DB bottlenecks.
4. Generalize the existing reflection/Skill-draft path into per-Agent learning policies and a governed data flywheel.

