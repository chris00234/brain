# Brain v2 — Cron Map

Visual schedule of every recurring job in the brain process.
All times are local (`America/Los_Angeles`). Source of truth: `brain_core/scheduler.py`.

This file is **derived from `JOB_SCHEDULE`**. Last regenerated 2026-04-13.
Re-derive with:
```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'brain_core'); from scheduler import JOB_SCHEDULE; print(len(JOB_SCHEDULE))"
```

## Hourly intervals

| Cadence | Job | Owner | Purpose |
|---|---|---|---|
| 5 min | `slos_check` | system | SLO budget check + Telegram alert on breach (Phase E1) |
| 5 min | `outbox_drain` | system | SessionEnd outbox replay (Phase 2D) |
| 1 h | `obsidian_sync` | jenna | Obsidian vault ↔ CouchDB pull |
| 1 h (:30) | `slo_monitor` | system | Hourly SLO check with Telegram alerts on 3+ violations |

## Nightly window (00:00 – 05:00)

| Time | Job | Owner | Purpose |
|---|---|---|---|
| 00:35,03:35,06:35,19:35,21:35,23:35 | `openclaw_sessions_ingest` | jenna | OpenClaw agent session distillation → raw/inbox |
| 01:15 | `claude_code_sessions_ingest` | jenna | Claude Code session distillation → raw/inbox |
| 01:30 | `gmail_ingest` | jenna | Gmail signal classifier → raw/inbox |
| 01:45 | `git_activity_ingest` | ellie | Git commit history distillation → raw/inbox |
| 02:00 | `canonical_pipeline` | system | Inbox → distilled → canonical promotion (daily) |
| 02:15 | `shell_ingest` | ellie | Shell history → experience collection |
| 02:30 | `browser_ingest` | sage | Browser history → experience collection |
| 02:30 (Sun) | `memory_lifecycle` | system | Age-out + promote durable semantic memories |
| 02:45 | `brain_reflect` | sage | Sage pattern/contradiction pass over last 7d |
| 02:50 | `graph_consolidation` | system | Nightly graph sleep: decay, prune, promote, cluster |
| 03:05 | `entity_resolution` | system | Embedding-similarity entity merge (auto >0.95, review 0.90–0.95) |
| 03:10 (Sun) | `stale_cleanup` | system | Weekly incremental stale doc cleanup |
| 03:15 | `neo4j_backup` | system | Nightly Neo4j data backup to MinIO (14d retention) |
| 03:17,23:17 | `reindex` | system | Full ChromaDB reindex (2× daily, off-hours) |
| 03:18 | `episode_binder` | system | Daily episode clustering + Hebbian boost |
| 03:20 (Sun) | `near_dedup` | system | Weekly retroactive near-duplicate scan |
| 03:25 | `code_index_refresh` | system | Daily incremental code function indexer |
| **03:25** | **`sm2_nightly`** | **system** | **SM-2 nightly: seed next_review_at + obsolete stale atoms** |
| **03:30** | **`eval_run`** | **system** | **Stable-track eval (138 queries) — strict 5pt gate, heal dispatch** |
| 03:35 (Sun) | `chroma_integrity` | system | Weekly PRAGMA integrity_check on ChromaDB SQLite |
| 03:45 | `memory_consolidation` | system | Nightly memory tier promotion/demotion |
| **03:50** | **`eval_run_extended`** | **system** | **Extended-track eval (606 queries) — trend only, no heal** |
| 04:00 (1st) | `active_contacts_ingest` | jenna | Monthly active iMessage contacts → raw/inbox |
| 04:00 | `log_rotation` | system | Truncate job/server logs >3d or >512KB |
| 04:00 (Sun) | `profile_regen` | sage | Sage regenerates Chris profile from canonical |
| 04:05 | `content_quality_slo` | system | Daily content quality SLO check (after eval_run) |
| 04:10 (15th) | `memory_pruning` | system | Monthly atrophied-memory dry-run |
| 04:15 | `fts_rebuild` | system | Nightly SQLite FTS5 keyword index rebuild |
| **04:15 (Sun)** | **`hnsw_tune`** | **system** | **Phase J2: adaptive HNSW ef_search tuning** |
| 04:15 (15th) | `memory_pruning_active` | system | Monthly REAL atrophied-memory pruning |
| 04:15 (Sun) | `weekly_synthesis` | sage | Weekly arc synthesis |
| 04:20 (1st) | `event_compressor` | system | Monthly event compression for old experience events |
| 04:30 (1st) | `backup_verify` | system | Monthly backup restore smoke test |
| 04:35 | `focus_aggregate` | system | Daily energy/focus data layer aggregation |
| 04:35 (Sun) | `screen_time_ingest` | sage | Screen Time daily patterns → raw/inbox |
| **04:45** | **`autonomy_proposer`** | **system** | **Phase 7: surface autonomy promote/demote proposals** |
| 04:45 (Sun) | `canonical_index` | system | Rebuild canonical knowledge index.md |
| 04:50 (Sun) | `hnsw_adaptive` | system | Weekly adaptive HNSW ef_search tuning |
| 04:55 (Sun) | `llm_usage_purge` | system | Weekly purge of llm_usage.db >90 days |

## Morning window (05:00 – 09:00)

