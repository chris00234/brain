# Brain Storage Map

Authoritative list of every on-disk store brain owns. Anything not listed here is either a cache, a log, or a bug.

**Rule:** the real databases live in `~/server/brain/logs/`. Nothing in `~/server/brain/` root. If you see a `.db` file at the brain root, it's a stray and should be deleted.

## SQLite databases (`~/server/brain/logs/`)

| File | Size | Purpose | Key tables | Retention | Defined in |
|---|---|---|---|---|---|
| `brain.db` | ~81 MB | **Atoms truth layer.** Every canonical fact/memory lives here as a row. | `raw_events`, `atoms`, `entities`, `atom_entity`, `provenance`, `action_audit`, `atom_evidence`, `atom_coactivation`, `sleep_cycles`, `web_search_*`, `community_summaries`, `eval_holdout_lifecycle`, `raw_events_fts` | `action_audit` 90d (`run_action_audit_retention`); atoms / entities / provenance never decay. | `brain_core/atoms_store.py`, `brain_core/migrations_brain_db.py` |
| `autonomy.db` | ~82 MB | **Executive state.** Tasks, goals, outcomes, agent messages, focus, triggers, breakers, session context, todos, procedures, autopilot state, decision ledger + autonomy gate audit. | `tasks`, `goals`, `outcomes`, `accuracy_tracker`, `focus_items`, `messages`, `triggers`, `entities`, `entity_relations`, `memory_access`, `session_context`, `todos`, `contradiction_votes`, `entity_activation`, `episodes`, `episode_membership`, `procedures`, `agent_source_prefs`, `heal_breakers`, `brain_config`, `eval_proposals`, `autonomy_decisions`, `decision_ledger` | `autonomy_decisions` 14d (`run_autonomy_decisions_retention`, daily 04:35) — table writes ~48K rows/day from every `autonomy.authorize` call; without retention this table grew the DB 600KB → 81MB in 8 days. `decision_ledger` and `outcomes` retained unbounded (low write rate, used by belief_state). `memory_access` 180d via `maintenance.prune_memory_access`. `session_context` is session-scoped via `wm_consolidate` on SessionEnd; orphan sessions (crashes / never-ended) are swept by `run_session_context_retention` daily 04:45 with a 30d window. | `brain_core/task_queue.py`, `brain_core/working_memory.py`, `brain_core/agent_messenger.py`, `brain_core/autonomy.py`, `brain_core/brain_config_store.py`, `brain_core/decision_ledger.py` |
| `facts.db` | ~32 KB | Structured `(entity, attribute, value)` triple store with temporal validity. Separate from atoms because facts have different lifecycle semantics. | `facts` | none (low write rate) | `brain_core/fact_store.py` |
| `audit.db` | ~456 KB | Unified audit log for merges, conflicts, dedup events. Separate from `action_audit` (which lives in brain.db for atom lineage). | `audit_events` | none yet (low write rate) | `brain_core/audit_log.py` |
| `embedding_cache.db` | ~397 MB | Shared query/document embedding cache. The size reflects the multilingual-e5-large-instruct 1024-dim vectors accumulated since model swap. | `prune_old(max_age_days=30, max_rows=15_000)` in `embed_cache.py`, scheduled daily at 04:08. Argparse defaults aligned 2026-04-26 — prior CLI defaults of 60d/25K were overriding the tighter function signature. | `brain_core/embed_cache.py` |
| `hyde_cache.db` | ~12 KB | HyDE hypothetical document expansion cache. Two-tier (in-memory TTLCache + persistent SQLite). | `hyde_expansions` | TTLCache 5min, persistent rows TTL via `clear_cache()` on prefix bump | `brain_core/hyde.py` |
| `llm_usage.db` | ~2.2 MB | LLM token/cost accounting per agent and per call. | `llm_calls`, `llm_usage_monthly` | 90d detail (`run_llm_usage_retention`, monthly 1st 04:30) → archived to `llm_usage_monthly` rollup forever. | `brain_core/openclaw_dispatch.py` (writer), `brain_core/db_maintenance.py` (retention) |
| `metrics_history.db` | ~81 MB | Persistence for `metrics_buffer.py`. Recent metrics snapshots for observability. SLOs only read the last 20 rows; everything older is trend-history. | `metrics_snapshots` | 14d via `run_metrics_history_retention` (daily 04:40) plus 90d safety net inside `metrics_buffer.persist`. Weekly VACUUM Sun 05:30. | `brain_core/metrics_buffer.py`, `brain_core/db_maintenance.py` (retention + VACUUM) |
| `reasoning_checkpoints.db` | ~16 KB | LangGraph-style checkpoints for multi-hop reasoning threads that can be resumed. | `checkpoints` | none (per-thread lifecycle) | `brain_core/reasoning_loop.py` |
| `scheduler_history.db` | ~2.3 MB | APScheduler job run history. Backs `GET /jobs/{name}/history`. | `job_runs` | 30d via `maintenance.prune_scheduler_history` | `brain_core/scheduler.py`, `brain_core/maintenance.py` |
| `schema_versions.db` | ~12 KB | Migration version gate for brain.db. | `schema_versions` | none (always grows by version count, currently 11) | `brain_core/schema_versions.py`, `brain_core/migrations_brain_db.py` |
| `self_heal_state.db` | ~12 KB | Self-healing dispatcher state — signal dedup, recent actions, heal history. | `heal_state`, `heal_log` | none (low write rate) | `brain_core/self_heal.py` |

**Total SQLite footprint:** ~643 MB. `embedding_cache.db` (397 MB) is the bulk; `brain.db` / `autonomy.db` / `metrics_history.db` are each ~80 MB and the latter two trim to steady-state under retention.

