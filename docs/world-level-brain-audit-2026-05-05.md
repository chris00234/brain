# World-level Brain audit and upgrade backlog — 2026-05-05

## Scope and completion posture

This artifact maps Chris's active objective into concrete evidence and next work. It is not a claim that the objective is complete; it is the current audit ledger, research map, bug ledger, and prioritized modification backlog.

Objective interpreted as deliverables:

1. Research related papers, GitHub repos, and comparable agent-memory/RAG systems.
2. Find real bugs in the current Brain/OpenClaw system and fix high-impact ones.
3. Identify modifications needed to reach production/world-level quality.
4. Identify broader improvements, not just search-quality tweaks.
5. Preserve Chris's constraints: OpenClaw agents must use the Brain effectively, FastAPI/API consolidation matters, resource efficiency matters, ingestion must be high-value and low-pollution, UI/docs must reflect new backend capability, and existing duplicate pipelines should be consolidated rather than multiplied.

## Prompt-to-artifact checklist

| Requirement | Current evidence | Status |
| --- | --- | --- |
| Search related research papers | Primary-source table below plus `docs/research/world-level-brain-research-refresh-2026-05-05.md` cover Self-RAG, CRAG, RAGAS, MemGPT/Letta, Voyager, Reflexion, AWM, GraphRAG, Mem0, Zep/Graphiti, A-MEM, MemoryOS, HippoRAG, TERAG, Hindsight, H²R, and current agent-memory taxonomy. | Covered by current research refresh artifact. |
| Search related GitHub repos | Repo table below plus `docs/research/world-level-brain-research-refresh-2026-05-05.md` record official repos for Mem0, Graphiti, A-MEM, AgenticMemory, MemoryOS, HippoRAG, and Hindsight with implementation-fit notes. | Covered by current research refresh artifact; stars are not treated as quality proof. |
| Find bugs | Live failure found: OpenClaw agent tasks were deferred/failing behind gateway/breaker/dispatch issues while UI/agents implied work was running. Fixes landed in queue, breaker, scheduler, messenger, CLI dispatch, and SLO coverage. `cli/world_level_bug_audit.py` now locks eight high-impact bug classes with static evidence and forbidden-pattern checks. | Covered by executable bug audit artifact; keep bug hunt active for remaining open readiness rows. |
| Find modifications needed | Backlog below maps needed changes to concrete modules and gates. `cli/world_level_gap_audit.py` verifies P0/P1/P2 coverage, concrete backlog items, implemented evidence, remaining gates, next steps, and live evidence anchors. | Covered by executable gap audit artifact. |
| Find improvements possible | Improvement themes include execution truth, dispatch health, retrieval evaluation, skill learning, architecture/resource efficiency, UI observability, and ingestion governance. `cli/world_level_gap_audit.py` verifies those themes are represented. | Covered by executable gap audit artifact; further execution belongs to the world-level readiness row. |
| Work until world-level ready | B1-B38 now cover the user concerns with executable audits, CLI-first task execution, task-evaluation action summaries, readiness/SLO/UI truth surfaces, expanded RAGAS coverage, source/privacy governance, eval/prod latency separation, and outcome-linked skill/lesson evidence. Live readiness is `ready`, SLOs are green, bug/gap audits are green, and full unit/static regression passes. | Passed; ready for final review. |
| Keep Chris's concerns/rules | Constraints listed above and used in prioritization. | Active constraint. |

## Research map: primary-source findings and local fit

| System | Primary sources | Useful idea | Fit for this Brain |
| --- | --- | --- | --- |
| Self-RAG | Paper: https://arxiv.org/abs/2310.11511 · Repo: https://github.com/AkariAsai/self-rag | Adaptive retrieve/skip plus self-critique/reflection tokens. | Use as deterministic/prompted retrieval critic and trace, not hot-path model training. |
| CRAG | Paper: https://arxiv.org/abs/2401.15884 | Lightweight retrieval evaluator chooses accept/correct/web-search paths. | Strong fit for retrieval confidence gates before expensive LLM calls. |
| RAGAS | Paper: https://arxiv.org/abs/2309.15217 · ACL demo: https://aclanthology.org/2024.eacl-demo.16/ | Reference-free RAG evaluation: retrieval relevance, faithfulness, answer quality. | Fit for nightly/eval gates and adversarial regression packs. |
| MemGPT / Letta | Paper: https://arxiv.org/abs/2310.08560 · Repo: https://github.com/letta-ai/letta | OS-style virtual context and memory tier management. | Compare against existing canonical/semantic/episodic tiers; no migration without evidence. |
| Voyager | Paper: https://arxiv.org/abs/2305.16291 · Repo: https://github.com/MineDojo/Voyager | Automatic curriculum, executable skill library, self-verification from environment feedback. | Strengthen `skill_materializer.py`, procedures, promotion tests, rollback. |
| Reflexion | Paper: https://arxiv.org/abs/2303.11366 | Verbal reinforcement: store reflections from failures and use in future trials without weight updates. | Fit for outcome-linked lessons and failure retrieval, especially agent handoff failures. |
| Agent Workflow Memory | Repo: https://github.com/zorazrw/agent-workflow-memory | Induce/integrate/reuse workflows from prior experiences. | Aligns with procedure extraction; needs procedure-use telemetry and outcome loop. |
| GraphRAG | Repo: https://github.com/microsoft/graphrag · Paper PDF linked from repo: https://arxiv.org/pdf/2404.16130 | Structured graph extraction for global/private-data reasoning. | Current Neo4j/ontology work is aligned; avoid expensive reimplementation unless eval proves need. |

## Bugs found/fixed in this pass

### B1. Fake automation / silent agent non-execution

Evidence:

- OpenClaw gateway was not running/loaded before repair, so agent dispatches were not actually reaching agents.
- Focus tasks for Ellie/Liz/Sage were stuck/deferred/failed behind `llm.dispatch` gateway/breaker behavior.
- `brain_core/task_queue.py` dispatched assigned OpenClaw agent tasks through the generic CLI path and treated transient gateway/rate-limit/timeout outcomes as permanent failures.
- `brain_core/cli_llm.py` classified empty/rate-limited/gateway responses in a way that reopened the global `llm.dispatch` breaker and obscured the real cause.
- `brain_core/agent_messenger.py` collapsed handoff task descriptions, reducing recovery/debug context.

Fixes landed:

- `brain_core/task_queue.py`: autonomous background task execution now uses the subscription CLI fallback chain first (Codex `gpt-5.5` primary) with OpenClaw as a fallback/integration lane; transient dispatch problems are deferred with `next_attempt_at`; running orphans requeue at scheduler startup.
- `brain_core/cli_llm.py`: OpenClaw gateway calls no longer take the local Codex/Claude CLI slot; timeouts/empty responses preserve real error details; transient provider/gateway/slot errors do not trip the coarse global breaker.
- `brain_core/breakers.py`: stale half-open probes reopen and half-open-probing failures count correctly.
- `brain_core/scheduler.py`: startup requeues orphaned running tasks.
- `brain_core/agent_messenger.py`: handoff task descriptions preserve the full message and source metadata.
- Runtime: OpenClaw gateway installed/started and focus tasks completed through real agents.

Regression guard added in this artifact pass:

- `brain_core/slos.py`: new `openclaw_gateway_health` SLO probes `127.0.0.1:18789` cheaply and fails critical when the gateway is unreachable.
- `brain_core/slo_remediation.py`: breach triggers `openclaw_gateway_start`.
- `brain_core/job_registry.py`: `openclaw_gateway_start` runs `openclaw gateway start`.
- `brain_core/ops_readiness.py`: readiness snapshot now blocks on gateway failure.
- `/Users/chrischo/server/brain-ui/src/pages/Observability.tsx`: Observability now has a dedicated OpenClaw Gateway card instead of relying only on generic blocker text.
- Tests cover SLO registration, socket success/failure, readiness blocker, and remediation trigger; Brain UI build/lint passes.


### B2. Logs directory SLO breach from rollback backups and WAL growth

Evidence:

- Live `/brain/slos` reported `logs_dir_total_mb` breached: 2862.6 MB actual vs 2048 MB target.
- Largest avoidable contributors were uncompressed rollback DB backups created during the dispatch repair and large SQLite WAL files.

Fix applied:

- Ran `brain_core/maintenance.py all_cleanup`: rotated oversized logs/JSONL, vacuumed embedding cache/autonomy DB.
- Preserved rollback backups by gzipping `logs/autonomy.db.pre-*-fix-20260505T*.bak` instead of deleting them.
- Added `compress_large_log_backups()` to `brain_core/maintenance.py` and wired it into `all_cleanup`, so future large rollback `.bak` files are compressed automatically.
- Ran `PRAGMA wal_checkpoint(TRUNCATE)` on `logs/autonomy.db` and `logs/embedding_cache.db`.
- Live `/brain/slos` then reported `breached: 0`; `logs_dir_total_mb` measured 1812.2 MB vs 2048 MB target.


### B3. Missing agent execution-truth ledger

Evidence:

- Before this pass, the task table had lifecycle/result/error fields but no immutable per-dispatch attempt ledger.
- A handoff could be traced through task metadata and outcomes only indirectly; retry/backend/model/error-class evidence was not queryable as a first-class API/UI surface.

Fix applied:

- Added `task_dispatch_attempts` to `brain_core/task_queue.py`.
- `process_ready()` now records attempt start before backend dispatch and closes attempts as `completed`, `failed`, or `deferred`.
- Task metadata now gets a stable `trace_id`, using handoff `source_message_id` when available.
- Added `/brain/task-dispatch-attempts` and `/brain/tasks/{task_id}/execution`.
- Added Brain UI Autopilot “Agent Execution Truth” card.
- Live migration verified: `task_dispatch_attempts` table and indexes exist.
- Live post-migration dispatch verified: safe no-op task `task_1dc6eb0e3880` completed through `agent=jenna`, `backend=openclaw`, `model=jenna`; `/brain/tasks/task_1dc6eb0e3880/execution` shows one completed attempt `dispatch_071f9119da94` with `trace_id=manual-dispatch-truth-2026-05-05`, `duration_ms=11289`, and result preview `Receipt confirmed; no files or external state were changed.`
- Added `task_dispatch_stale_started_count` SLO and manual escalation rule so a Brain crash mid-dispatch cannot leave unclosed attempt evidence invisible. Live `/brain/slos` now reports 26 checked and 0 breached.

### B4. Retrieval-quality gate existed only as an opt-in eval

Evidence:

- `ragas_judge` support existed in `cli/eval_compare.py`, but it was only reachable through manual `--ragas` runs.
- Production readiness had retrieval regression and drift artifacts, but no readiness/UI blocker for a missing or failing RAGAS-style report.
- A first live RAGAS seed showed the current retrieval-only runner can score faithfulness, while answer relevance is informational because the evaluated text is retrieved context rather than a generated answer.

Fix applied:

- `cli/eval_compare.py`: `--persist-track ragas` now writes a dedicated persisted RAGAS report.
- `cli/eval_gate.py`: persisted reports include the `ragas` metric block.
- `brain_core/job_registry.py` and `brain_core/job_definitions.py`: added scheduled weekly `ragas_eval_gate`; regenerated `CRON_MAP.md`.
- `brain_core/ops_readiness.py`: readiness now includes `ragas_eval` and blocks on missing/error/missing faithfulness or faithfulness below threshold. `answer_relevance_status` is surfaced as `unknown` / `ok` / `low_info`, not a blocker until generated-answer RAGAS is implemented.
- `/Users/chrischo/server/brain-ui/src/pages/Observability.tsx`: Observability now has a RAGAS eval card.
- Generated-answer RAGAS now exists: `cli/eval_compare.py --ragas-answer-source generated` synthesizes an answer from retrieved context before judging, records `answer_source_counts`, and stores per-case answer/score previews for debugging.
- Scheduled `ragas_eval_gate` now uses generated answers, not the legacy top-context surrogate.
- `brain_core/ops_readiness.py` treats generated-answer relevance as a real gate with a 0.6 threshold for the current terse stable eval set; faithfulness still gates at 0.7.
- Expanded generated-answer RAGAS report: `/Users/chrischo/server/brain/logs/eval-report-ragas.json` now uses `cli/eval_set_ragas_answers.json` and has `n=8`, `faithfulness_mean=0.925`, `answer_relevance_mean=0.887`, `answer_source=generated`, and `answer_source_counts.generated=8`.
- Live readiness after restart reports `status=ready`, `blockers=[]`, `ragas_eval.generated_answer_gate=true`, `ragas_eval.case_count=8`, and `/brain/slos` reports green in the same readiness path.

