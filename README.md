<p align="center">
  <img src="./docs/assets/aegora-logo.svg" alt="Aegora logo" width="260" />
</p>

<h1 align="center">Aegora</h1>

[中文文档](./README_CN.md)

**Enterprise Agent Control Plane & Stateless Runtime**

Aegora is an enterprise-oriented platform for registering, governing, composing and operating AI agents, MCP tools, workflows, long-term memory and organizational skills. A configuration-driven stateless Runtime provides the execution substrate, while pluggable ExecutionEngine and MemoryProvider interfaces keep execution/memory implementations replaceable and leave authorization, memory policy, Skill promotion and auditability under platform governance. The repository is organized as a monorepo while keeping the control plane and data plane independently deployable.

## Architecture

The diagram below describes the **target production topology**. PostgreSQL remains the source of truth; Redis, runtime-local caches and LiteLLM are operational layers that do not change that ownership model.

```text
                           Aegora Control Plane
          Agent / Release / RBAC / MCP / Memory Policy / Skill Governance
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
              ExecutionEngine ExecutionEngine ExecutionEngine
               MemoryService  MemoryService  MemoryService
                    |             |             |
                    +------+------+-+-----------+
                           |        |
                           v        v
                       LiteLLM   MemoryProvider
                         Proxy    Native PG / OpenViking
                           |
                  Router / Retry / Fallback
                           |
                           v
                    Model Providers
```

### Runtime and cache hierarchy

The runtime is stateless with respect to durable business/configuration truth, but it may keep **disposable acceleration state**:

- **PostgreSQL — source of truth**: Agent drafts/releases, RBAC, capability state, MCP registrations, learning policies, audit records and other durable control-plane facts.
- **Redis — shared L2 and event fabric (target)**: versioned runtime-context cache, invalidation/version events and other rebuildable shared state. Redis must never become the canonical configuration database.
- **Runtime L1 — process-local hot cache**: bounded immutable Release config entries sit in front of Redis L2. Versioned keys prevent cross-release reuse; entries are disposable and never contain mutable authorization facts.
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
- **Memory remains platform-governed**: providers handle storage/retrieval; Aegora decides what may be remembered, who may read/share it, when it expires, and whether experience may be promoted into a Skill.

## Governed Memory Plane

Aegora deliberately separates **conversation history, execution checkpoints, long-term memory and publishable Skills** instead of treating them as one generic “memory” layer:

```text
Conversation History        current conversational context
        |
        +--> Checkpoint      LangGraph / HITL / resume
        |
        +--> MemoryService
               |
          MemoryProvider
          /            \
     Native PG      OpenViking
          |
   Governance / Policy
          |
   Episodic / Semantic
          |
   Procedural Memory
          |
      Governed Skill
```

The P0–P6 Memory Plane currently includes:

- **Pluggable providers**: `MEMORY_PROVIDER=native_pg|openviking`. Native PostgreSQL remains the default; OpenViking is an independent HTTP memory backend rather than a Runtime Core dependency.
- **Multi-Agent / multi-tenant scopes**: `user_global`, `user_agent`, and `tenant_user`, governed by `tenant_required`, `user_controlled`, or `agent_private` namespace policies with stricter public-Agent access.
- **Governance before retrieval**: Release/user/tenant policy defines visibility; providers return candidates, then Aegora filters, reranks and budgets context before injection.
- **Background extraction**: explicit, stable, reusable user facts first become `memory_candidates`; sensitive/inferred/conflicting or insufficiently trusted candidates are blocked or sent to review instead of becoming facts directly from an LLM.
- **Versioning / conflict / forget / expiry**: long-term memory keeps provenance, replacement versions, explicit forget, TTL/expiry and a `memory_events` audit trail; ordinary automatic memory cannot overwrite tenant-managed facts.
- **OpenViking adapter**: trusted identity headers map tenant/user/agent identity, while the adapter handles `viking://~` canonicalization, built-in memory-type paths and overview/abstract documents.
- **Unified Memory Eval**: the same corpus can run against different providers and tracks recall, distractor rejection, privacy isolation, update/forget behavior, candidate decisions and context budget.

