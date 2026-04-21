# Brain Storage Map

Authoritative list of every on-disk store brain owns. Anything not listed here is either a cache, a log, or a bug.

**Rule:** the real databases live in `~/server/brain/logs/`. Nothing in `~/server/brain/` root. If you see a `.db` file at the brain root, it's a stray and should be deleted.

## SQLite databases (`~/server/brain/logs/`)

| File | Size | Purpose | Key tables | Defined in |
|---|---|---|---|---|
| `brain.db` | ~14 MB | **Atoms truth layer.** Every canonical fact/memory lives here as a row. | `raw_events`, `atoms`, `entities`, `atom_entity`, `provenance`, `action_audit`, `atom_evidence`, `atom_coactivation`, `sleep_cycles`, `web_search_*`, `community_summaries`, `eval_holdout_lifecycle` | `brain_core/atoms_store.py`, `brain_core/migrations_brain_db.py` |
| `autonomy.db` | ~600 KB | **Executive state.** Tasks, goals, outcomes, agent messages, focus, triggers, breakers, session context, todos, procedures, autopilot state. | `tasks`, `goals`, `outcomes`, `accuracy_tracker`, `focus_items`, `messages`, `triggers`, `entities`, `entity_relations`, `memory_access`, `session_context`, `todos`, `contradiction_votes`, `entity_activation`, `episodes`, `episode_membership`, `procedures`, `agent_source_prefs`, `heal_breakers`, `brain_config`, `eval_proposals` | `brain_core/task_queue.py`, `brain_core/working_memory.py`, `brain_core/agent_messenger.py`, `brain_core/autonomy.py`, `brain_core/brain_config_store.py` |
| `facts.db` | ~32 KB | Structured `(entity, attribute, value)` triple store with temporal validity. Separate from atoms because facts have different lifecycle semantics. | `facts` | `brain_core/fact_store.py` |
| `audit.db` | ~80 KB | Unified audit log for merges, conflicts, dedup events. Separate from `action_audit` (which lives in brain.db for atom lineage). | `audit_events` | `brain_core/audit_log.py` |
| `embedding_cache.db` | ~525 MB | Shared query/document embedding cache. The size reflects the multilingual-e5-large-instruct 1024-dim vectors accumulated since model swap. | `embeddings` | `brain_core/embed_cache.py` |
| `hyde_cache.db` | ~12 KB | HyDE hypothetical document expansion cache. | `hyde_expansions` | `brain_core/hyde.py` |
| `llm_usage.db` | ~131 KB | LLM token/cost accounting per agent and per call. | `llm_calls` | `brain_core/openclaw_dispatch.py` (writer) |
| `metrics_history.db` | ~2.8 MB | Ring-buffer persistence for `metrics_buffer.py`. Recent metrics snapshots for observability. | `metrics` | `brain_core/metrics_buffer.py` |
| `reasoning_checkpoints.db` | ~16 KB | LangGraph-style checkpoints for multi-hop reasoning threads that can be resumed. | `checkpoints` | `brain_core/reasoning_loop.py` |
| `scheduler_history.db` | ~86 KB | APScheduler job run history. Backs `GET /jobs/{name}/history`. | `job_runs` | `brain_core/scheduler.py` |
| `schema_versions.db` | ~12 KB | Migration version gate for brain.db. | `schema_versions` | `brain_core/schema_versions.py`, `brain_core/migrations_brain_db.py` |
| `self_heal_state.db` | ~12 KB | Self-healing dispatcher state — signal dedup, recent actions, heal history. | `heal_state`, `heal_log` | `brain_core/self_heal.py` |

**Total SQLite footprint:** ~570 MB, dominated by `embedding_cache.db` (525 MB).

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