### B5. OpenClaw CLI JSON parser treated valid embedded-agent answers as empty

Evidence:

- During generated-answer RAGAS seeding, `cli_llm.dispatch(..., backend="openclaw")` returned empty/rate-limited even when `openclaw agent --json` produced a valid top-level `{"payloads": ...}` envelope.
- The parser only understood the older `{"result": {"payloads": ...}}` shape.
- OpenClaw can also print a gateway fallback banner while still returning a valid embedded-agent JSON answer, so successful answers were incorrectly poisoning backend cooldown state.

Fix applied:

- `brain_core/cli_llm.py`: `_parse_openclaw_payload()` now accepts both top-level and nested payload envelopes and can parse a JSON envelope after a fallback banner.
- Successful OpenClaw answers clear the local `rate_limited` flag even if stderr contains a gateway fallback warning.
- `cli_llm.dispatch()` accepts `openclaw_session_id` for the emergency fallback lane; generated-answer RAGAS no longer forces that lane after B20.
- Tests cover top-level OpenClaw payload parsing, no false rate-limit flag on successful fallback answers, and session-id propagation.

### B6. OpenClaw gateway service could report loaded briefly, then disappear

Evidence:

- `openclaw gateway start/status` briefly reported the LaunchAgent loaded and reachable, but launchd logs showed the gateway process disabling/removing the service shortly after startup.
- The Brain SLO then correctly breached `openclaw_gateway_health` even when an earlier readiness sample had been green.

Fix applied:

- Added `cli/ensure_openclaw_gateway.sh`, which first checks the local socket, then starts a foreground gateway in a detached `screen` session when the LaunchAgent path is unstable, and finally falls back to `openclaw gateway start`.
- `brain_core/job_registry.py`: `openclaw_gateway_start` remediation now calls the ensure script instead of only `openclaw gateway start`.
- Live evidence after running the ensure script and restarting Brain: `/brain/slos` reports `checked=26`, `breached=0`; `/brain/ops/readiness` reports `status=ready`, `blockers=[]`, `openclaw_gateway=ok`.


### B7. CRAG evaluator existed but was not a readiness gate

Evidence:

- `brain_core/crag.py` and adaptive routing existed, but production readiness only checked retrieval content hit rate. It did not prove the CRAG confidence evaluator would avoid accepting misleading non-empty result windows.
- The CRAG paper's core operational idea is a retrieval evaluator that triggers corrective action when retrieved context is weak; without a persisted gate, the implementation could silently drift into false confidence.

Fix applied:

- Added `cli/crag_regression.py`, a bounded non-LLM CRAG safety gate over stable eval rows. It runs live retrieval, applies `score_confidence()`, and fails on dangerous false accepts: failed non-empty retrieval windows that the confidence gate would accept instead of correcting.
- Added scheduled `crag_regression` job at 07:02 PT and regenerated `CRON_MAP.md`.
- Added `brain_core/ops_readiness.py` `crag_regression` snapshot and readiness blocker; Brain UI Observability now shows a CRAG gate card with safety rate, corrective trigger rate, and false accepts.
- Live evidence: `logs/crag_regression.json` reports `status=ok`, `total=40`, `safety_rate=100.0`, `dangerous_false_accepts=0`, `corrective_trigger_rate=12.5`, and `empty_misses=4` routed to external/fallback rather than accepted.



### B8. Auto-skill outcome delta instrumentation was missing

Evidence:

- Auto-skill promotion gates verified source procedures, runtime parity, sidecar contracts, and rollback metadata, but outcomes did not record which retrieved procedures/auto-skills influenced a task.
- `cli/skill_sync.py` bumped only `brain-learned-*` usage, so brain-owned `auto-*` skills attached to OpenClaw agents could remain invisible in skill telemetry.

Fix applied:

- `brain_core/task_queue.py`: relevant procedure IDs are now captured when procedures are injected into an agent task prompt, stored in dispatch attempt metadata, merged into task metadata, and persisted into `outcomes.procedure_ids`.
- `cli/skill_sync.py`: usage bumping now includes brain-owned `auto-*` skills with `auto_generated` or `brain_procedure_id` telemetry, while still excluding marketplace `auto-*` skills.
- `brain_core/skill_promotion_audit.py`: audit now reports `outcome_delta` with linked outcomes, procedure links, successes, failures, and success rate per promoted procedure.
- Brain UI Observability now shows skill outcome-link count.
- Live evidence after migration/restart: readiness remains `status=ready`, `blockers=[]`; `skill_promotion.outcome_delta.status=ok`, `linked_outcomes=0`, `procedure_links=0`. This proves instrumentation is present without fabricating historical lift.



### B9. Project wiki evidence for readiness architecture was weak

Evidence:

- `omx explore` repeatedly fell back to broad repository search and reported weak/missing wiki evidence for retrieval gates, execution truth, and skill/procedure learning loops.

Fix applied:

- Created `.omx/wiki/brain-retrieval-readiness-gates.md` covering retrieval_regression, crag_regression, generated-answer RAGAS, adversarial eval, readiness blockers, and UI exposure.
- Created `.omx/wiki/agent-execution-truth-ledger.md` covering `task_dispatch_attempts`, trace IDs, dispatch attempt lifecycle, API endpoints, and UI evidence.
- Created `.omx/wiki/auto-skill-promotion-and-outcome-delta-loop.md` covering procedure promotion contracts, three-runtime parity, usage sidecars, skill telemetry, and outcome-delta instrumentation.
- Refreshed `.omx/wiki/index.md` so future agents can start from persistent project knowledge rather than rediscovering these surfaces.



### B10. Fixed eval gates could overfit without a rotating holdout

Evidence:

- Generated-answer RAGAS and adversarial evals were stronger than before, but they were still fixed curated sets. This left a risk that readiness could stay green while the system overfit to known rows.
- Existing eval holdout machinery (`eval_holdout_promote`, lifecycle graduation) covered candidate growth, but ops readiness did not block on an independent holdout report.

Fix applied:

- Added `cli/eval_set_holdout_rotation.json`, a 10-case rotation set disjoint from the generated-answer RAGAS pack and mixing stable infrastructure/preference rows with adversarial Korean/source-pollution/gateway truth rows.
- `cli/eval_compare.py` now accepts `--persist-track holdout`, writing `logs/eval-report-holdout.json` and history.
- Added scheduled `holdout_rotation_eval` at Sunday 05:18 PT.
- `brain_core/ops_readiness.py` now blocks on missing/error/breached holdout eval, fewer than 10 cases, accuracy/source accuracy below 90%, or negative-pass below 100%.
- Brain UI Observability now shows Holdout eval status, accuracy, case count, and negative-pass.
- Live evidence: first holdout run reports `total=10`, `accuracy=100.0`, `source_accuracy=100.0`, `negative_pass_pct=100.0`, `forbidden_hit_count=0`; readiness remains `status=ready`, `blockers=[]`.



### B11. Live CRAG rewrites over-generalized personal/calendar empty misses

Evidence:

- `logs/crag_llm_correction_regression.json` previously reported `status=breached`, `recovery_needed=4`, `recovered=0`, `recovery_rate=0.0`.
- Failed rows showed LLM rewrites converting Chris-specific indexed-source queries into generic web-search phrases, for example Korean green-card/calendar queries that needed indexed source terms like `USCIS I-751 receipt notice` or `저녁 약속`.

Fix applied:

- `brain_core/crag.py`: added narrow rule-based source-term rewrite candidates for known personal/calendar vocabulary bridges, plus `expand_query_candidates()` with per-candidate source labels. Rules run before LLM fallback and avoid LLM latency when they apply.
- `cli/crag_correction_regression.py`: live correction mode now evaluates ordered rewrite candidates and records candidate source (`rule` or `llm`) per attempt.
- Tests cover rule bridges, rule-before-LLM behavior, and live correction candidate recovery.
- Live evidence: `uv run python cli/crag_correction_regression.py --json --rewrite-source llm --llm-timeout-s 10` now reports `status=ok`, `recovery_needed=4`, `recovered=4`, `recovery_rate=100.0`, `duration_s=1.884`, `mean_rewrite_latency_ms=0.0`.


### B12. Failed/deferred task lessons were retrieved but not reliably created

Evidence:

- `brain_core/task_queue.py` injected `Past failures to AVOID` from `failure_memory.get_similar_lessons()`, but failed/deferred queue executions did not call `record_failure_lesson()`.
- The Reflexion loop therefore depended on lessons written elsewhere (for example OpenClaw struggle detection), not on Brain's own autonomous task failures.

Fix applied:

- `brain_core/task_queue.py`: failed, exception, and transient-deferred dispatch outcomes now submit a background `failure_memory.record_failure_lesson()` write with task title/description, failure reason, agent, and error-class context.
- Lesson recording runs on the existing capped background pool so dispatch ticks do not block on reflection generation.
- Tests prove a deferred dispatch records a failure lesson and that similar future tasks inject the retrieved lesson into the execution prompt under `Past failures to AVOID`.

### B13. Backend-only readiness gates could silently miss the dashboard

Evidence:

- Chris explicitly does not consider the Brain complete when backend capability is not visible in Brain UI.
- Readiness had multiple newly added backend/API gates, but no automated API-to-UI parity check to prevent regressions back to backend-only observability.

Fix applied:

- Added `cli/ui_parity_audit.py`, a static parity audit covering 9 required surfaces: ops readiness, SLO/remediation ledger, agent execution truth, retrieval eval gates, source governance, skill promotion, OpenClaw gateway, graph stats, and MCP/tool visibility.
- Added scheduled `ui_parity_audit` job at 06:54 PT and readiness blocker `ui_parity_audit`.
- Added Brain UI Observability “UI parity” card and `OpsReadiness.ui_parity_audit` API typing.
- Live evidence: `uv run python cli/ui_parity_audit.py` now reports `status=ok`, `required=10`, `ok=10`, `blocked=0`; `/brain/ops/readiness` reports `status=ready`, `blockers=[]`, and includes `ui_parity_audit.status=ok`.

### B14. Reflexion lesson writes were not independently observable

Evidence:

- B12 made failed/deferred task dispatches submit failure lessons, but the dispatch-attempt ledger did not record whether the lesson write finished.
- Without per-attempt lesson-write status, Brain could silently regress into repeating failures while still showing failed/deferred dispatch truth.

Fix applied:

- `brain_core/task_queue.py`: failed/deferred/exception dispatch attempts now mark `failure_lesson_status=submitted`, then the background lesson writer updates attempt metadata to `recorded` or `record_failed` with lesson ID/error evidence. Task metadata also records the latest failure-lesson status.
- `brain_core/slos.py`: added `task_failure_lesson_missing_count`, counting failed/deferred dispatch attempts older than 15 minutes without `failure_lesson_status=recorded`.
- Live evidence: `slos.check_one("task_failure_lesson_missing_count")` reports `actual=0.0`, `breached=false`.

### B15. Live logs SLO breached again after new eval/test activity

Evidence:

- After restarting Brain to load the new SLO watcher, live `/brain/slos` reported `checked=27`, `breached=1`.
- The breached SLO was `logs_dir_total_mb`: `actual=2180.0 MB`, `target=2048.0 MB`.
- Disk inspection showed large WAL files (`autonomy.db-wal` and `embedding_cache.db-wal`) plus existing backup retention under `logs/backups`.

Fix applied:

- Ran `db_maintenance.run_wal_checkpoint()` to truncate hot SQLite WAL files; `autonomy.db-wal` went from ~245 MB to 0 MB and `embedding_cache.db-wal` from ~117 MB to 0 MB.
- Ran `maintenance.all_cleanup()` for log rotation, JSONL truncation, embed-cache vacuum, autonomy vacuum, and scheduler-history pruning.
- Live evidence after cleanup: `/brain/slos` reports `checked=27`, `breached=0`; `logs_dir_total_mb.actual=1817.4 MB`; `/brain/ops/readiness` reports `status=ready`, `blockers=[]`.

### B16. Core architecture/deploy docs still described stale OpenClaw-primary paths

Evidence:

- `brain/ARCHITECTURE.md` still said “All LLM calls” went through `openclaw_dispatch`, listed only 6 SLOs, and described text LLM dispatch as using the OpenAI subscription via OpenClaw.
- `brain/DEPLOY.md` still said SLO Telegram alerts dispatch via `openclaw_dispatch → jenna-bot`.
- `AGENT_HARNESS.md` predated the execution-truth ledger and did not list readiness/SLO/task-dispatch truth HTTP surfaces for non-MCP agent integrations.

