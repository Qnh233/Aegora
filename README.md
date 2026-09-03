<p align="center">
  <img src="./docs/assets/aegora-logo.svg" alt="Aegora logo" width="260" />
</p>

<h1 align="center">Aegora</h1>

[中文文档](./README_CN.md)

**Enterprise Agent Control Plane & Stateless Runtime**

Aegora is an enterprise-oriented platform for registering, governing, composing and operating AI agents, MCP tools, workflows and organizational skills. The repository is organized as a monorepo while keeping the control plane and data plane independently deployable.

## Architecture

The diagram below describes the **target production topology**. PostgreSQL remains the source of truth; Redis, runtime-local caches and LiteLLM are operational layers that do not change that ownership model.

```text
                           Aegora Control Plane
                   Agent / Release / RBAC / Registry
                                  |
                                  v
                            PostgreSQL
                       Control Plane Facts
                                  |
                                  v
                               Redis
                    Config L2 / Events / Invalidation
                                  |
                    +-------------+-------------+
                    |             |             |
                    v             v             v
               Runtime Pod 1 Runtime Pod 2 Runtime Pod 3
                 L1 Cache      L1 Cache      L1 Cache
                 MCP Pool      MCP Pool      MCP Pool
                    |             |             |
                    +-------------+-------------+
                                  |
                                  v
                            LiteLLM Proxy
                                  |
                       Router / Retry / Fallback
                                  |
                                  v
                           Model Providers
                                  |
                                  v
                         Prompt / KV Cache
```

### Runtime and cache hierarchy

The runtime is stateless with respect to durable business/configuration truth, but it may keep **disposable acceleration state**:

- **PostgreSQL — source of truth**: Agent drafts/releases, RBAC, capability state, MCP registrations, learning policies, audit records and other durable control-plane facts.
- **Redis — shared L2 and event fabric (target)**: versioned runtime-context cache, invalidation/version events and other rebuildable shared state. Redis must never become the canonical configuration database.
- **Runtime L1 — process-local hot cache**: resolved release/runtime contexts and bounded hot data. Versioned keys allow stale entries to stop matching after publish or policy changes.
- **MCP pool — process-local reusable connections**: runtime pods may reuse MCP sessions/connections, while pool state remains disposable and rebuildable after restart.
- **LiteLLM Proxy — central model gateway**: Runtime and Control Plane use stable `aegora-chat` / `aegora-fast` aliases through an OpenAI-compatible gateway; provider credentials and concrete model names remain behind LiteLLM. Staging deployment wiring is included under `deploy/`.
- **Langfuse — Agent/LLM observability**: optional Runtime root spans reuse Aegora trace/session/user context, while LiteLLM exports model generations through Langfuse OTEL so Agent execution and token/latency/cost data can be correlated.
- **Prompt/KV cache — inference optimization (optional)**: a performance layer below model routing, never an authorization or business-state source.

The intended configuration path is **PostgreSQL -> Redis L2 -> Runtime L1**. Publish, disable or policy changes advance a version and emit invalidation information; runtimes re-resolve on a miss/version change instead of relying on long TTLs for correctness.

### Design principles

- **Control plane / runtime separation**: configuration and governance live in the control plane; production execution lives in the runtime.
- **Stateless business execution**: runtime instances may keep disposable process-local caches and MCP sessions, but persistent business/configuration truth remains external.
- **Release-driven execution**: immutable Agent Releases define the capability ceiling; runtime authorization can only reduce that ceiling.
- **Dynamic authorization**: every run intersects release capabilities with current actor permissions, tool status and MCP connection state.
- **Capability-oriented integration**: MCP is the primary tool integration protocol; complex SOP/workflows are exposed as governed capabilities rather than leaking low-level APIs to the model.
- **Centralized configuration**: both applications read the repository-root `.env` by default; real secrets must never be committed.

## Governed data flywheel

Aegora treats runtime experience as **candidate improvement material**, not as permission for an Agent to silently rewrite itself. The target loop is:

```text
Production Runs / Traces / Human Feedback
                  |
                  v
      Evaluation + Failure Mining
                  |
                  v
   Reflection / Skill Draft / Proposal
                  |
                  v
 Policy Gate + Human Review + Audit Trail
                  |
          +-------+--------+
          |                |
          v                v
   Versioned Skill     Agent/Prompt Change
          |                |
          +-------+--------+
                  |
                  v
        Offline / Canary Evaluation
                  |
                  v
        Publish New Version
                  |
                  v
           New Production Runs
```

### Flywheel guardrails

- **Separate evidence from executable change**: traces, feedback and evaluations may produce proposals, but proposals do not alter production behavior by themselves.
- **Version everything promotable**: Skills, prompts, policies and Agent releases are immutable/versioned once published so decisions can be reproduced and rolled back.
- **Policy before promotion**: per-Agent learning policy defines what may be learned automatically, what requires review and what is prohibited from entering the learning set.
- **Quality gates**: candidate changes pass offline regression/evaluation and, where appropriate, canary rollout before becoming the new published version.
- **Full lineage**: retain source run/evidence, evaluator result, proposal, reviewer/policy decision and resulting version for auditability.
- **No authorization expansion through learning**: learned Skills/prompts cannot grant tools or scopes beyond the published release ceiling and current runtime authorization intersection.

The current codebase already contains foundations for this design: immutable releases, runtime policy resolution, governance/audit boundaries, Skills, the existing reflection/Skill-draft path, a central LiteLLM gateway integration and Langfuse trace correlation. The end-to-end automated flywheel and Redis L2 event fabric remain **roadmap architecture until their corresponding implementation lands**.

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
├─ README.md
└─ README_CN.md
```

## Local development

1. Copy the root configuration:

```bash
cp .env.example .env
```

2. Configure the `LITELLM_*` upstream provider variables (and Langfuse keys when tracing is enabled), then start the central gateway:

```bash
python -m pip install "litellm[proxy]"
litellm --config deploy/litellm-config.yaml --port 4000
```

3. Start the control-plane backend:

```bash
cd apps/control-plane/backend
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

4. Start the control-plane frontend:

```bash
cd apps/control-plane/frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

5. Start the runtime:

```bash
cd services/runtime
python -m pip install -r requirements.txt
PYTHONPATH=src uvicorn apps.api_app:app --host 127.0.0.1 --port 5000
```

## Migration status

This workspace is the consolidated successor of the existing Agent Platform and Agentic RAG Runtime repositories. The first migration keeps their proven module/package boundaries intact to reduce regression risk. New cross-plane capabilities should depend on versioned contracts under `packages/contracts` instead of importing implementation code across applications.

Planned next steps:

1. Workflow Capability phase 1 is implemented on the roadmap branch: administrators can register governed workflow capabilities backed by an existing MCP connection, and Runtime reuses the MCP execution adapter while preserving `source=workflow`. Next, add workflow version/lifecycle management and richer scope-schema editing.
2. Harden the LiteLLM/Langfuse production path: pin tested image digests, add multi-provider fallback policies, budgets and trace/evaluation dashboards.
3. Add Redis-backed L2 configuration caching plus version/event-driven invalidation, keeping runtime L1 caches disposable.
4. Generalize the existing reflection/Skill-draft path into per-Agent learning policies, evaluation gates and the governed data flywheel described above.