P5 validated migration idempotency plus the Native PG create → update → recall → forget → expiry → audit lifecycle on isolated WSL PostgreSQL + pgvector. OpenViking v0.4.21 also completed a real HTTP/storage/search integration run. Its current evaluation used a test embedding service to validate provider and isolation semantics, so it is **not presented as a production semantic-retrieval benchmark**. See [`services/runtime/docs/architecture.md`](./services/runtime/docs/architecture.md) for the detailed boundaries and evidence.

## Governed data flywheel

Aegora treats runtime experience as **candidate improvement material**, not as permission for an Agent to silently rewrite itself. The target loop is:

```text
Production Runs / Traces / Memory Evidence / Human Feedback
                             |
                             v
                      Weekly Reflection
                             |
               repeated success + feedback
                             |
                             v
                     Procedural Memory
                             |
                   Deterministic Gate
                             |
                             v
                        Skill Draft
                             |
                  Offline Eval / Regression
                             |
                             v
                     Canary / Shadow
                             |
                             v
                      Human Reviewer
                             |
                             v
                        Active Skill
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

The current codebase now carries this loop through **Memory → Reflection → Procedural Memory → Governed Skill**. Reflection only groups evidence for the same Agent when its learning policy allows capture. By default, a promotable procedural pattern requires at least three independent traces, two positive-feedback traces, successful Assistant-response evidence, and no negative-feedback conflict beyond policy thresholds. Candidates are deduplicated by `agent_id + fingerprint` and retain `source_trace_ids / positive_trace_ids / source_memory_candidate_ids` lineage. Passing the procedural gate still creates only a `draft` Skill; publishing requires content-bound Offline Eval, Regression, Canary/Shadow evidence and an explicit human reviewer. If later evidence turns a drafted procedural memory into `conflicted`, the Strapi-to-PostgreSQL active sync rechecks the live procedural state and blocks publication. Automatic procedural learning is disabled by default. Producing Canary evidence from real bounded traffic remains roadmap work.

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

1. Workflow Capability now includes governance editing plus the first version/lifecycle slice: administrators can register MCP-backed governed workflows, Runtime preserves `source=workflow`, and the console edits Input Schema, scope allow-lists and scope descriptions. Publishing now records an immutable `workflow_id + version` fact, allows only one `active` version per workflow, retires the previous active version when a new one is published, and projects the selected version into the `tools` runtime view; admin APIs can list history and retire a version. Next, add frontend history/retirement management and richer draft/canary lifecycle states.
2. Harden the LiteLLM/Langfuse production path: staging now requires an explicitly tested immutable LiteLLM image digest; next add multi-provider fallback policies, budgets and trace/evaluation dashboards.
3. Redis cache phase 1/2 plus the first event-fabric slice is included in this integration candidate: Runtime caches only immutable Release `config_json` with bounded process-local L1 + versioned Redis L2; Control Plane emits versioned publish/revoke/tool-policy events and Runtime consumes them, evicting exact release-version cache entries when relevant. Runtime Prometheus metrics expose applied/ignored/invalid event counts, event lag, and subscriber reconnect failures; the Control Plane keeps low-cardinality publisher success/failure/disabled counters behind an admin-only operational endpoint. Release status, RBAC, tool state and MCP connection state remain live PostgreSQL reads. Next, add tool-session convergence only where a real stale-session risk is demonstrated.
4. Governed learning now includes the P6 path: Memory Evidence → Reflection → Procedural Memory → Skill Draft → Eval/Regression → Canary → Human Review → Active Skill, with deduplication, negative-feedback conflicts, live procedural-state rechecks and full lineage. Next, generate Canary evidence from real bounded shadow/limited traffic and expand evaluation on real multi-turn datasets.
5. Native PG remains the default Memory Provider while OpenViking stays a pluggable experimental provider. Next, run same-corpus A/B with a real embedding model and, where multi-replica deployment is required, validate shared storage/indexing, HA and capacity boundaries instead of treating “multiple Pods can start” as proof of mature clustering.