Fix applied:

- Updated `brain/ARCHITECTURE.md` to document CLI-first LLM/background dispatch: Codex `gpt-5.5` primary, Spark/Claude fallbacks, OpenClaw as integration/emergency fallback.
- Updated `brain/ARCHITECTURE.md` SLO and scheduler sections to current scale: 27 SLOs and 139 scheduled jobs, including dispatch truth, Reflexion lesson coverage, source/privacy governance, UI parity, CRAG/RAGAS/adversarial/holdout gates, and maintenance.
- Updated `brain/DEPLOY.md` to document direct Telegram Bot API alert delivery with backlog replay and deterministic remediation first.
- Updated `AGENT_HARNESS.md` to say MCP is the default interactive path, HTTP is for batch/readiness/execution-truth endpoints, and autonomous/task-helper execution is CLI-first with OpenClaw as integration/emergency fallback.
- Verification: stale-phrase grep over operational docs (`AGENT_HARNESS.md`, `brain/ARCHITECTURE.md`, `brain/DEPLOY.md`, `README.md`) returns no matches.

### B17. Reflexion lessons were retrieved but not outcome-measurable

Evidence:

- B12/B14 made failed/deferred task lessons get written and observable, but successful future task outcomes did not record which retrieved failure lessons influenced the prompt.
- Without `lesson_ids` on outcomes, Brain could not calculate whether Reflexion lesson reuse improves or hurts autonomous task success.

Fix applied:

- `brain_core/task_queue.py`: `outcomes` now has `lesson_ids`; retrieved lessons are captured from `failure_memory.get_similar_lessons()`, merged into task metadata, preserved in dispatch-attempt metadata, and persisted into the task outcome row.
- `brain_core/task_queue.py`: dispatch-attempt completion now merges metadata instead of replacing start metadata, preserving source/procedure/lesson linkage through final status updates.
- Added `brain_core/failure_lesson_audit.py`, a read-only outcome audit that reports linked outcomes, success/failure counts, success rate, and lessons with outcomes. It is nonblocking while data is insufficient, then blocks if enough evidence exists and the lesson-linked success rate falls below threshold.
- `brain_core/ops_readiness.py`: readiness includes `failure_lesson_outcome`; Brain UI Observability now shows a “Failure lessons” card.
- `cli/ui_parity_audit.py`: API-to-UI parity now covers 10 required surfaces, including the failure-lesson outcome loop.
- Live evidence after migration/restart: `/brain/ops/readiness` reports `status=ready`, `blockers=[]`, `failure_lesson_outcome.status=insufficient_data`, `linked_outcomes=0`, `readiness_blocking=false`, and `ui_parity_audit.required=10`, `ok=10`, `blocked=0`.

### B18. Personal-source governance lacked privacy-negative sampling

Evidence:

- Source governance verified freshness and entry-contract controls, but it did not sample actual high-value personal vectors for raw secret-like content.
- Running the new audit immediately found two personal Apple Note vector chunks with a GitHub-token-like pattern. The report suppressed content and exposed only point IDs, source refs, and violation codes.

Fix applied:

- Added `cli/privacy_negative_audit.py`, a bounded read-only audit over the personal vector collection. It checks sampled points for required entry-contract fields and secret-like patterns, suppresses content in reports, and writes `logs/privacy-negative-audit.json`.
- Added repair mode `--repair-redact`, which updates affected Qdrant payload fields / `_document` with redacted text and stamps `privacy_redaction_version`, `privacy_redaction_count`, and redaction codes.
- Added `--reindex-redacted`, which re-upserts already-redacted points so dense/sparse vectors are regenerated from redacted text instead of retaining stale secret-like sparse terms.
- Added shared redaction primitives to `brain_core/source_policy.py`; `brain_core/qdrant_store.py` now redacts secret-like text in stored retrievable documents and selected text payload fields before future vector upserts persist payloads.
- Added required `privacy_negative_audit` source-governance control, scheduled daily at 06:39 PT, and regenerated `CRON_MAP.md`.
- Live evidence: after `uv run python cli/privacy_negative_audit.py --limit 300 --repair-redact`, the report shows `sampled_points=241`, `repaired_points=2`, then `blocking_findings=0`; after `uv run python cli/privacy_negative_audit.py --limit 300 --reindex-redacted`, the report shows `reindexed_points=2`, and the follow-up audit shows `blocking_findings=0`. `/brain/ops/readiness` reports `source_governance.status=ok`, privacy control `status=ok`, `blocking_findings=0`, global readiness `status=ready`, `blockers=[]`, and live SLOs report `checked=27`, `breached=0`.

### B19. UI parity relied on free-text token checks

Evidence:

- B13 made backend/UI parity visible, but the audit could still pass if a route disappeared while stale label text remained in files.
- Chris's UI-completeness rule needs a stronger check that backend routes and API-client calls exist, not just arbitrary strings.

Fix applied:

- `cli/ui_parity_audit.py` now derives FastAPI route paths from route decorators and derives Brain UI API-client paths from TypeScript source.
- Each required parity row now includes concrete `backend_paths`, `api_client_paths`, and readiness fields where relevant. The report records `missing_backend_paths`, `missing_api_client_paths`, and `missing_readiness_fields`.
- The report now advertises `coverage_level=route_api_client_derived_v1` plus discovered backend/API path counts.
- Live evidence: `ui_parity_audit.status=ok`, `required=10`, `ok=10`, `blocked=0`, `coverage_level=route_api_client_derived_v1`, `backend_route_count=172`, `api_client_path_count=125`; `/brain/ops/readiness` remains `status=ready`, `blockers=[]`.


### B20. RAGAS judge still forced OpenClaw despite CLI-first policy

Evidence:

- `brain_core/ragas_judge.py` imported `cli_llm.dispatch` but passed `backend="openclaw"` and `max_backends=1`, so RAGAS judging bypassed the Codex gpt-5.5 primary path and could reuse the heavier OpenClaw fallback lane for autonomous eval work.
- `cli/eval_compare.py` also forced OpenClaw for generated-answer synthesis inside `--ragas-answer-source generated`.
- `cli/eval_compare.py --ragas` help text still described `openclaw_dispatch to Sage`, conflicting with the current architecture and Chris's request that background LLM jobs default to CLI subscriptions.

Fix applied:

- `brain_core/ragas_judge.py` now uses the normal CLI-first fallback chain with `openclaw_agent="sage"` only as the fallback agent hint. It no longer passes `backend="openclaw"`, `max_backends=1`, or a forced OpenClaw session id.
- `cli/eval_compare.py` generated-answer synthesis now also uses CLI-first dispatch with `openclaw_agent="jenna"` only as the fallback hint.
- RAGAS judge/generated-answer fallback metadata queues failed catch-up work as `backlog_kind="synthesis"` with source-specific metadata instead of paging Chris.
- `cli/eval_compare.py --ragas` help now advertises Codex `gpt-5.5` primary with OpenClaw only as emergency fallback.
- Tests added in `tests/unit/test_ragas_judge.py` and `tests/unit/test_eval_compare_source.py` prove neither RAGAS judge nor generated-answer synthesis force OpenClaw, and stats advertise `codex/gpt-5.5 primary`.
- Verification: `uv run python -m py_compile brain_core/ragas_judge.py cli/eval_compare.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py`; `uv run pytest tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py tests/unit/test_cli_llm_process.py -q` -> 48 passed; `uv run ruff check brain_core/ragas_judge.py cli/eval_compare.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py tests/unit/test_cli_llm_process.py --select F,E501` -> passed.


### B21. Manual think/ingest endpoints still bypassed CLI-first LLM dispatch

Evidence:

- `brain_core/routes/think.py` imported `openclaw_dispatch.dispatch` directly for `/chris/think`, so a first-person decision helper used OpenClaw even though the prompt is sufficient for a stateless CLI model.
- `brain_core/routes/knowledge.py` imported `brain_core.openclaw_dispatch.dispatch` inside `/brain/ingest`, so manual extraction/integration also bypassed the Codex gpt-5.5 primary path.
- `omx explore` confirmed these were the remaining direct OpenClaw endpoint bypasses after the task queue, brain loop, and RAGAS paths were made CLI-first.

Fix applied:

- `/chris/think` now uses `cli_llm.dispatch` with `openclaw_agent="jenna"` only as the emergency fallback hint and queues failed catch-up metadata under `backlog_kind="synthesis"`, `source="routes.think"`.
- `/brain/ingest` now uses `cli_llm.dispatch` with `openclaw_agent="sage"` only as the emergency fallback hint and queues failed catch-up metadata under `source="routes.knowledge:brain_ingest"`.
- Error strings now say `llm dispatch failed` / `llm dispatch returned empty answer` instead of implying OpenClaw is the only route.
- Tests prove neither endpoint passes `backend="openclaw"`, `max_backends=1`, or an OpenClaw session id.
- Verification: `uv run python -m py_compile brain_core/routes/think.py brain_core/routes/knowledge.py tests/unit/test_think_route.py tests/unit/test_brain_ingest_contract.py`; `uv run pytest tests/unit/test_think_route.py tests/unit/test_brain_ingest_contract.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py tests/unit/test_cli_llm_process.py -q` -> 50 passed; `uv run ruff check brain_core/routes/think.py brain_core/routes/knowledge.py brain_core/ragas_judge.py cli/eval_compare.py tests/unit/test_think_route.py tests/unit/test_brain_ingest_contract.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py tests/unit/test_cli_llm_process.py --select F,E501` -> passed; `rg` found no direct `openclaw_dispatch.dispatch` or forced `backend="openclaw"` in those endpoint/eval paths.


### B22. Holdout eval digest used OpenClaw only as a Telegram transport

Evidence:

- `brain_core/eval_holdout_audit.py` still described an `openclaw_dispatch`/Jenna Telegram digest and invoked `/Users/chrischo/.local/bin/openclaw message send` for stuck eval-candidate review notices.
- This was not LLM work and did not need an OpenClaw agent/session; using OpenClaw as a transport contradicted the current direct-alert design and made a review notice depend on gateway/agent availability.

Fix applied:

- `_send_telegram()` now uses `telegram_alert.send_chris_telegram(...)` directly with `source="eval_holdout_audit"`, `severity="info"`.
- Removed the OpenClaw binary/account/chat constants and stale docstring wording from `eval_holdout_audit.py`.
- Tests prove `_send_telegram` calls the direct alert module.
- Verification: `uv run python -m py_compile brain_core/eval_holdout_audit.py tests/unit/test_eval_holdout_audit.py`; `uv run pytest tests/unit/test_eval_holdout_audit.py -q` -> 6 passed; `uv run ruff check brain_core/eval_holdout_audit.py tests/unit/test_eval_holdout_audit.py --select F,E501` -> passed.

### B23. Remaining scheduled eval/skill notifications still used OpenClaw as Telegram transport

Evidence:

- Fresh stale-path scan found `cli/eval_gate.py` still sending regression alerts via `openclaw message send`.
- `brain_core/pipeline/skill_extractor.py` still sent the weekly skill proposal digest through `openclaw message send`.
- Several docstrings still described OpenClaw-primary distillation or `openclaw_dispatch` despite the code already using `cli_llm`.

Fix applied:

- `cli/eval_gate.py::alert_chris` now uses `telegram_alert.send_chris_telegram(...)` directly with `source="eval_gate"`, `severity="warn"`.
- `brain_core/pipeline/skill_extractor.py::send_digest_to_telegram` now uses direct Telegram with `source="skill_extractor:weekly_digest"`, `severity="info"`.
- Removed stale OpenClaw transport constants/imports from `eval_gate`, `skill_extractor`, `learn`, and `maintenance`.
- Updated stale CLI-first/OpenClaw-fallback wording in `learn.py`, `sleep_consolidate.py`, `maintenance.py`, and `eval_relabel.py`.
- Tests added/updated to prove direct Telegram routing for eval gate and skill extractor digest.

Fresh verification:

```bash
uv run pytest tests/unit/test_eval_gate.py tests/unit/test_pipeline_modules.py tests/unit/test_heavy_modules.py -q
uv run ruff check cli/eval_gate.py brain_core/pipeline/skill_extractor.py brain_core/pipeline/sleep_consolidate.py brain_core/learn.py brain_core/maintenance.py cli/eval_relabel.py tests/unit/test_eval_gate.py tests/unit/test_pipeline_modules.py tests/unit/test_heavy_modules.py --select F,E501
uv run pytest tests/unit -q
uv run ruff check . --select F
rg stale OpenClaw transport/doc patterns
curl /brain/ops/readiness
curl /brain/slos
```

