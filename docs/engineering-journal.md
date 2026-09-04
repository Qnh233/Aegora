# Engineering Journal

## 2026-09-04 — Governed flywheel evidence lineage

- **Objective / roadmap item**: establish the first governed-data-flywheel prerequisite without depending on the open Workflow, Redis or LiteLLM PRs.
- **Key design decisions / tradeoffs**: keep PostgreSQL/Strapi evidence storage unchanged; persist only stable Agent/Release identity into message metadata. Reflection may still analyze legacy rows, but it treats them as `legacy` and never clusters rows from different Agents together.
- **Pitfalls / root causes**: the existing configured runtime knew `agent_id` and `release_id` during execution but dropped that identity before chat persistence, so later reflection could mix semantically similar failures from unrelated Agents. This is a lineage loss, not a clustering-quality problem.
- **Highlights / reusable patterns**: normalize learning lineage once in `sessions.py`; carry `source_agent_id` plus bounded `source_trace_ids` into Skill draft metadata; partition embedding clustering by Agent before similarity grouping.
- **Important files / commands**: `services/runtime/apps/api_app.py`, `services/runtime/src/aegora_runtime/sessions.py`, `services/runtime/src/aegora_runtime/data_ops/reflection_flow.py`; targeted tests under `services/runtime/tests/test_sessions.py` and `test_skills.py`.
- **Verification / tests**: `29 passed` for targeted Runtime session/Skill tests; `python -m compileall -q services/runtime/src services/runtime/apps` passed; `git diff --check` passed. The local Anaconda environment initially lacked `psycopg` and `pocoflow`; both repository-declared dependencies were installed before rerunning the tests rather than masking import failures with stubs.
- **Blockers**: none for this phase. Existing PR #3 Python CI failure was independently confirmed as the known `main` pytest module-name collision already fixed in PR #1, so the fix was not duplicated here.
- **Next step**: add a per-Agent learning-policy contract that gates which evidence can produce proposals and which proposals may advance to evaluation/review.

## 2026-09-04 — Per-Agent learning-policy gate

- **Objective / roadmap item**: turn evidence lineage into an explicit Release-scoped learning contract so each Agent can independently control evidence capture and Skill proposal creation.
- **Key design decisions / tradeoffs**: keep the policy inside immutable Release configuration and persist a normalized snapshot with each chat turn. `capture_evidence` defaults to `true` so operational reports do not silently lose legacy visibility, while `propose_skills` defaults to `false` so autonomous learning is opt-in. `requires_human_review` defaults to `true`; promotion is intentionally not automated in this phase.
- **Pitfalls / root causes**: gating only at weekly-reflection execution time would make historical evidence semantics depend on whatever policy happens to be current later. Persisting the Release policy snapshot beside the evidence avoids that time-of-check/time-of-use drift. Mixed-policy rows inside one semantic cluster are handled conservatively: a Skill proposal is allowed only when every contributing evidence row permits it.
- **Highlights / reusable patterns**: normalize policy once in `runtime_context.py`; carry it through configured request persistence; filter disallowed evidence before both rule and embedding clustering; downgrade threshold-qualified but non-authorized clusters to `learning_review` instead of silently dropping them.
- **Important files / commands**: `services/runtime/src/aegora_runtime/runtime_context.py`, `services/runtime/apps/api_app.py`, `services/runtime/src/aegora_runtime/data_ops/reflection_flow.py`, targeted tests in `test_runtime_context.py` and `test_skills.py`; verification command: `python -m pytest services/runtime/tests/test_skills.py services/runtime/tests/test_runtime_context.py services/runtime/tests/test_sessions.py -q`.
- **Verification / tests**: `43 passed`; `python -m compileall -q services/runtime/src services/runtime/apps` passed; `git diff --check` passed.
- **Blockers**: no implementation blocker. Promotion/evaluation gates remain deliberately separate so they can be measured and reviewed before any automatic activation path exists.
- **Next step**: add an evaluation gate that records candidate evaluation results and prevents `source=agent` Skills from becoming active without explicit review criteria.