**Retention policy summary** (defined in `brain_core/db_maintenance.py`):
- Hot audit trails (write-only, no SELECT outside maintenance): 14d — `autonomy_decisions`, `metrics_snapshots`.
- Operational data (used by SLOs, calibration, post-mortems): 30–90d — `action_audit`, `llm_usage`, `scheduler_history`, `memory_access`, `embeddings`.
- Truth layer (canonical facts, decisions, atoms, ledger): no decay; managed via `supersede_by` pointers.
- Weekly VACUUM (Sun 05:30) covers `brain.db`, `autonomy.db`, `llm_usage.db`, `metrics_history.db`.

## JSONL state + log files (`~/server/brain/logs/`)

| File | Purpose | Rotated? |
|---|---|---|
| `proactive_insights.jsonl` | Persistent store of proactive insights (category, severity, evidence, TTL, acted_on). Read by `proactive.get_current_insights`. | TTL-based dedup (48 h default) |
| `brain_loop_journal.jsonl` | **NEW (v3 plan).** Stream of consciousness — one line per brain_loop tick with observations, decisions, actions, internal monologue. | Daily via `log_rotation` job |
| `boot-context-log.jsonl` | Boot context fetch history for observability. | Daily |
| `failures.jsonl` | Top-level ingest/dispatch failure log. | Daily |
| `search-feedback.jsonl` | Auto-recorded served-result feedback from `/recall/v2` (rate-limited 100/h). Feeds the learning loop. | Daily |
| `recall-gaps.jsonl` | Detected recall gaps (max_score < threshold) for eval proposal mining. | Daily |
| `dispatch-failures.jsonl` | Failed LLM dispatch attempts. | Daily |
| `focus-aggregate.jsonl` | Day-of-week/hour activity rollup consumed by `_predictive_queries`. | Weekly overwrite |
| `collection_size_history.jsonl` | Qdrant collection size snapshots over time. | Daily append |
| `eval-history.jsonl` / `eval-history-extended.jsonl` | Regression eval run history. | Persistent |
| `ghost-ingest-failures.jsonl`, `pdf-ingest-failures.jsonl`, `personal-ingest-failures.jsonl`, `openclaw-sessions-failures.jsonl`, `screen-time-failures.jsonl` | Per-source ingest failure logs. | Daily |
| `hooks.jsonl` | Hook firing log (brain_core/hooks.py). | Daily |
| `jobs/<name>.log` | Per-scheduler-job stdout/stderr. | Per `log_rotation` job (>512 KB or >3 d → truncate) |
| `server.log` | Main FastAPI server log. | Per `log_rotation` |

## State files (`~/server/brain/logs/` or `/tmp/`)

| File | Purpose | Lifecycle |
|---|---|---|
| `.batch_learn_state.json`, `.batch_learn_openclaw_state.json` | Watermarks for batch_distill + batch_propose pipelines. | Overwritten on each run |
| `.healthcheck_state.json` | Last healthcheck snapshot. | Overwritten on each run |
| `/tmp/.claude_boot_context.cache` + `.ts` | 5-min TTL boot payload cache (session-start identity/state/etc). | 5-min TTL |
| `/tmp/.claude_turn_<session_id>` | **NEW (v3 plan).** Per-session turn counter for active_recall dedup. | Per-session lifetime |
| `/tmp/.brain_doorbell.<session_id>.jsonl` | **NEW (v3 plan).** Brain-initiated injection queue for a specific Claude Code session. Consumed + cleared by `claude_boot.sh` on next turn. | Per-turn (cleared on read) |
| `/tmp/.brain_loop_wake` | **NEW (v3 plan).** Event-driven wake file — any caller touches it to request immediate `brain_loop.tick()`. | mtime-triggered |
| `/tmp/.claude_memory_regen.ts` | **NEW (v3 plan).** Throttle sentinel for MEMORY.md regeneration (60 s floor). | Overwritten per regen |

## External services

| Service | Endpoint | Role |
|---|---|---|
| Qdrant | `http://127.0.0.1:6333` (native, v1.17 source-build) | Vector store. 7 collections: `canonical`, `semantic_memory`, `experience`, `knowledge`, `code`, `personal`, `obsidian`. Legacy names (`semantic_contradictions`, `canonical_raptor`, `experience_compressed`, `context`, `patterns`) are aliased to their target collection via payload discriminators. int8 scalar quantization, HNSW m=16 / ef_construct=128, named `dense`/`contextual`/`raptor` vectors on canonical + `sparse` (BM25) on every collection. |
| Ollama | `http://127.0.0.1:11434` (native) | Embedder only. Model: `blaifa/multilingual-e5-large-instruct` (1024-dim). No LLM inference. |
| Neo4j | `bolt://127.0.0.1:7687` (native) | Entity graph + 2-hop expansion + `MemoryAccess` utility scoring (Zep/Graphiti pattern). No auth (localhost). |

## Staleness rule

Anything that looks like persistent state but is not in this map is a candidate for deletion or documentation. If you find such a file:

1. Check `git log --all --diff-filter=A -- <path>` to see who created it
2. Check `grep -rn '<path>' ~/server/brain/` for callers
3. If no callers → delete
4. If callers → add a row to this map

## History

- 2026-04-14 — File created. Two 0-byte stray files (`~/server/brain/brain.db`, `~/server/brain/autonomy.db`) deleted along with 9 stale `~/.openclaw/openclaw.json.bak*` config backups. Part of Phase 0 of the active brain cortical engine migration (`~/.claude/plans/atomic-chasing-liskov.md`).