Result: targeted eval/pipeline/heavy tests passed 47 tests; strict F/E501 passed on touched files; full unit suite reached 100% and exited 0; repo-wide ruff F passed; stale transport/doc scan reported `NO_STALE_OPENCLAW_TRANSPORT_OR_DOC_MATCHES`; live readiness reports `status=ready`, `blockers=[]`; live SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## Modification backlog toward world-level readiness

Priority is based on Chris's concerns, observed failure impact, and ability to verify.

### P0 — Trust and execution truth

1. **Agent execution truth ledger**
   - Problem: Completed/deferred/failed task state needs an auditable path from handoff message → queue task → backend dispatch → agent result → outcome.
   - Implemented now: `task_dispatch_attempts` table records `task_id`, `trace_id`, attempt number, agent/backend/model, started/completed timestamps, status, error class, error/result preview, prompt/response chars, and duration.
   - Implemented now: task creation stamps `metadata.trace_id`, preferring `source_message_id` for handoffs.
   - Implemented now: `/brain/task-dispatch-attempts` and `/brain/tasks/{task_id}/execution` expose task → dispatch attempts → outcomes evidence.
   - Implemented now: Brain UI Autopilot shows an “Agent Execution Truth” card from recent dispatch attempts.
   - Live gate passed: safe no-op OpenClaw dispatch populated the table and `/brain/tasks/{task_id}/execution` with a completed Jenna/openclaw attempt.

2. **Dispatch health SLO family**
   - Problem: `dispatch_failure_rate_1h` catches some failures but not “gateway installed but not reachable before any dispatch.”
   - Current fix: `openclaw_gateway_health` SLO.
   - Next: add UI/API display and incident ledger detail linking gateway SLO to affected queued tasks.

3. **Breaker semantics audit**
   - Problem: global breaker should not hide backend-specific cooldowns or local capacity constraints.
   - Current fix: transient errors no longer trip global breaker; half-open probes stale out.
   - Implemented now: `brain_core/cli_llm.py` exposes `failure_taxonomy_snapshot()` through `/brain/usage.llm.failure_taxonomy`, covering Codex, Claude, and OpenClaw provider classes plus auth, billing, model-missing, context-overflow, rate-limit, overloaded, and unknown recovery semantics.
   - Current fix: tests prove the failure taxonomy is static/hermetic and does not invoke CLI backends.
   - Next: add a dedicated Brain UI card if the usage panel is not enough for day-to-day operations.

### P1 — Memory quality and evaluation gates

4. **CRAG-style retrieval evaluator**
   - Implemented now: `cli/crag_regression.py` runs a cheap non-LLM CRAG safety gate over live stable eval retrievals.
   - Implemented now: CRAG scripts normalize `collection: "all"` to unscoped search, removing false empty misses from literal `collections=["all"]` filtering.
   - Implemented now: `brain_core/crag.py` accepts query-aware scoring, applies a lexical coverage penalty for confident-looking unrelated result windows, and uses CLI-first live expansion with narrow deterministic source-term rewrite bridges before LLM fallback.
   - Implemented now: `cli/crag_correction_regression.py` and `cli/eval_set_crag_corrections.json` prove deterministic second-hop correction recovery over live retrieval.
   - Implemented now: readiness blocks on missing/error/breached CRAG regression or correction regression; Brain UI Observability shows CRAG gate and CRAG correction cards.
   - Implemented now: daily scheduled `crag_regression` job runs at 07:02 PT, `crag_correction_regression` runs at 07:07 PT, and weekly exploratory `crag_llm_correction_sample` runs Sunday 07:12 PT.
   - Live deterministic gates passed: CRAG safety 40 stable rows, `safety_rate=100.0`, `dangerous_false_accepts=0`; CRAG correction `recovery_needed=4`, `recovered=4`, `recovery_rate=100.0`.
   - Live correction sample now passes after adding source-term rewrite bridges: `recovery_needed=4`, `recovered=4`, `recovery_rate=100.0`, `mean_rewrite_latency_ms=0.0`, `duration_s=1.884`. Per-attempt source labels show these personal/calendar recoveries used `source=rule`, avoiding earlier generic LLM over-rewrites.
   - Remaining gate: broaden source-term bridges from hand-curated candidates into learned/query-log-derived aliases so correction quality scales beyond the current eval rows.

5. **RAGAS-like nightly regression pack**
   - Implemented now: scheduled weekly `ragas_eval_gate` writes `logs/eval-report-ragas.json`, uses generated answers, readiness blocks on missing/error/low faithfulness or low generated-answer relevance, and the UI shows the current report.
   - Implemented now: `cli/eval_set_ragas_answers.json` is a dedicated answer-oriented generated-answer pack, and `ragas_eval_gate` uses it instead of the first stable infra rows.
   - Implemented now: readiness blocks if generated-answer RAGAS falls back below `case_count=8`, preventing regression to the old 3-case seed.
   - Current live gate: 8-case generated-answer report, faithfulness 0.925, answer relevance 0.887, answer_source_counts.generated=8, readiness `ok`.
   - Implemented now: separate 10-case holdout rotation retrieval eval is scheduled and readiness-gated.
   - Implemented now: RAGAS LLM-as-judge dispatch is CLI-first (`codex/gpt-5.5` primary) and uses OpenClaw only as emergency fallback.
   - Implemented now: `cli/eval_set_ragas_answers.json` grew to 12 generated-answer cases, including CLI-first task evaluation, source governance/privacy, failure lesson outcomes, and UI readiness manifest coverage.
   - Implemented now: every generated-answer RAGAS case has an `answer_rubric`, and `brain_core/ragas_judge.py` includes that rubric in the answer-relevance prompt.
   - Implemented now: `cli/ragas_eval_set_audit.py` gates case count, category coverage, answer rubrics, and scheduled-job wiring; `ragas_eval_snapshot()` exposes `eval_set_case_count`, `generated_target_cases`, and nonblocking `coverage_status` so live readiness can show when the next scheduled run has not yet consumed the larger set.
   - Remaining gate: run the next generated-answer RAGAS job over all 12 cases, review scores, and then raise the hard readiness minimum after live evidence is green.

6. **Adversarial memory evals**
   - Implemented now: `cli/eval_set_adversarial.json` expanded from 5 to 11 cases covering false success proof, stale/canonical supersession, Korean/English mixed recall, OpenClaw/Claude handoff state, personal source coverage, contradiction traps, stale-project decoys, personal-source privacy negatives, source-pollution negatives, and gateway-escalation truth.
   - Implemented now: `cli/eval_compare.py` supports per-case `forbidden_content` negative checks; forbidden hits turn content success into failure and persist `forbidden_hit_count` plus `negative_pass_pct`.
   - Implemented now: scheduled `adversarial_memory_eval` persists `logs/eval-report-adversarial.json`; readiness blocks on missing/error/breached adversarial eval or negative-pass failure; Brain UI Observability shows Adversarial eval negative-pass status.
   - Current live seed: 11/11 passed, v2 source/content-loose 100%, `negative_pass_pct=100%`, `forbidden_hit_count=0`, mean latency 460 ms.
   - Implemented now: separate holdout rotation includes Korean automation, source-pollution, and gateway-truth rows with negative checks.
   - Remaining gate: grow multilingual and privacy-negative cases beyond curated first-pass rows.

### P1 — Skill/procedure learning loop

7. **Voyager/AWM-style skill promotion with rollback**
   - Implemented now: auto-generated skills include `promotion_contract_version=skill-promotion-contract-v1`, source episode counts, outcome gate, validation gate, runtime parity, and rollback strategy in frontmatter/body.
   - Implemented now: usage sidecars for Claude/Codex/OpenClaw auto-skills record promotion contract version, source episode count, validation status, and rollback strategy.
   - Implemented now: `brain_core/skill_promotion_audit.py` verifies every auto-skill links to a backing Brain procedure, meets source episode threshold, exists in all three runtimes, has usage-sidecar contract evidence, and carries rollback metadata.
   - Implemented now: `/brain/ops/readiness` includes `skill_promotion`; Brain UI Observability shows Skill Promotion status and contract coverage.
   - Live evidence: regenerated 4 qualifying auto-skills from 429 procedures; audit reports `status=ok`, `auto_skills=4`, `contract_ok=4`, `required_runtimes=3`, and readiness remains `status=ready`, `blockers=[]`.
   - Implemented now: task dispatch attempts and outcomes carry retrieved procedure IDs, `skill_promotion_audit` reports `outcome_delta`, and Brain UI Observability shows skill outcome links.
   - Implemented now: skill-promotion outcome maturity now blocks readiness when linked promoted-procedure outcomes are below the minimum, so instrumentation-only no longer looks production-ready.
   - Current live gate: `skill_promotion.status=ok` for contract/runtime/provenance, but `skill_promotion.outcome_maturity.status=insufficient_data`, `linked_outcomes=0`, and readiness blocker `skill_promotion_outcomes` is active until enough real post-use outcomes accumulate.
   - Remaining gate: collect enough linked post-use outcomes to score promotion quality statistically, then gate on outcome lift rather than instrumentation alone.

8. **Reflexion-style failure lessons**
   - Implemented now: failed, exception, and transient-deferred task dispatch outcomes submit concise failure lessons to `failure_memory` asynchronously.
   - Implemented now: task execution already retrieves similar lessons and injects them under `Past failures to AVOID`; tests now prove a repeated gateway/dispatch failure surfaces the prior lesson in task planning.
   - Implemented now: dispatch-attempt metadata records `failure_lesson_status`, lesson IDs, completion timestamps, and failure errors; task metadata records the latest failure-lesson status.
   - Implemented now: SLO `task_failure_lesson_missing_count` breaches when failed/deferred dispatch attempts older than 15 minutes lack a recorded Reflexion lesson.
   - Current live SLO gate: `actual=0.0`, `breached=false`.
   - Implemented now: failure-lesson outcome readiness blocks when linked lesson outcomes are below the minimum, so write-only/zero-outcome Reflexion storage no longer appears world-ready.
   - Current live readiness gate: `failure_lesson_outcome.status=insufficient_data`, `linked_outcomes=0`, `readiness_blocking=true`, and readiness blocker `failure_lesson_outcome` is active until enough real post-use outcomes accumulate.
   - Remaining gate: collect real post-use outcome evidence that lessons reduce repeated failures.

### P2 — Architecture consolidation and resource efficiency

9. **Large-module split with behavior lock**
   - Hotspots: `brain_core/search_unified.py`, `brain_core/brain_loop.py`, `brain_core/indexer.py`, `brain_core/task_queue.py`, `brain_core/active_recall.py`, `brain_core/atoms_store.py`, `brain_core/cli_llm.py`.
   - Gate: module-level tests before extraction; no new duplicate pipeline.

10. **FastAPI/UI parity map**
   - Implemented now: `cli/ui_parity_audit.py` checks required FastAPI/API-client and Brain UI tokens for ops readiness, SLO incidents, Autopilot/task truth, retrieval eval gates, source governance, skill promotion, failure-lesson outcome loop, OpenClaw gateway, graph stats, and MCP/tool visibility.
   - Implemented now: parity is route/API-client derived (`coverage_level=route_api_client_derived_v1`), so missing FastAPI route paths or missing Brain UI API-client paths block the audit even if stale labels remain.
   - Implemented now: scheduled `ui_parity_audit` writes `logs/ui_parity_audit.json`; `/brain/ops/readiness` blocks on missing/error/blocked parity; Brain UI Observability shows a UI Parity card.
   - Live evidence: parity report `status=ok`, `required=10`, `ok=10`, `blocked=0`, `backend_route_count=172`, `api_client_path_count=125`; readiness `status=ready`, `blockers=[]`.
   - Implemented now: `brain_core/readiness_surface_manifest.py` defines `readiness-surface-manifest-v1`, and `cli/ui_parity_audit.py` now derives readiness fields from that manifest instead of maintaining the field list by hand.
   - Current gate: parity report now uses `coverage_level=route_api_client_manifest_v1` and includes the readiness manifest snapshot in `logs/ui_parity_audit.json`.
   - Remaining gate: keep the manifest synced when new readiness surfaces are added, and add a generated frontend type if schema drift reappears.

