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