| Time | Job | Owner | Purpose |
|---|---|---|---|
| 05:00 | `ghost_blog_ingest` | market | Ghost blog posts via Admin API → knowledge collection |
| 05:00 (Sun) | `memory_observability` | system | Weekly memory observability report |
| 05:00 (1st) | `monthly_synthesis` | sage | Monthly arc synthesis |
| 05:30 (Sun) | `lint_memory` | system | Weekly memory lint pass |
| 05:45 (Sun) | `memory_leak_detector` | system | Weekly memory leak detection |
| 06:00 (Sun) | `auto_resolve_contradictions` | system | Weekly auto-resolve stale/low-confidence contradictions |
| 06:00,14:00,22:00 | `personal_ingest` | jenna | Apple Notes + iMessage + Calendar + Reminders → ChromaDB |
| 06:10 (Sun) | `supersession_chain_cleanup` | system | Weekly cleanup of orphaned supersession chains |
| 06:15 (Sun) | `stale_superseded_cleanup` | system | Weekly stale superseded memory cleanup |
| 06:30 (Sun) | `feedback_aggregate` | system | Weekly search feedback aggregation |
| 06:45 (Sun) | `memory_nudge` | system | Weekly memory review nudge |
| 07:00 (Sun) | `trust_recompute` | system | Weekly cross-source corroboration trust score refresh |
| 07:15 (Sun) | `infra_validation` | system | Weekly infra fact cross-check against live state |
| 07:30,13:30,19:30,01:30 | `proactive_check` | sage | Proactive insights — schedule gaps, contradictions, trends |
| 07:30 (Sun) | `memory_health_report` | system | Weekly memory health report |
| 07:45 (Sun) | `skill_extract` | system | Weekly skill graph indexing |
| 08:00 | `proactive_insights` | system | Daily proactive insights surfacing (PST) |
| 08:00 (Sun) | `training_pairs_generate` | system | Weekly LoRA training pair generation from feedback |
| **08:45 (Sun)** | **`eval_holdout_promote`** | **system** | **Phase C1: novelty-score eval candidates, promote top-N** |
| 09:00 (Sun) | `gap_detection` | system | Weekly knowledge gap detection from recall failures |
| 09:00 | `healthcheck` | ellie | System + service health capture |
| **09:15 (Sun)** | **`eval_holdout_audit`** | **jenna** | **Phase C2: Telegram digest of pending eval candidates** |
| **09:30 (Sun)** | **`lora_ab_gate`** | **system** | **Phase 7: weekly LoRA A/B gate + deploy** |

## Evening window

| Time | Job | Owner | Purpose |
|---|---|---|---|
| 21:00 | `daily_synthesis` | jenna | Daily narrative + reflection Q |
| 22:03 | `daily_reflection` | jenna | Send reflection Q to Chris via Telegram |
| 23:17 | `reindex` (2nd run) | system | Full ChromaDB reindex (off-hours pair) |

## v3 llm-wiki jobs (added 2026-04-15)

| Time | Job | Owner | Purpose |
|---|---|---|---|
| 03:15 daily | `answer_canonicalize` | system | Score pending answer_candidates, promote top-3 to raw/inbox |
| 03:30 (Sun) | `graph_rebuild_mentions` | system | Rebuild atom→entity MENTIONS edges in Neo4j |
| 03:40 (Sun) | `graph_backfill_co_mention` | system | Create RELATES_TO edges from shared MemoryAccess (co-mention) |
| 04:30 (Sun) | `entity_pages` | sage | Sage generates one canonical entity page per run from hot Neo4j entities |
| 05:45 (Sun) | `canonical_lint` | system | Orphan notes + data gaps + missing cross-refs report |
| 06:00 (Sun) | `canonical_compaction` | system | Cluster similar canonical notes (cosine 0.94) — report only |
| 06:15 (Sun) | `canonical_merge_draft` | sage | Sage drafts consolidated pages from top compaction clusters |
| 06:35 (Sun) | `canonical_quality_filter_report` | system | Dry-run audit-log archival report |

**Human-reviewed (NOT auto-scheduled):** `canonical_merge_apply`, `canonical_quality_filter --apply`, `canonicalize_entities --apply`.

## Job count

90 total scheduled jobs as of 2026-04-15 (68 v2 + 9 v3 llm-wiki + 13 interim). To re-derive:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'brain_core'); from scheduler import JOB_SCHEDULE; print(len(JOB_SCHEDULE))"
```

## Maintenance windows

- **No heavy Ollama/Chroma jobs between 9am–6pm PST** (work hours rule).
  Enforced by `brain_core/autonomy.py` `EXECUTION_WINDOWS["heal.reindex"] = ["night"]`.
- **Quiet hours**: 23:00–07:00 PT. L3 actions get auto-demoted to L2 unless
  in the exception list (`heal.log_rotate`, `heal.vacuum_embed_cache`).
- **Reindex 2× daily**: 03:17 + 23:17 (off-hours).
- **Personal ingest 3× daily**: 06:00, 14:00, 22:00.

## Failure handling

- Every job has `misfire_grace=900` (15 min) by default; heavy jobs use 1800.
- Failed jobs land in `scheduler_failures` on `/brain/health`.
- Repeated failures trigger the persistent breaker for the action_kind
  (e.g. `heal.reindex`) and back off 5m → 15m → 1h → 4h.
- See RUNBOOK.md §2 for recovery.