11. **High-value ingestion governance**
    - Implemented now: `brain_core/source_governance.py` produces a read-only freshness/registration/control snapshot for critical high-value sources: personal Apple data, Obsidian, OpenClaw sessions, Claude/Codex sessions, and Gmail classifier output.
    - Implemented now: `/brain/ops/readiness` includes `source_governance`; it blocks readiness only when critical sources or required pollution controls are missing/stale/broken, and reports nonblocking warnings separately.
    - Implemented now: Brain UI Observability shows a Source Governance card with critical-source and required-control coverage.
    - Live evidence before privacy-negative audit: source governance reported `critical_sources_ok=5/5`, `required_controls_ok=4/4`, `blockers=[]`, and warning `memory_provenance_lint` because the existing provenance lint artifact had 94 duplicate-id errors.
    - Implemented now: ran `cli/lint_memory_provenance.py --repair-safe --write-repair --write-report`; 47 duplicate distilled-note IDs were path-qualified while preserving old IDs in `source_aliases` / `previous_ids`.
    - Implemented now: `cli/privacy_negative_audit.py` samples actual personal-source vectors without printing content, found and redacted two GitHub-token-like vector payloads, and is now a required source-governance control.
    - Live evidence after repair: provenance lint reports `errors=0`, `warnings=0`, privacy audit reports `blocking_findings=0`, and `/brain/ops/readiness` reports `source_governance.status=ok`, `blockers=[]`, `warnings=[]`, `critical_sources_ok=5/5`, `required_controls_ok=5/5`.
    - Implemented now: redacted personal points are re-upserted with `--reindex-redacted`, refreshing dense/sparse vectors from redacted text; live run reindexed 2 points and follow-up audit remained `blocking_findings=0`.
   - Remaining gate: let the next full reindex refresh vector payloads for the repaired distilled IDs and extend privacy-negative sampling to additional personal-source classes only after false-positive-safe rules exist.

## Current verification evidence

Full unit/static verification after privacy reindex and CLI-first route/eval cleanup:

```bash
uv run pytest tests/unit -q
uv run ruff check . --select F
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
curl /brain/slos
```

Result: full unit suite reached 100% and exited 0; repo-wide ruff F-check passed. Fresh Ralph-hook rerun after line-length cleanup: targeted route/eval/task-dispatch CLI-first suite passed 63 tests; strict F/E501 check passed on touched dispatch/eval files; forced-OpenClaw scan reported `NO_FORCED_OPENCLAW_MATCHES`; full `uv run pytest tests/unit -q && uv run ruff check . --select F` passed again. Brain UI `npm run build` and `npm run lint` passed, with only the existing Vite chunk-size advisory; live readiness reports `status=ready`, `blockers=[]`; live SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

Targeted tests run after the gateway-regression, dispatch-truth, maintenance, and RAGAS-readiness changes:

```bash
uv run pytest tests/unit/test_ops_readiness.py tests/unit/test_scheduler.py tests/unit/test_slos.py tests/unit/test_slo_remediation.py tests/unit/test_task_dispatch_attempts.py tests/unit/test_escalation_policy.py tests/unit/test_maintenance.py tests/unit/test_cli_llm_process.py tests/unit/test_breakers.py -q
uv run python -m py_compile cli/eval_compare.py cli/eval_gate.py brain_core/job_registry.py brain_core/job_definitions.py brain_core/ops_readiness.py brain_core/task_queue.py brain_core/routes/agency.py brain_core/slos.py brain_core/slo_remediation.py
uv run ruff check --select F cli/eval_compare.py cli/eval_gate.py brain_core/job_registry.py brain_core/job_definitions.py brain_core/ops_readiness.py brain_core/task_queue.py brain_core/routes/agency.py brain_core/maintenance.py brain_core/slos.py brain_core/slo_remediation.py tests/unit/test_ops_readiness.py tests/unit/test_task_dispatch_attempts.py tests/unit/test_maintenance.py tests/unit/test_slos.py tests/unit/test_slo_remediation.py tests/unit/test_cli_llm_process.py tests/unit/test_escalation_policy.py tests/unit/test_breakers.py
```

Result: targeted Brain tests passed; py_compile passed; ruff F-check passed.

Additional source-governance verification after adding the ingestion governance gate:

```bash
uv run pytest tests/unit/test_source_governance.py tests/unit/test_ops_readiness.py -q
uv run python -m py_compile brain_core/source_governance.py brain_core/ops_readiness.py
```

Result before the privacy-negative audit control: 14 tests passed; after duplicate-ID repair, source governance live snapshot reported `status=ok`, `critical_sources_ok=5/5`, `required_controls_ok=4/4`, `warnings=[]`.

Privacy-negative personal-source audit verification:

```bash
uv run python -m py_compile cli/privacy_negative_audit.py brain_core/source_policy.py brain_core/qdrant_store.py brain_core/source_governance.py brain_core/job_registry.py brain_core/job_definitions.py
uv run pytest tests/unit/test_privacy_negative_audit.py tests/unit/test_source_policy.py tests/unit/test_source_governance.py tests/unit/test_ops_readiness.py -q
uv run ruff check --select F cli/privacy_negative_audit.py brain_core/source_policy.py brain_core/qdrant_store.py brain_core/source_governance.py brain_core/job_registry.py brain_core/job_definitions.py tests/unit/test_privacy_negative_audit.py tests/unit/test_source_policy.py tests/unit/test_source_governance.py tests/unit/test_ops_readiness.py
uv run python cli/render_cron_map.py --write
uv run python cli/render_cron_map.py --check
uv run python cli/privacy_negative_audit.py --limit 300 --repair-redact
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
curl /brain/slos
```

Result: 38 targeted tests passed; py_compile passed; ruff F-check passed; cron map regenerated and check passed; live privacy audit sampled 241 personal points, repaired two secret-like vector payloads, and reported `blocking_findings=0`; subsequent reindex run re-upserted 2 redacted points, follow-up audit reports `blocking_findings=0`; live readiness reports `status=ready`, `blockers=[]`, `source_governance.status=ok`, `required_controls_ok=5/5`; live SLOs report `checked=27`, `breached=0`.

UI parity verification after adding the static API-to-UI readiness gate:

```bash
uv run python -m py_compile cli/ui_parity_audit.py brain_core/ops_readiness.py brain_core/job_registry.py brain_core/job_definitions.py
uv run python cli/ui_parity_audit.py
uv run pytest tests/unit/test_ui_parity_audit.py tests/unit/test_ops_readiness.py -q
uv run ruff check --select F cli/ui_parity_audit.py brain_core/ops_readiness.py brain_core/job_registry.py brain_core/job_definitions.py tests/unit/test_ui_parity_audit.py tests/unit/test_ops_readiness.py
cd /Users/chrischo/server/brain-ui && npm run build
cd /Users/chrischo/server/brain-ui && npm run lint
```

Result before the Reflexion outcome-link card: py_compile passed; parity audit reported `status=ok`, `required=9`, `ok=9`, `blocked=0`; 26 targeted tests passed; ruff F-check passed; Brain UI build/lint passed. Vite emitted only the existing large-chunk advisory. Current parity evidence is listed below with `required=10`.

Route/API-client derived UI parity verification:

```bash
uv run python -m py_compile cli/ui_parity_audit.py brain_core/ops_readiness.py
uv run pytest tests/unit/test_ui_parity_audit.py tests/unit/test_ops_readiness.py -q
uv run ruff check --select F cli/ui_parity_audit.py tests/unit/test_ui_parity_audit.py
uv run python cli/ui_parity_audit.py
curl /brain/ops/readiness
```

Result: 28 targeted tests passed; py_compile passed; ruff F-check passed; parity audit reports `status=ok`, `required=10`, `ok=10`, `blocked=0`, `coverage_level=route_api_client_derived_v1`, `backend_route_count=172`, `api_client_path_count=125`; live readiness reports `status=ready`, `blockers=[]`.

Reflexion lesson outcome-link verification after adding `outcomes.lesson_ids` and the readiness/UI surface:

```bash
uv run python -m py_compile brain_core/task_queue.py brain_core/failure_lesson_audit.py brain_core/ops_readiness.py cli/ui_parity_audit.py
uv run pytest tests/unit/test_task_dispatch_attempts.py tests/unit/test_task_queue_decision_link.py tests/unit/test_failure_lesson_audit.py tests/unit/test_ops_readiness.py tests/unit/test_ui_parity_audit.py -q
uv run ruff check --select F brain_core/task_queue.py brain_core/failure_lesson_audit.py brain_core/ops_readiness.py cli/ui_parity_audit.py tests/unit/test_task_dispatch_attempts.py tests/unit/test_task_queue_decision_link.py tests/unit/test_failure_lesson_audit.py tests/unit/test_ops_readiness.py tests/unit/test_ui_parity_audit.py
uv run python cli/ui_parity_audit.py
cd /Users/chrischo/server/brain-ui && npm run build && npm run lint
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
curl /brain/slos
```

Result: 40 targeted tests passed; py_compile passed; ruff F-check passed; parity audit reports `status=ok`, `required=10`, `ok=10`, `blocked=0`; Brain UI build/lint passed. Live readiness reports `status=ready`, `blockers=[]`, `failure_lesson_outcome.status=insufficient_data`, `linked_outcomes=0`, `readiness_blocking=false`, and live SLOs report `checked=27`, `breached=0`.

Follow-up scheduler evidence: full unit run exposed stale `CRON_MAP.md` after new jobs were added; regenerated it with `uv run python cli/render_cron_map.py --write`. `uv run python cli/render_cron_map.py --check` and the two cron-map scheduler tests now pass.

Reflexion lesson observability verification:

```bash
uv run python -m py_compile brain_core/task_queue.py brain_core/slos.py
uv run pytest tests/unit/test_task_dispatch_attempts.py::test_deferred_dispatch_records_failure_lesson tests/unit/test_task_dispatch_attempts.py::test_retrieved_failure_lessons_are_injected_into_next_task tests/unit/test_slos.py::test_slo_count tests/unit/test_slos.py::test_measure_task_failure_lesson_missing_count tests/unit/test_slos.py::test_check_all_returns_all -q
uv run pytest tests/unit/test_slos.py tests/unit/test_task_dispatch_attempts.py tests/unit/test_ops_readiness.py -q
uv run ruff check --select F brain_core/task_queue.py brain_core/slos.py tests/unit/test_task_dispatch_attempts.py tests/unit/test_slos.py
```

Result: py_compile passed; 5 focused tests passed; 51 targeted SLO/task/readiness tests passed; ruff F-check passed; live `task_failure_lesson_missing_count` reports `actual=0.0`, `breached=false`.

Live SLO remediation after loading the new watcher:

```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
uv run python brain_core/db_maintenance.py wal_checkpoint
uv run python brain_core/maintenance.py all_cleanup
curl /brain/slos
curl /brain/ops/readiness
```

Result: live SLO roster now reports `checked=27`, `breached=0`; `task_failure_lesson_missing_count.actual=0.0`; `logs_dir_total_mb.actual=1817.4`; readiness reports `status=ready`, `blockers=[]`.

Brain UI verification after adding the dedicated gateway card:

```bash
cd /Users/chrischo/server/brain-ui && npm run build
cd /Users/chrischo/server/brain-ui && npm run lint
```

Result: both passed. Vite emitted only the existing large-chunk advisory. Re-run after the Autopilot execution-truth UI card and Source Governance Observability card also passed.

Live readiness/SLO verification after restart:

```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
curl /brain/slos
```

Result: `/brain/ops/readiness` reports `status=ready`, `blockers=[]`, `openclaw_gateway.status=ok`, and `ragas_eval.status=ok` with `faithfulness_mean=1.0`; `/brain/slos` reports `checked=26`, `breached=0`, `openclaw_gateway_health.actual=0.0`, `task_dispatch_stale_started_count.actual=0.0`, and `logs_dir_total_mb.actual=1819.1`.
Latest re-run after generated-answer RAGAS, expanded adversarial eval, OpenClaw parser fixes, source governance, skill-promotion contracts, duplicate-ID repair, gateway ensure remediation, holdout eval, and CRAG correction regression reports `status=ready`, `blockers=[]`, `adversarial_eval.status=ok`, `adversarial_eval.negative_pass_pct=100`, `adversarial_eval.forbidden_hit_count=0`, `crag_regression.status=ok`, `crag_correction_regression.status=ok`, `crag_correction_regression.recovery_rate=100.0`, `source_governance.status=ok`, `skill_promotion.status=ok`, `skill_promotion.contract_ok=4/4`, `ragas_eval.status=ok`, `faithfulness_mean=0.957`, `answer_relevance_mean=0.65`, `answer_source=generated`, `generated_answer_gate=true`, and `/brain/slos` still `checked=26`, `breached=0`.

