# Brain Replacement Capability Gap Analysis — 2026-06-11

Goal: Brain should eventually substitute for Chris's own memory, not only improve known recall bugs.

## Evidence-backed current architecture

- Recall quality / top-k cleanliness: `brain_core/routes/recall.py`, `brain_core/search_unified.py`, `brain_core/recall_governance/*`, `cli/retrieval_regression.py`, `cli/eval_set_stable.json`, and recent clean-hit tests under `tests/unit/test_recall_governance_*`.
- Temporal current-truth model: `brain_core/atoms_store.py` stores `valid_from`, `valid_until`, `supersedes`, `superseded_by`, `created_at`, `updated_at`; `brain_core/recall_governance/temporal_resolution.py` demotes stale temporal conflicts.
- Entity/property model: `brain_core/atoms_store.py` has `entities` and `atom_entity`; `brain_core/entity_graph.py` provides Neo4j plus SQLite fallback relations; `tests/unit/test_ontology.py` gates aliases, constraints, and one-hop expansion.
- Doubt/uncertainty: `brain_core/routes/memory.py:/brain/doubt` surfaces low-confidence atoms and pending contradictions; `brain_core/conflict_surfacer.py` creates candidate contradiction review tasks.
- Autonomous/open work: `brain_core/task_queue.py` stores tasks, goals, dependencies, outcomes, dispatch attempts in `logs/autonomy.db`.
- Ingestion/provenance: `brain_core/atoms_store.py` has `raw_events` and `provenance`; `brain_core/routes/knowledge.py:/brain/ingest` ingests documents; `pyproject.toml` includes `docling`.
- Consolidation/forgetting: `brain_core/atoms_store.py` has review/decay/supersession fields; `brain_core/routes/memory.py` exposes `/brain/consolidate` and delete/contradiction-resolution routes.
- Answer interface: `brain_mcp_server.py` exposes compact MCP tools including `brain_recall` and `brain_doubt`; `AGENT_HARNESS.md` documents agent-facing contracts.

## Capability gap ranking

| Rank | Capability | Current status | Daily-use impact | Risk | Gap |
|---:|---|---|---:|---:|---|
| 1 | Open-loop / commitment tracking | implemented_v1 in this task | 5 | 2 | Detection now exists, but no resolve/snooze/feedback workflow yet. |
| 2 | Prospective memory | partial | 5 | 3 | No first-class due_at/context-triggered reminders or calendar/reminder joins. |
| 3 | Temporal autobiographical memory | partial | 5 | 3 | Atom timestamps exist, but no unified timeline projection explaining what changed/why/current truth. |
| 4 | Metacognitive evals beyond recall | partial | 5 | 2 | Recall evals exist; replacement-readiness categories need CI/nightly thresholds. |
| 5 | Confidence/doubt | partial | 5 | 2 | Doubt exists; needs actionability ranking and usefulness/false-alarm feedback. |
| 6 | Permission/privacy | partial | 4 | 4 | Scope fields and prefetch gates exist; no policy matrix by agent/channel/context. |
| 7 | Entity relationship/property model | partial | 4 | 3 | Entity data split across atoms, facts, Neo4j, SQLite; no unified personal ontology API. |
| 8 | Sensory/document ingestion quality | partial | 4 | 3 | Not all sources normalized; no per-source evidence/noise/PII eval slices. |
| 9 | Active consolidation/forgetting | partial | 4 | 3 | Lifecycle primitives exist; no single policy manifest with archival/rollback reasons. |
| 10 | Answer interface projections | partial | 4 | 2 | Compact recall exists, but not intent-specific packs for commitments/timelines/entities/doubts. |

## Implemented now

1. `brain_core/open_loops.py`
   - Deterministic durable commitment / follow-up / waiting-on / deadline classifier.
   - Rejects session chatter (`maybe`, `could`, `what if`, Korean idea/chatter forms).
   - Rejects resolved items (`done`, `completed`, `shipped`, Korean complete forms).
   - Scans active atoms from `logs/brain.db` and non-terminal tasks from `logs/autonomy.db` without mutating state.

2. `brain_core/routes/memory.py`
   - `/brain/doubt` now includes `open_loops` so uncertainty includes stale commitments and waiting-on items.
   - Added direct `GET /brain/open-loops` surface.
   - Added `GET /brain/replacement-readiness` surface.

3. `brain_core/brain_replacement_readiness.py`
   - Deterministic readiness manifest for the 10 required capability categories.
   - Includes status, daily-use impact, implementation risk, score, exact evidence, gap, and next contract.

4. Tests/evals
   - `tests/unit/test_open_loops.py` gates durable commitments vs session chatter/resolved chatter, atom scanning, task scanning, combined snapshot.
   - `tests/unit/test_brain_replacement_readiness.py` gates required capability coverage, shipped open-loop evidence, and ranked next contracts.

## New measurable gate

`readiness_snapshot()` returns:

- `overall_score`: weighted replacement readiness score.
- `capabilities`: all required categories with evidence/gap/next contract.
- `implemented_now`: includes `open_loop_tracking` and `brain_replacement_readiness_eval`.
- `next_3_contracts`: highest-priority next work.

## Next 3 capability contracts

1. Add first-class prospective memory store: `commitments/reminders` table with `due_at`, `context_trigger`, `owner`, `visibility`, `status`, source evidence, and gateway-safe notification policy.
2. Build current-belief timeline API: join supersession/provenance/change events into “what changed, when, why, what remains true now” projections per topic/entity.
3. Promote replacement-readiness evals to CI/nightly: thresholds for open-loop precision/recall, privacy negative controls, temporal current-truth harm, and doubt usefulness/false alarms.