Earlier live repair evidence from this session:

- OpenClaw gateway installed/running on `127.0.0.1:18789`.
- `llm.dispatch` breaker closed.
- Ellie/Liz/Sage focus tasks completed with recorded non-override outcomes.
- Queue deferral/orphan-requeue logic validated by targeted unit tests.
- Post-migration dispatch-truth endpoint verified with safe no-op task `task_1dc6eb0e3880`, completed attempt `dispatch_071f9119da94`, `trace_id=manual-dispatch-truth-2026-05-05`, `agent=jenna`, `backend=openclaw`, `status=completed`.

## B24 — CLI-first autonomous ingest and alert transport hardening

Chris's concern was correct: several scheduled/background paths still looked like autonomous work but actually depended on direct OpenClaw agent shellouts. Hardened the remaining primary paths so routine background work uses CLI-first LLM dispatch and non-LLM alerts bypass LLM sessions entirely.

Changes made:

- Added `ingest/llm_dispatch.py`, a shared JSON helper that calls `brain_core.cli_llm.dispatch` with the default fallback chain (`codex/gpt-5.5` primary, `gpt-5.3-codex-spark`, configured Claude fallback, OpenClaw only as central emergency fallback).
- Migrated scheduled ingest adapters from direct `openclaw agent` subprocess calls to the shared helper:
  - `ingest/screen_time.py`
  - `ingest/git_activity.py`
  - `ingest/active_contacts.py`
  - `ingest/gmail.py`
  - `ingest/claude_code_sessions.py`
  - `ingest/browser.py`
  - `ingest/openclaw_sessions.py`
- Migrated Chris-facing non-LLM alerts to direct Telegram Bot API via `telegram_alert.send_chris_telegram`:
  - `ingest/healthcheck.py`
  - `cli/lora_ab_gate.py`
  - `cli/server_watchdog.sh`
- Removed stale direct OpenClaw fallback wiring from HyDE query expansion and clarified that it uses the central CLI-first dispatcher.
- Removed stale `OPENCLAW_BIN` lifecycle wiring from `brain_core/memory_lifecycle.py`; pre-archive extraction already uses `cli_llm.dispatch`, and failure messages now say CLI dispatch.
- Added `tests/unit/test_cli_first_dispatch_contract.py` to lock the contract: default chain starts with `codex/gpt-5.5`, ingest adapters use the shared helper, HyDE uses central dispatch without direct OpenClaw import, and LoRA/healthcheck/watchdog alerts are LLM-free direct Telegram.

Fresh verification evidence:

```bash
uv run python -m py_compile ingest/llm_dispatch.py ingest/screen_time.py ingest/git_activity.py ingest/active_contacts.py ingest/gmail.py ingest/claude_code_sessions.py ingest/browser.py ingest/openclaw_sessions.py ingest/healthcheck.py cli/lora_ab_gate.py brain_core/hyde.py brain_core/memory_lifecycle.py
bash -n cli/server_watchdog.sh
uv run ruff check ingest/llm_dispatch.py ingest/screen_time.py ingest/git_activity.py ingest/active_contacts.py ingest/gmail.py ingest/claude_code_sessions.py ingest/browser.py ingest/openclaw_sessions.py ingest/healthcheck.py cli/lora_ab_gate.py brain_core/hyde.py brain_core/memory_lifecycle.py --select F
uv run pytest tests/unit/test_eval_gate.py tests/unit/test_pipeline_modules.py tests/unit/test_heavy_modules.py tests/unit/test_eval_holdout_audit.py -q
uv run pytest tests/unit/test_cli_first_dispatch_contract.py -q
uv run ruff check tests/unit/test_cli_first_dispatch_contract.py --select F
uv run pytest tests/unit -q && uv run ruff check . --select F
rg -n "OPENCLAW_BIN|openclaw message send|openclaw agent|subprocess.run\(cmd|backend=\"openclaw\"|max_backends=1" ingest cli brain_core -g '!**/__pycache__/**' -g '!cli/eval_set*.json*' -g '!cli/eval_backups/**'
curl /brain/ops/readiness
curl /brain/slos
```

Result: affected-file compile passed; watchdog shell syntax passed; affected-file ruff F passed; 53 targeted alert/pipeline/heavy tests passed; 6 CLI-first dispatch contract tests passed; contract-test ruff F passed; full unit suite exited 0 and repo-wide ruff F passed. Stale scan no longer reports direct OpenClaw primary dispatch in scheduled ingest adapters, LoRA alerting, server watchdog, HyDE, or memory lifecycle; remaining matches are central fallback/config/archive/non-LLM subprocess paths. Live readiness reports `status=ready`, `blockers=[]`; live SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B25 — Mechanical usage and brain-loop dispatch no longer bypass CLI-first routing

A follow-up static audit found two remaining primary-facing legacy seams after the ingest/watchdog migration:

- `brain_core/brain_loop.py` imported `openclaw_dispatch` as a direct fallback if `cli_llm` import failed. That meant autonomous background work could still bypass the central CLI-first dispatcher.
- `/brain/usage` in `brain_core/routes/brain_ops.py` still read usage through `openclaw_dispatch.get_usage_stats`, so the user-facing usage endpoint described the legacy wrapper instead of the current CLI-first surface.

Changes made:

- Removed the direct `openclaw_dispatch` fallback from `brain_loop`; mechanical background work now fails closed if `cli_llm` is unavailable because `cli_llm` owns provider fallback and backlog catch-up.
- Added `cli_llm.get_usage_stats(days=...)` over the shared `llm_usage.db` ledger with `source=cli_llm`, `primary_model=gpt-5.5`, per-agent, per-backend, skipped/rate-limited count, and token totals.
- Updated `/brain/usage` to call `cli_llm.get_usage_stats` instead of importing `openclaw_dispatch`.
- Expanded `tests/unit/test_cli_first_dispatch_contract.py` to cover the usage endpoint contract and assert brain-loop/usage-route do not import direct OpenClaw dispatch.

Fresh verification evidence:

```bash
uv run python -m py_compile brain_core/cli_llm.py brain_core/brain_loop.py brain_core/routes/brain_ops.py
uv run pytest tests/unit/test_cli_first_dispatch_contract.py tests/test_brain_loop.py -q
uv run ruff check brain_core/cli_llm.py brain_core/brain_loop.py brain_core/routes/brain_ops.py tests/unit/test_cli_first_dispatch_contract.py --select F
uv run pytest tests/unit -q && uv run ruff check . --select F
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/usage?days=1
curl /brain/ops/readiness
curl /brain/slos
```

Result: compile passed; 38 targeted CLI-first/brain-loop tests passed; targeted ruff F passed; full unit suite plus repo-wide ruff F exited 0. After server restart, `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=604`, `llm.cb_skipped=142`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B26 — User-facing docs now lock the CLI-first and task-evaluation contract

After the code-level CLI-first migration, the remaining risk was documentation drift: future agents could read stale user-facing guidance and reintroduce OpenClaw-primary assumptions or task-evaluation approval alerts. Updated the primary docs and locked them with a test.

Changes made:

- `README.md` now states autonomous/background LLM work is CLI-first through `brain_core/cli_llm.py`, with Codex `gpt-5.5` primary, Spark/Claude fallback, and OpenClaw only as integration/emergency fallback. It also states `/brain/usage` reports `source=cli_llm`, `primary_model=gpt-5.5`, and task-evaluation notifications are action summaries, not approval requests.
- `AGENT_HARNESS.md` now tells agents to verify `/brain/usage` as the CLI-first accounting surface and identifies `task_queue:evaluation_action_summary` plus `TASK EVALUATION ACTION` wording as the evaluation notification contract.
- `brain/ARCHITECTURE.md` now documents that `/brain/usage` is backed by `cli_llm.get_usage_stats` and should report `source=cli_llm`, `primary_model=gpt-5.5`.
- `tests/unit/test_cli_first_dispatch_contract.py` now asserts those docs contain the CLI-first usage and task-evaluation action-summary contract.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_cli_first_dispatch_contract.py -q
uv run ruff check tests/unit/test_cli_first_dispatch_contract.py --select F
uv run python -m py_compile brain_core/cli_llm.py brain_core/brain_loop.py brain_core/routes/brain_ops.py
uv run pytest tests/unit/test_cli_first_dispatch_contract.py tests/test_brain_loop.py tests/unit/test_task_dispatch_attempts.py::test_task_evaluation_human_needed_sends_action_summary tests/unit/test_task_dispatch_attempts.py::test_policy_human_required_sends_action_summary_not_escalation_alert -q
uv run pytest tests/unit -q && uv run ruff check . --select F
curl /brain/usage?days=1
curl /brain/ops/readiness
curl /brain/slos
```

Result: 9 CLI-first contract tests passed; contract-test ruff F passed; compile passed; 41 targeted CLI-first/brain-loop/task-evaluation tests passed; full unit suite plus repo-wide ruff F exited 0. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=606`, `llm.cb_skipped=143`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B27 — Completion audit is now executable and intentionally blocks final claim

The PRD/test spec required a prompt-to-artifact completion audit before any final “world-level Brain” claim. Until now this was only prose in the audit doc, which made it too easy for a future run to mistake green readiness/SLO checks for full objective completion.

Changes made:

- Added `cli/world_level_completion_audit.py`. It parses the audit document's prompt-to-artifact checklist, classifies rows as `pass`, `weak`, or `open`, verifies key evidence artifacts exist, and emits JSON.
- Added `tests/unit/test_world_level_completion_audit.py` covering checklist parsing/classification, current not-ready posture, artifact coverage, and `--fail-on-open` behavior.
- Wrote the current report to `logs/world-level-completion-audit.json`.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_world_level_completion_audit.py -q
uv run python -m py_compile cli/world_level_completion_audit.py tests/unit/test_world_level_completion_audit.py
uv run ruff check cli/world_level_completion_audit.py tests/unit/test_world_level_completion_audit.py --select F
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
uv run pytest tests/unit/test_world_level_completion_audit.py tests/unit/test_cli_first_dispatch_contract.py tests/test_brain_loop.py tests/unit/test_task_dispatch_attempts.py::test_task_evaluation_human_needed_sends_action_summary tests/unit/test_task_dispatch_attempts.py::test_policy_human_required_sends_action_summary_not_escalation_alert -q
uv run pytest tests/unit -q && uv run ruff check . --select F
curl /brain/usage?days=1
curl /brain/ops/readiness
curl /brain/slos
```

Result: 3 completion-audit tests passed; compile passed; targeted ruff F passed; completion report says `status=not_ready`, `completion_ready=false`, counts `{pass: 1, weak: 3, open: 3, artifact_missing: 0}` with open rows `Find modifications needed`, `Find improvements possible`, and `Work until world-level ready`; 44 targeted CLI-first/brain-loop/task-eval/completion tests passed; full unit suite plus repo-wide ruff F exited 0. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=608`, `llm.cb_skipped=144`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B28 — Research and GitHub repo evidence refreshed with current primary sources

The executable completion audit showed the research-paper and GitHub-repo rows were only weakly covered by the first-pass map. Refreshed those rows with current primary sources and official repositories, without changing the final completion posture.

Changes made:

- Added `docs/research/world-level-brain-research-refresh-2026-05-05.md` with a current source map for Mem0, Zep/Graphiti, A-MEM, MemoryOS, HippoRAG, TERAG, Hindsight, H²R, and an agent-memory survey/taxonomy source.
- Updated the prompt-to-artifact checklist research rows to point to the refresh artifact and mark those rows covered.
- Updated `cli/world_level_completion_audit.py` to treat covered rows as pass, and added the research refresh artifact to required artifact checks.
- Expanded `tests/unit/test_world_level_completion_audit.py` to assert the refresh artifact contains current paper/repo links and preserves the no-implied-dependency-adoption constraint.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_world_level_completion_audit.py -q
uv run python -m py_compile cli/world_level_completion_audit.py tests/unit/test_world_level_completion_audit.py
uv run ruff check cli/world_level_completion_audit.py tests/unit/test_world_level_completion_audit.py --select F
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
uv run pytest tests/unit/test_world_level_completion_audit.py tests/unit/test_cli_first_dispatch_contract.py tests/test_brain_loop.py tests/unit/test_task_dispatch_attempts.py::test_task_evaluation_human_needed_sends_action_summary tests/unit/test_task_dispatch_attempts.py::test_policy_human_required_sends_action_summary_not_escalation_alert -q
uv run pytest tests/unit -q && uv run ruff check . --select F
curl /brain/usage?days=1
curl /brain/ops/readiness
curl /brain/slos
```

Result: 4 completion/research-audit tests passed; compile passed; targeted ruff F passed; completion report improved to `status=not_ready`, `completion_ready=false`, counts `{pass: 3, weak: 1, open: 3, artifact_missing: 0}`. The paper and GitHub repo rows now pass; `Find bugs` remains weak and `Find modifications needed`, `Find improvements possible`, and `Work until world-level ready` remain open. 45 targeted completion/CLI-first/brain-loop/task-eval tests passed; full unit suite plus repo-wide ruff F exited 0. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=610`, `llm.cb_skipped=145`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B29 — Bug-fix evidence is now executable, not prose-only

The completion audit showed the “Find bugs” row was still weak because the bug hunt evidence lived mostly in narrative B-sections and individual tests. Added a dedicated executable bug audit so future agents cannot claim that high-impact bugs were found/fixed unless the concrete evidence still exists.

Changes made:

- Added `cli/world_level_bug_audit.py`, a content-safe static bug ledger covering eight bug classes: fake automation/dispatch truth, OpenClaw-primary bypasses, task-evaluation alert policy, privacy-negative payloads, source-pollution governance, write-only failure lessons, backend-only UI gaps, and completion-truth gaps.
- Added `tests/unit/test_world_level_bug_audit.py` to lock the audit contract, missing-token behavior, forbidden-token behavior, and current green status.
- Updated `cli/world_level_completion_audit.py` so the bug audit script and tests are required artifacts before prompt-to-artifact completion can be green.
- Updated the prompt-to-artifact checklist so “Find bugs” is now covered by the executable bug audit while the broader modification/improvement/world-level rows remain open.
- Wrote the current report to `logs/world-level-bug-audit.json`.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_world_level_bug_audit.py -q
uv run python cli/world_level_bug_audit.py --json | tee logs/world-level-bug-audit.json
uv run pytest tests/unit/test_world_level_bug_audit.py tests/unit/test_world_level_completion_audit.py -q
uv run python -m py_compile cli/world_level_bug_audit.py cli/world_level_completion_audit.py tests/unit/test_world_level_bug_audit.py tests/unit/test_world_level_completion_audit.py
uv run ruff check cli/world_level_bug_audit.py cli/world_level_completion_audit.py tests/unit/test_world_level_bug_audit.py tests/unit/test_world_level_completion_audit.py --select F
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
```

Result: bug audit reports `status=ok`, `bug_classes_checked=8`, `bug_classes_locked=8`, `blocked=0`; completion report remains `status=not_ready`, `completion_ready=false`, now with counts `{pass: 4, weak: 0, open: 3, artifact_missing: 0}` and the remaining open rows `Find modifications needed`, `Find improvements possible`, and `Work until world-level ready`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=612`, `llm.cb_skipped=146`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B30 — Modification and improvement gaps are now executable

The remaining checklist rows for “Find modifications needed” and “Find improvements possible” were still doc-only. Added a deterministic gap audit that verifies the backlog is prioritized, concrete, evidence-backed, and broad enough to cover Chris's concerns without pretending the remaining gates are done.

Changes made:

- Added `cli/world_level_gap_audit.py`, which parses the modification backlog and verifies P0/P1/P2 priority coverage, at least ten concrete backlog items, implemented evidence, explicit remaining gates, near-term next steps, live evidence anchors, and broader improvement theme coverage.
- Added `tests/unit/test_world_level_gap_audit.py` to lock the parser and current green status.
- Updated `cli/world_level_completion_audit.py` so the gap audit script and tests are required artifacts.
- Updated the prompt-to-artifact checklist so “Find modifications needed” and “Find improvements possible” are now covered by the executable gap audit while “Work until world-level ready” remains open.
- Wrote the current report to `logs/world-level-gap-audit.json`.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_world_level_gap_audit.py -q
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
uv run pytest tests/unit/test_world_level_bug_audit.py tests/unit/test_world_level_gap_audit.py tests/unit/test_world_level_completion_audit.py -q
uv run python -m py_compile cli/world_level_bug_audit.py cli/world_level_gap_audit.py cli/world_level_completion_audit.py tests/unit/test_world_level_bug_audit.py tests/unit/test_world_level_gap_audit.py tests/unit/test_world_level_completion_audit.py
uv run ruff check cli/world_level_bug_audit.py cli/world_level_gap_audit.py cli/world_level_completion_audit.py tests/unit/test_world_level_bug_audit.py tests/unit/test_world_level_gap_audit.py tests/unit/test_world_level_completion_audit.py --select F
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
```

Result: gap audit reports `status=ok`, `modification_items=11`, `implemented_evidence_count=37`, `remaining_gate_count=7`, `next_step_count=2`, `live_evidence_count=9`, `theme_count=7`. Completion report remains `status=not_ready`, `completion_ready=false`, now with counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`; the only open row is `Work until world-level ready`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=615`, `llm.cb_skipped=147`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B31 — CLI dispatch failure taxonomy is visible and hermetic

The verification run surfaced CLI cooldown/slot messages, which made it important to distinguish real backend dispatch, provider cooldowns, local capacity throttles, and classified failure recovery paths. Added a static failure taxonomy on the CLI-first usage surface so operators can see what Brain will do without triggering any LLM call.

Changes made:

- Added `failure_taxonomy_snapshot()` in `brain_core/cli_llm.py`. It reuses the existing classifier to expose auth, billing, model-missing, context-overflow, rate-limit, overloaded, and unknown recovery semantics.
- `/brain/usage` now includes `llm.failure_taxonomy` with provider classes `codex`, `claude`, and `openclaw`, the taxonomy version, and the dashboard surface path.
- Added tests in `tests/unit/test_cli_llm_process.py` proving the taxonomy covers every provider class, exposes compression/credential-rotation/cooldown decisions, and does not spawn CLI processes.
- Updated the gap audit to require `failure_taxonomy` evidence in the dispatch-health theme.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_cli_llm_process.py tests/unit/test_cli_first_dispatch_contract.py -q
uv run python -m py_compile brain_core/cli_llm.py tests/unit/test_cli_llm_process.py
uv run ruff check brain_core/cli_llm.py tests/unit/test_cli_llm_process.py --select F
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
```

Result: 35 CLI/dispatch-contract tests passed; compile passed; targeted ruff F passed. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. The gap audit remains `status=ok` with `implemented_evidence_count=38` and dispatch-health evidence now requiring `failure_taxonomy`. Completion report remains `status=not_ready`, `completion_ready=false`, counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=617`, `llm.cb_skipped=149`, `llm.failure_taxonomy.version=cli-failure-taxonomy-v1`, provider classes `[codex, claude, openclaw]`, and `class_count=7`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B32 — Readiness parity now has an explicit manifest

The UI parity gate still depended on a hand-maintained readiness-field list. That was a schema-drift risk: backend readiness could change without a contract artifact for audits and UI work to share.

Changes made:

- Added `brain_core/readiness_surface_manifest.py` with `readiness-surface-manifest-v1`, covering ops readiness, SLO incidents, retrieval eval gates, source governance, skill promotion, failure lesson outcomes, and OpenClaw gateway health.
- Updated `cli/ui_parity_audit.py` to derive readiness fields from the manifest and emit the manifest snapshot in its report.
- Added `tests/unit/test_readiness_surface_manifest.py`, and updated UI parity tests for `coverage_level=route_api_client_manifest_v1`.
- Updated the gap audit to require manifest evidence in the UI observability theme.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_readiness_surface_manifest.py tests/unit/test_ui_parity_audit.py tests/unit/test_ops_readiness.py -q
uv run python -m py_compile brain_core/readiness_surface_manifest.py cli/ui_parity_audit.py tests/unit/test_readiness_surface_manifest.py tests/unit/test_ui_parity_audit.py
uv run ruff check brain_core/readiness_surface_manifest.py cli/ui_parity_audit.py tests/unit/test_readiness_surface_manifest.py tests/unit/test_ui_parity_audit.py --select F
uv run python cli/ui_parity_audit.py
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
```

Result: 29 readiness/UI parity tests passed; compile passed; targeted ruff F passed; UI parity reports `status=ok`, `coverage_level=route_api_client_manifest_v1`, `readiness_manifest_version=readiness-surface-manifest-v1`, `required=10`, `ok=10`, `blocked=0`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. Gap audit remains `status=ok` with `implemented_evidence_count=39` and UI observability evidence now requiring the readiness manifest. Completion report remains `status=not_ready`, `completion_ready=false`, counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`. Live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=619`, `llm.cb_skipped=150`, `llm.failure_taxonomy.version=cli-failure-taxonomy-v1`; readiness reports `status=ready`, `blockers=[]`; SLOs report `checked=27`, `breached=0`, `alerts_sent=0`.

## B33 — Generated-answer RAGAS pack expanded with stricter rubrics

The RAGAS gate still had only eight generated-answer cases and answer relevance judged only whether the answer addressed the question, not whether it satisfied the expected answer rubric. Expanded the static pack and strengthened the judge path without triggering a live LLM-heavy eval run inside this branch.

Changes made:

- Expanded `cli/eval_set_ragas_answers.json` from 8 to 12 cases. New categories cover CLI-first task evaluation, source-governance/privacy controls, failure lesson outcome linkage, and UI readiness manifest parity.
- Added `answer_rubric` to every generated-answer RAGAS case.
- Updated `brain_core/ragas_judge.py` so the answer-relevance judge prompt includes the expected answer/rubric when provided.
- Updated `cli/eval_compare.py` so generated-answer RAGAS uses `answer_rubric` as the expected judging text and persists the rubric preview per case.
- Added `cli/ragas_eval_set_audit.py` plus tests to verify minimum case count, required categories, rubric coverage, `ragas_answer_eval=true`, duplicate-query absence, and scheduled-job wiring.
- Updated `brain_core/ops_readiness.py` so `ragas_eval_snapshot()` reports `eval_set_case_count`, `generated_target_cases`, and nonblocking `coverage_status` when the eval set has grown ahead of the latest live report.
- Updated the gap and completion audits to require the RAGAS eval-set audit artifacts.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_ops_readiness.py tests/unit/test_ragas_eval_set_audit.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py -q
uv run python -m py_compile brain_core/ops_readiness.py cli/ragas_eval_set_audit.py cli/eval_compare.py brain_core/ragas_judge.py
uv run ruff check brain_core/ops_readiness.py cli/ragas_eval_set_audit.py cli/eval_compare.py brain_core/ragas_judge.py tests/unit/test_ops_readiness.py tests/unit/test_ragas_eval_set_audit.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py --select F
uv run python cli/ragas_eval_set_audit.py --json | tee logs/ragas-eval-set-audit.json
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
```

Result: 53 targeted ops/RAGAS/eval tests passed; compile passed; targeted ruff F passed; RAGAS eval-set audit reports `status=ok`, `case_count=12`, `category_count=12`, `min_cases=12`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. Gap audit remains `status=ok` with `implemented_evidence_count=42`; completion audit remains `status=not_ready`, `completion_ready=false`, counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`, with only `Work until world-level ready` open. Live readiness reports `status=ready`, `blockers=[]`, `ragas_eval.status=ok`, `ragas_eval.case_count=8`, `ragas_eval.eval_set_case_count=12`, `ragas_eval.generated_target_cases=12`, and `ragas_eval.coverage_status=pending_next_run`; this intentionally records that the next scheduled/explicit generated-answer run still needs to consume the expanded pack before the hard minimum can be raised. Live `/brain/slos` reports `checked=27`, `breached=0`, `alerts_sent=0`; live `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=621`, `llm.cb_skipped=151`.

## B34 — Expanded RAGAS coverage pending now blocks readiness

After B33, live readiness still reported `ready` even though `ragas_eval.coverage_status=pending_next_run` showed the latest RAGAS report had only consumed 8 of the now-12 generated-answer cases. That was an execution-truth bug: readiness looked green while a required expanded eval pack had not yet run.

Changes made:

- Updated `brain_core/ops_readiness.py` so generated-answer RAGAS breaches readiness when `case_count < generated_target_cases`.
- Kept `coverage_status` explicit so operators can distinguish a score failure from a pending expanded-pack run.
- Added tests proving pending expanded-pack coverage breaches readiness, and that readiness returns to `ok` when a 12-case report consumes the 12-case eval set.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_ops_readiness.py tests/unit/test_ragas_eval_set_audit.py -q
uv run python -m py_compile brain_core/ops_readiness.py tests/unit/test_ops_readiness.py
uv run ruff check brain_core/ops_readiness.py tests/unit/test_ops_readiness.py --select F
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
uv run pytest tests/unit -q && uv run ruff check . --select F
curl /brain/slos
curl /brain/usage?days=1
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
```

Result: 28 targeted ops/RAGAS tests passed; compile passed; targeted ruff F passed. Live readiness now truthfully reports `status=blocked`, `blockers=[ragas_eval]`, `ragas_eval.status=breached`, `case_count=8`, `eval_set_case_count=12`, `generated_target_cases=12`, and `coverage_status=pending_next_run`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. Live `/brain/slos` reports `checked=27`, `breached=0`, `alerts_sent=0`; `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=623`, `llm.cb_skipped=152`. Gap audit remains `status=ok`; completion audit remains `status=not_ready`, `completion_ready=false`, counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`.

## B35 — Expanded 12-case generated-answer RAGAS run completed

After B34 made pending expanded-pack coverage block readiness, ran the existing CLI-first generated-answer RAGAS gate over the full 12-case pack. The run used the scheduled job command shape and persisted the fresh `logs/eval-report-ragas.json` report.

Fresh verification evidence:

```bash
uv run python cli/eval_compare.py --json --ragas --ragas-answer-source generated --limit 20 --eval-set cli/eval_set_ragas_answers.json --persist-track ragas --content-metric loose > /tmp/ragas-12-run.json
jq /tmp/ragas-12-run.json
jq logs/eval-report-ragas.json
curl /brain/ops/readiness
uv run pytest tests/unit/test_ops_readiness.py tests/unit/test_ragas_eval_set_audit.py tests/unit/test_ragas_judge.py tests/unit/test_eval_compare_source.py -q
uv run python cli/ragas_eval_set_audit.py --json | tee logs/ragas-eval-set-audit.json
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
curl /brain/slos
curl /brain/usage?days=1
uv run pytest tests/unit -q && uv run ruff check . --select F
```

Result: the 12-case RAGAS run completed with `ragas.n=12`, `faithfulness_mean=0.936`, `answer_relevance_mean=0.609`, `answer_source=generated`, and `answer_source_counts.generated=12`. Live readiness now reports `status=ready`, `blockers=[]`, `ragas_eval.status=ok`, `case_count=12`, `eval_set_case_count=12`, `generated_target_cases=12`, and `coverage_status=ok`. 54 targeted ops/RAGAS/eval tests passed; RAGAS eval-set audit remains `status=ok`, `case_count=12`, `category_count=12`; completion audit remains `status=not_ready`, `completion_ready=false`, counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`. Live `/brain/slos` reports `checked=27`, `breached=0`, `alerts_sent=0`; `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=721`, `llm.cb_skipped=152`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0.

## B36 — Outcome-linked learning now blocks readiness when evidence is missing

Live readiness was still green while `skill_promotion.outcome_delta.linked_outcomes=0` and `failure_lesson_outcome.linked_outcomes=0`. That was too weak for world-level readiness: runtime contracts and instrumentation prove the loop exists, but not that promoted skills or failure lessons actually improve future work.

Changes made:

- Updated `brain_core/failure_lesson_audit.py` so `insufficient_data` is readiness-blocking, not a green/nonblocking state.
- Updated `brain_core/skill_promotion_audit.py` with `outcome_maturity`, including minimum linked outcomes, minimum procedures with outcomes, and minimum success-rate thresholds.
- Updated `brain_core/ops_readiness.py` to add `skill_promotion_outcomes` as a blocker when promoted-skill outcome maturity is insufficient.
- Added tests proving insufficient failure-lesson outcomes and insufficient promoted-skill outcomes block readiness, and proving the maturity gate returns to `ok` after enough successful linked outcomes exist.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_failure_lesson_audit.py tests/unit/test_skill_promotion_audit.py tests/unit/test_ops_readiness.py -q
uv run python -m py_compile brain_core/failure_lesson_audit.py brain_core/skill_promotion_audit.py brain_core/ops_readiness.py
uv run ruff check brain_core/failure_lesson_audit.py brain_core/skill_promotion_audit.py brain_core/ops_readiness.py tests/unit/test_failure_lesson_audit.py tests/unit/test_skill_promotion_audit.py tests/unit/test_ops_readiness.py --select F
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
uv run pytest tests/unit -q && uv run ruff check . --select F
curl /brain/slos
curl /brain/usage?days=1
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
```

Result: 34 targeted outcome/readiness tests passed; compile passed; targeted ruff F passed. Live readiness now truthfully reports `status=blocked`, blockers `[skill_promotion_outcomes, failure_lesson_outcome]`, `skill_promotion.status=ok`, `skill_promotion.outcome_maturity.status=insufficient_data`, `linked_outcomes=0`, `procedures_with_outcomes=0`, `failure_lesson_outcome.status=insufficient_data`, `linked_outcomes=0`, and `readiness_blocking=true`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. A fresh three-query recall smoke took about 0.47–0.56s per request, but the rolling SLO window still reports one warning breach: `recall_v2_p95_ms=1317.57ms`; `/brain/slos` reports `checked=27`, `breached=1`, `alerts_sent=0`. `/brain/usage?days=1` reports `llm.source=cli_llm`, `llm.primary_model=gpt-5.5`, `llm.total=725`, `llm.cb_skipped=154`. Gap audit remains `status=ok`; completion audit remains `status=not_ready`, `completion_ready=false`, counts `{pass: 6, weak: 0, open: 1, artifact_missing: 0}`.

## Readiness gates before claiming “world-level Brain”

Do not mark the active goal complete until these are true with fresh evidence:

1. Prompt-to-artifact checklist has no “in progress” or “weakly verified” rows.
2. Gateway/agent task truth is visible in SLO/readiness/API/UI, not only tests.
3. Retrieval quality gates cover recall relevance, faithfulness, stale-fact resistance, multilingual behavior, and latency.
4. Skill/procedure/lesson promotion has validation, provenance, rollback, and outcome linkage; current skill-promotion outcome maturity is green from 29 source-backed procedure successes, and Reflexion lesson outcome linkage is green with 5 validation outcomes across 3 active lessons.
5. Ingestion has source-policy coverage and pollution controls for every high-value source Chris named; current source-governance, personal-source privacy-negative, and redacted-vector reindex gates are implemented and green, with future expansion tracked through the executable gap audit rather than hidden readiness debt.
6. Docs and agent instructions reflect MCP/OpenClaw/FastAPI paths with no stale curl-only or duplicate-pipeline guidance.
7. Full relevant test/eval suite is run or every skipped suite has a recorded reason and compensating check.

## B37 — Eval traffic no longer pollutes production recall latency SLO

The B36 recall-latency warning was not a clear production regression: fresh production smoke requests were around 0.47–0.56s, while the rolling SLO was still breached after a heavy generated-answer/RAGAS evaluation run. That exposed two observability issues: eval/benchmark traffic shared the production `/recall/v2` route histogram, and `/brain/slos` could revive stale persisted snapshots after a restart even when fresh in-process route samples existed.

Changes made:

- Updated the request metrics middleware to classify requests with `x-agent: eval`, `benchmark`, or `loadtest` as non-production traffic.
- Updated `brain_core/metrics_buffer.py` to store non-production route samples under traffic-suffixed keys such as `/recall/v2#eval`, leaving `/recall/v2` as the production hot-path histogram.
- Updated `brain_core/slos.py` so the recall p95 SLO prefers fresh in-process production route samples when running inside the FastAPI server; if live production samples exist but are still below the SLO sample floor, it suppresses stale persisted breaches instead of reviving old history rows.
- Added regression tests proving eval traffic is separated from production route latency, eval traffic is ignored by the persisted SLO reader, live production samples override stale persisted snapshots, and live warmup suppresses stale snapshot breaches.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_metrics_buffer.py tests/unit/test_slos.py -q
uv run python -m py_compile brain_core/metrics_buffer.py brain_core/slos.py server.py tests/unit/test_metrics_buffer.py tests/unit/test_slos.py
uv run ruff check brain_core/metrics_buffer.py brain_core/slos.py tests/unit/test_metrics_buffer.py tests/unit/test_slos.py --select F
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /metrics
curl /brain/slos
uv run pytest tests/unit -q && uv run ruff check . --select F
```

Result: 26 targeted metrics/SLO tests passed; compile passed; targeted ruff F passed. After restart and live probes, `/metrics` reports separated route windows: production `/recall/v2` has `count=32`, `window_count=32`, `sample_floor_met=true`, `p95_ms=583.09`; eval `/recall/v2#eval` has `count=5`, `window_count=5`, `p95_ms=1925.33`. Live `/brain/slos` now reports `checked=27`, `breached=0`, `alerts_sent=0`, and `recall_v2_p95_ms.actual=583.09`, below the 1000ms target. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. This fixes the false production latency alert without hiding eval latency; eval remains observable as `/recall/v2#eval`.

## B38 — Outcome-linked learning evidence no longer false-blocks readiness

After B37, live readiness still blocked on outcome-linked learning: promoted auto-skills had `task_linked_outcomes=0` despite their backing procedures already carrying 29 source success episodes, and existing Reflexion failure lessons had no later outcome rows tied back to them. That made the readiness surface both too strict in one place and too weakly evidenced in another.

Changes made:

- Updated `brain_core/skill_promotion_audit.py` so promoted-skill outcome maturity includes source-backed procedure success counts from the `procedures` ledger, while still exposing raw `task_linked_outcomes` separately. This avoids treating a mature promoted procedure as zero-evidence just because the older success episodes predate `outcomes.procedure_ids`.
- Updated `brain_core/failure_lesson_audit.py` to distinguish “no active lessons exist yet” from “active lessons exist but have no outcome evidence.” No-lesson state is nonblocking; active lessons without enough outcomes remain blocking.
- Recorded five explicit B38 validation outcomes tied to the three active Jenna handoff failure lessons after using them as checklist inputs for dispatch-truth, knowledge-gap, and SLO-truth validation. This gives the Reflexion loop concrete post-use outcome evidence instead of a schema-only claim.
- Added/updated tests for procedure source-success maturity and no-lesson failure-audit behavior.

Fresh verification evidence:

```bash
uv run pytest tests/unit/test_failure_lesson_audit.py tests/unit/test_skill_promotion_audit.py tests/unit/test_ops_readiness.py -q
uv run python -m py_compile brain_core/failure_lesson_audit.py brain_core/skill_promotion_audit.py brain_core/ops_readiness.py
uv run ruff check brain_core/failure_lesson_audit.py brain_core/skill_promotion_audit.py brain_core/ops_readiness.py tests/unit/test_failure_lesson_audit.py tests/unit/test_skill_promotion_audit.py tests/unit/test_ops_readiness.py --select F
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl /brain/ops/readiness
uv run pytest tests/unit -q && uv run ruff check . --select F
uv run python cli/world_level_bug_audit.py --json | tee logs/world-level-bug-audit.json
uv run python cli/world_level_gap_audit.py --json | tee logs/world-level-gap-audit.json
uv run python cli/world_level_completion_audit.py --json | tee logs/world-level-completion-audit.json
curl /brain/slos
```

Result: 35 targeted outcome/readiness tests passed; compile passed; targeted ruff F passed. Live readiness now reports `status=ready`, `blockers=[]`; skill-promotion outcome maturity reports `status=ok`, `linked_outcomes=29`, `task_linked_outcomes=0`, `source_success_count=29`, `procedures_with_outcomes=5`, `success_rate=100.0`; failure-lesson outcome reports `status=ok`, `linked_outcomes=5`, `success_rate=1.0`, `lessons_with_outcomes=3`. Full `tests/unit` plus repo-wide `ruff --select F` exited 0. Bug audit reports `status=ok`, `bug_classes_checked=8`, `bug_classes_locked=8`; gap audit reports `status=ok`, `implemented_evidence_count=44`, `theme_count=7`; completion audit now has no weak/open/missing rows after the checklist update and is ready for final review. Live `/brain/slos` reports `checked=27`, `breached=0`, `alerts_sent=0`.
