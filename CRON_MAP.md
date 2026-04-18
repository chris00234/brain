# Brain Scheduler Cron Map

> **Auto-regenerated 2026-04-18T04:38:16+00:00** from `brain_core/scheduler.py`.
> Do not hand-edit; run `python /tmp/regen_cron_map.py > CRON_MAP.md` to refresh.

**Total jobs**: 108
**Default `misfire_grace`**: 300s (5min). Heavy nightly jobs override to 900s (15min). Changed 2026-04-16 from 3600s to prevent thundering herd after brain-server restart.

## Jobs by owning agent

### ellie (3 jobs)

| Name | Trigger | Misfire Grace | Description |
|------|---------|---------------|-------------|
| `git_activity_ingest` | `cron(hour=1, minute=45)` | 300s | Git commit history distillation via Ellie → raw/inbox (1:45am, after gmail_ingest) |
| `healthcheck` | `cron(hour=9, minute=0)` | 300s | System + service health capture |
| `shell_ingest` | `cron(hour=2, minute=15)` | 300s | Shell history → experience collection |

### jenna (8 jobs)

| Name | Trigger | Misfire Grace | Description |
|------|---------|---------------|-------------|
| `active_contacts_ingest` | `cron(day=1, hour=4, minute=0)` | 300s | Active iMessage contacts via Jenna → raw/inbox (monthly) |
| `claude_code_sessions_ingest` | `cron(hour=1, minute=15)` | 300s | Claude Code session distillation via Jenna → raw/inbox |
| `daily_synthesis` | `cron(hour=21, minute=0)` | 300s | Daily narrative + reflection Q (Jenna) |
| `eval_holdout_audit` | `cron(day_of_week=sun, hour=9, minute=15)` | 900s | Phase C2: Telegram digest of >=14d stuck candidates only (Sun 9:15am) |
| `gmail_ingest` | `cron(hour=1, minute=30)` | 300s | Gmail signal classifier → raw/inbox |
| `obsidian_sync` | `interval(1:00:00)` | 300s | Obsidian vault ↔ CouchDB pull |
| `openclaw_sessions_ingest` | `cron(hour=0,3,6,19,21,23, minute=35)` | 300s | OpenClaw agent session distillation via Jenna → raw/inbox (6×/day off-peak, respects 9am-6pm no-Ollama rule) |
| `personal_ingest` | `cron(hour=6,14,22, minute=0)` | 300s | Apple Notes + iMessage + Calendar + Reminders → ChromaDB (3x daily off-peak) |

### market (1 jobs)

| Name | Trigger | Misfire Grace | Description |
|------|---------|---------------|-------------|
| `ghost_blog_ingest` | `cron(hour=5, minute=0)` | 300s | Ghost blog posts via Admin API → knowledge collection |

### sage (12 jobs)

| Name | Trigger | Misfire Grace | Description |
|------|---------|---------------|-------------|
| `brain_reflect` | `cron(hour=2, minute=45)` | 900s | Nightly Sage pattern/contradiction pass over last 7d of semantic_memory |
| `browser_ingest` | `cron(hour=2, minute=30)` | 300s | Browser history → experience collection |
| `canonical_merge_draft` | `cron(day_of_week=sun, hour=6, minute=15)` | 1800s | Weekly top-3 compaction cluster Sage drafts (Sunday 6:15am, after compaction report) |
| `community_summaries` | `cron(day_of_week=sun, hour=5, minute=0)` | 1800s | M8.5: Louvain community detection on entity graph + Sage summary per cluster (Sun 5:00am) |
| `dream_replay` | `cron(day_of_week=sun, hour=8, minute=30)` | 1800s | Weekly REM-like generative conjecture synthesis (Sun 08:30) |
| `entity_pages` | `cron(day_of_week=sun, hour=4, minute=30)` | 1800s | Weekly entity page generator — Sage synthesizes one hot entity per run (Sunday 4:30am) |
| `monthly_synthesis` | `cron(day=1, hour=5, minute=0)` | 300s | Monthly arc (Sage, 1st of month 5am) |
| `proactive_check` | `cron(hour=7,13,19,1, minute=30)` | 300s | Proactive insights — schedule gaps, contradictions, trends (4x daily) |
| `profile_regen` | `cron(day_of_week=sun, hour=4, minute=0)` | 300s | Sage regenerates Chris profile from canonical knowledge (Sunday 4am) |
| `raptor_build` | `cron(day_of_week=sun, hour=7, minute=15)` | 1800s | Weekly RAPTOR hierarchical summary tree (Sun 07:15) |
| `screen_time_ingest` | `cron(day_of_week=sun, hour=4, minute=35)` | 300s | Screen Time daily patterns via Sage → raw/inbox (weekly) |
| `weekly_synthesis` | `cron(day_of_week=sun, hour=4, minute=15)` | 300s | Weekly arc (Sage, Sunday 4:15am) |

### system (84 jobs)

| Name | Trigger | Misfire Grace | Description |
|------|---------|---------------|-------------|
| `action_audit_retention` | `cron(hour=4, minute=20)` | 900s | Prune action_audit rows older than 90d (daily 4:20am) |
| `answer_canonicalize` | `cron(hour=3, minute=50)` | 900s | Nightly query→canonical promoter (03:50am — staggered off neo4j_backup at 03:15 to avoid reading while backup writes) |
| `atoms_to_skills` | `cron(day_of_week=sun, hour=4, minute=55)` | 900s | Promote high-confidence atoms → domain Claude Code skills (Sun 04:55) |
| `auto_resolve_contradictions` | `cron(hour=6, minute=0)` | 900s | Daily auto-resolve stale/low-confidence contradictions (6:00am) — v3 bumped from weekly to daily after finding 20-ite... |
| `autonomy_proposer` | `cron(hour=4, minute=45)` | 300s | Phase 7: surface autonomy level promote/demote proposals (4:45am) |
| `backup_verify` | `cron(day=1, hour=4, minute=30)` | 900s | Monthly backup restore smoke test (1st of month, 4:30am) |
| `brain_loop_tick` | `interval(0:01:00)` | 30s | v3: brain_loop executive cortex tick (every 60s) |
| `canonical_compaction` | `cron(day_of_week=sun, hour=6, minute=0)` | 1800s | Weekly compaction candidate clustering report (Sunday 6:00am, after canonical_lint) |
| `canonical_design_drift` | `cron(day_of_week=sun, hour=5, minute=30)` | 900s | v3: weekly design source vs canonical mirror SHA check (Sun 05:30) |
| `canonical_index` | `cron(day_of_week=sun, hour=4, minute=45)` | 300s | Rebuild canonical knowledge index.md (weekly Sunday 4:45am, no LLM) |
| `canonical_lint` | `cron(day_of_week=sun, hour=5, minute=45)` | 900s | Weekly structural lint: orphan canonical notes (Sunday 5:45am) |
| `canonical_pipeline` | `cron(hour=2,7,22, minute=0)` | 900s | Automated canonical promotion (3× daily: 02:00 / 07:00 / 22:00 PT) |
| `canonical_quality_filter_report` | `cron(day_of_week=sun, hour=6, minute=35)` | 900s | Weekly quality filter dry-run report (Sunday 6:35am, review only) |
| `canonical_quality_triage` | `cron(day_of_week=sun, hour=7, minute=0)` | 1800s | LLM classifies score=2 canonical_quality items as archive/keep/uncertain |
| `canonicalize_entities_dryrun` | `cron(day_of_week=sun, hour=6, minute=45)` | 900s | v3: weekly entity dedup proposal scan (Sun 06:45, dry-run) |
| `chroma_integrity` | `cron(day_of_week=sun, hour=3, minute=35)` | 300s | Weekly PRAGMA integrity_check on ChromaDB SQLite (Sun 3:35am) |
| `code_index_refresh` | `cron(hour=3, minute=35)` | 1200s | Daily incremental code function indexer (3:35am — staggered off sm2_nightly at 03:25) |
| `confidence_calibration` | `cron(day_of_week=sun, hour=4, minute=10)` | 900s | Weekly Platt calibration of atoms.confidence vs eval outcomes (Sun 04:10) |
| `content_quality_slo` | `cron(hour=4, minute=5)` | 300s | Daily content quality SLO check (4:00am, after eval_run) |
| `contextual_embed_weekly` | `cron(day_of_week=sun, hour=5, minute=0)` | 1800s | T2.12: re-embed canonical chunks with Anthropic-style per-doc context prefix (Sun 5:00am) |
| `db_vacuum_weekly` | `cron(day_of_week=sun, hour=5, minute=30)` | 1800s | Weekly VACUUM + ANALYZE on brain.db/autonomy.db/llm_usage.db (Sun 5:30am) |
| `embed_cache_prune` | `cron(hour=4, minute=5)` | 900s | Prune embed cache: drop legacy rows, age >60d, cap 25k (daily 4:05am) |
| `embed_finetune` | `cron(day_of_week=sat, hour=23, minute=30)` | 3600s | Phase N3: weekly LoRA training on accumulated feedback pairs (Sat 23:30) |
| `entity_reconcile` | `cron(hour=2, minute=55)` | 1800s | v3: nightly catch-up for atoms with missing entity extraction (02:55) |
| `entity_resolution` | `cron(hour=3, minute=5)` | 900s | Nightly entity merge: embedding similarity + co-occurrence (3:05am) |
| `episode_binder` | `cron(hour=3, minute=18)` | 900s | Daily episode clustering + Hebbian boost (3:18am, after entity_resolution) |
| `eval_holdout_graduate` | `cron(day_of_week=sun, hour=7, minute=30)` | 900s | Phase N3: auto-graduate consistently-passing holdout candidates (Sun 7:30am) |
| `eval_holdout_promote` | `cron(day_of_week=sun, hour=8, minute=45)` | 900s | Phase C1: novelty-score eval candidates, promote top-N to pending file (Sun 8:45am) |
| `eval_proposal_triage` | `cron(hour=4, minute=20)` | 900s | CLI codex auto-approves/rejects candidate eval_proposals (daily 4:20am) |
| `eval_run` | `cron(hour=3, minute=30)` | 900s | Stable-track eval (daily 3:30am) — strict 5pt gate, heal on regression |
| `eval_run_extended` | `cron(hour=3, minute=50)` | 900s | Extended-track eval (daily 3:50am) — trend only, no heal, 10pt threshold |
| `event_compressor` | `cron(day=1, hour=4, minute=20)` | 1800s | Monthly event compression for old experience events (1st of month, 4:20am) |
| `feedback_aggregate` | `cron(day_of_week=sun, hour=6, minute=30)` | 900s | Weekly search feedback aggregation (Sun 6:30am) |
| `focus_aggregate` | `cron(hour=4, minute=35)` | 600s | Daily energy/focus data layer aggregation (4:35am) |
| `fts_rebuild` | `cron(hour=4, minute=15)` | 900s | Nightly SQLite FTS5 keyword index rebuild (4:15am) |
| `gap_detection` | `cron(day_of_week=sun, hour=9, minute=0)` | 900s | Weekly knowledge gap detection from recall failures (Sunday 9:00am) |
| `graph_backfill_co_mention` | `cron(day_of_week=sun, hour=3, minute=40)` | 900s | Weekly co-occurrence RELATES_TO backfill from shared MemoryAccess (Sunday 3:40am) |
| `graph_consolidation` | `cron(hour=2, minute=50)` | 900s | Nightly graph sleep: decay, prune, promote, cluster (2:50am) |
| `graph_rebuild_mentions` | `cron(day_of_week=sun, hour=3, minute=30)` | 1800s | Weekly rebuild of atom→entity MENTIONS edges in Neo4j (Sunday 3:30am) |
| `habituation_prune` | `cron(hour=3, minute=20)` | 300s | Drop attention_queue rows with shown_count ≥ 300 (daily 3:20am) |
| `hnsw_adaptive` | `cron(day_of_week=sun, hour=4, minute=50)` | 900s | Weekly adaptive HNSW ef_search tuning (Sunday 4:50am) |
| `image_ingest` | `cron(hour=5, minute=45)` | 1800s | M7-WS2b: scan ~/Pictures/brain-ingest, OCR via Docling, embed captions → knowledge |
| `infra_validation` | `cron(day_of_week=sun, hour=7, minute=15)` | 300s | Weekly infra fact cross-check against live state (Sunday 7:15am) |
| `intent_miss_scan` | `cron(hour=3, minute=28)` | 900s | v3: scan active_recall misses via correction regex (daily 3:28am) |
| `lint_memory` | `cron(day_of_week=sun, hour=5, minute=35)` | 900s | Weekly memory lint pass (Sunday 5:35am — staggered off canonical_design_drift at 05:30) |
| `live_state_snapshot` | `interval(0:10:00)` | 120s | v3: snapshot current docker/launchd/goals/commits/sessions state (every 10min) |
| `llm_backlog_drain` | `interval(0:30:00)` | 300s | v3: LLM backlog catch-up queue drain (every 30 min) |
| `llm_usage_purge` | `cron(day_of_week=sun, hour=4, minute=55)` | 900s | Weekly purge of llm_usage.db >90 days (Sun 4:55am) |
| `llm_usage_retention` | `cron(day=1, hour=4, minute=30)` | 1800s | Roll up llm_usage older than 90d into llm_usage_monthly (1st of month 4:30am) |
| `log_rotation` | `cron(hour=4, minute=0)` | 300s | Truncate job/server logs >3d or >512KB (keeps last 100 lines) |
| `lora_ab_gate` | `cron(day_of_week=sun, hour=9, minute=30)` | 1800s | Phase 7: weekly LoRA A/B gate + deploy (Sun 9:30am) |
| `ltr_train` | `cron(day_of_week=sun, hour=4, minute=20)` | 900s | Weekly LogisticRegression LtR fit on recall feedback (Sun 04:20) |
| `memory_consolidation` | `cron(hour=3, minute=45)` | 900s | Nightly memory tier promotion/demotion (3:45am, Phase 1D) |
| `memory_health_report` | `cron(day_of_week=sun, hour=7, minute=30)` | 300s | Weekly memory health report (Sunday 7:30am) |
| `memory_leak_detector` | `cron(day_of_week=sun, hour=5, minute=50)` | 900s | Weekly memory leak detection (Sunday 5:50am — staggered off canonical_lint at 05:45) |
| `memory_lifecycle` | `cron(day_of_week=sun, hour=2, minute=30)` | 300s | Age out + promote durable semantic memories (Sunday 2:30am) |
| `memory_nudge` | `cron(day_of_week=sun, hour=6, minute=50)` | 900s | Weekly memory review nudge (Sunday 6:50am — staggered off canonicalize_entities_dryrun at 06:45) |
| `memory_observability` | `cron(day_of_week=sun, hour=5, minute=0)` | 900s | Weekly memory observability report (Sunday 5am) |
| `memory_pruning` | `cron(day=15, hour=4, minute=10)` | 1800s | Monthly atrophied-memory dry-run (15th 4:10am) |
| `memory_pruning_active` | `cron(day=15, hour=4, minute=15)` | 1800s | Monthly REAL atrophied-memory pruning (15th 4:15am, dry_run=False) |
| `near_dedup` | `cron(day_of_week=sun, hour=3, minute=20)` | 300s | Weekly retroactive near-duplicate scan of semantic_memory (Sun 3:20am) |
| `neo4j_backup` | `cron(hour=3, minute=15)` | 300s | Nightly Neo4j data backup to MinIO (14-day retention) |
| `outbox_drain` | `interval(0:05:00)` | 120s | Phase 2D: drain SessionEnd outbox envelopes (every 5 min) |
| `pdf_ingest` | `cron(hour=5, minute=30)` | 1800s | M7-WS2a: scan ~/Documents/PDFs, parse via Docling, embed → knowledge |
| `proactive_insights` | `cron(hour=8, minute=0)` | 900s | Daily proactive insights surfacing (8:00am PST) |
| `prune_raw_orphaned` | `cron(month=1,4,7,10, day=1, hour=4, minute=25)` | 1800s | Quarterly raw/orphaned prune (180d retention; 1st of Jan/Apr/Jul/Oct @ 04:25) |
| `re_examine_rejected` | `cron(day=2, hour=4, minute=30)` | 1800s | Monthly rejected-proposal re-examination (2nd of month @ 04:30) |
| `reindex` | `cron(hour=3,23, minute=17)` | 900s | Full ChromaDB reindex (2x daily, off-hours) |
| `retrieval_inhibition` | `cron(hour=3, minute=58)` | 600s | Nightly Bjork-style inhibition of consistent retrieval losers (03:58am) |
| `schema_learner` | `cron(day_of_week=sun, hour=4, minute=40)` | 900s | CLS spectral clustering on atom coactivation → compaction candidates (Sun 04:40) |
| `schema_revision` | `cron(day_of_week=sun, hour=8, minute=45)` | 900s | Weekly free-energy schema revision (Sun 08:45) |
| `session_rotate` | `cron(day_of_week=sun, hour=4, minute=30)` | 900s | Weekly: archive old agent session checkpoints; alert on oversized live sessions (Sun 4:30am) |
| `skill_extract` | `cron(day_of_week=sun, hour=7, minute=45)` | 900s | Weekly skill graph indexing (Sunday 7:45am) |
| `skill_materialize_cleanup` | `cron(hour=4, minute=10)` | 900s | T2.10: archive orphaned/stale auto-* SKILL.md files; enforce MAX_AUTO_SKILLS cap (daily 4:10am) |
| `sleep_consolidate` | `cron(hour=3, minute=55)` | 900s | CLS sleep consolidation: coactivation + A-MEM + promotion (3:55am, Phase N4) |
| `slo_monitor` | `cron(minute=30)` | 300s | Hourly SLO check with Telegram alerts on 3+ violations |
| `slos_check` | `interval(0:05:00)` | 120s | Phase E1: SLO budget check + Telegram alert on breach (every 5 min) |
| `sm2_nightly` | `cron(hour=3, minute=25)` | 900s | SM-2 nightly: seed next_review_at + obsolete stale atoms (3:25am) |
| `stale_cleanup` | `cron(day_of_week=sun, hour=3, minute=10)` | 300s | Weekly incremental stale doc cleanup across collections (Sun 3:10am) |
| `stale_superseded_cleanup` | `cron(day_of_week=sun, hour=6, minute=20)` | 900s | Weekly stale superseded memory cleanup (Sun 6:20am — staggered off canonical_merge_draft at 06:15) |
| `supersession_chain_cleanup` | `cron(day_of_week=sun, hour=6, minute=10)` | 300s | Weekly cleanup of orphaned supersession chains (Sun 6:10am) |
| `training_pairs_generate` | `cron(day_of_week=sun, hour=8, minute=0)` | 900s | Weekly training pair generation from feedback (Sunday 8:00am) |
| `trust_recompute` | `cron(day_of_week=sun, hour=7, minute=0)` | 900s | Weekly cross-source corroboration trust score refresh (Sunday 7:00am) |
| `web_source_trust_recompute` | `cron(day_of_week=sun, hour=5, minute=15)` | 900s | Phase M6: recompute per-domain web search trust scores (Sun 5:15) |

## All jobs alphabetical

| Name | Trigger | Agent | Misfire Grace |
|------|---------|-------|---------------|
| `action_audit_retention` | `cron(hour=4, minute=20)` | system | 900s |
| `active_contacts_ingest` | `cron(day=1, hour=4, minute=0)` | jenna | 300s |
| `answer_canonicalize` | `cron(hour=3, minute=50)` | system | 900s |
| `atoms_to_skills` | `cron(day_of_week=sun, hour=4, minute=55)` | system | 900s |
| `auto_resolve_contradictions` | `cron(hour=6, minute=0)` | system | 900s |
| `autonomy_proposer` | `cron(hour=4, minute=45)` | system | 300s |
| `backup_verify` | `cron(day=1, hour=4, minute=30)` | system | 900s |
| `brain_loop_tick` | `interval(0:01:00)` | system | 30s |
| `brain_reflect` | `cron(hour=2, minute=45)` | sage | 900s |
| `browser_ingest` | `cron(hour=2, minute=30)` | sage | 300s |
| `canonical_compaction` | `cron(day_of_week=sun, hour=6, minute=0)` | system | 1800s |
| `canonical_design_drift` | `cron(day_of_week=sun, hour=5, minute=30)` | system | 900s |
| `canonical_index` | `cron(day_of_week=sun, hour=4, minute=45)` | system | 300s |
| `canonical_lint` | `cron(day_of_week=sun, hour=5, minute=45)` | system | 900s |
| `canonical_merge_draft` | `cron(day_of_week=sun, hour=6, minute=15)` | sage | 1800s |
| `canonical_pipeline` | `cron(hour=2,7,22, minute=0)` | system | 900s |
| `canonical_quality_filter_report` | `cron(day_of_week=sun, hour=6, minute=35)` | system | 900s |
| `canonical_quality_triage` | `cron(day_of_week=sun, hour=7, minute=0)` | system | 1800s |
| `canonicalize_entities_dryrun` | `cron(day_of_week=sun, hour=6, minute=45)` | system | 900s |
| `chroma_integrity` | `cron(day_of_week=sun, hour=3, minute=35)` | system | 300s |
| `claude_code_sessions_ingest` | `cron(hour=1, minute=15)` | jenna | 300s |
| `code_index_refresh` | `cron(hour=3, minute=35)` | system | 1200s |
| `community_summaries` | `cron(day_of_week=sun, hour=5, minute=0)` | sage | 1800s |
| `confidence_calibration` | `cron(day_of_week=sun, hour=4, minute=10)` | system | 900s |
| `content_quality_slo` | `cron(hour=4, minute=5)` | system | 300s |
| `contextual_embed_weekly` | `cron(day_of_week=sun, hour=5, minute=0)` | system | 1800s |
| `daily_synthesis` | `cron(hour=21, minute=0)` | jenna | 300s |
| `db_vacuum_weekly` | `cron(day_of_week=sun, hour=5, minute=30)` | system | 1800s |
| `dream_replay` | `cron(day_of_week=sun, hour=8, minute=30)` | sage | 1800s |
| `embed_cache_prune` | `cron(hour=4, minute=5)` | system | 900s |
| `embed_finetune` | `cron(day_of_week=sat, hour=23, minute=30)` | system | 3600s |
| `entity_pages` | `cron(day_of_week=sun, hour=4, minute=30)` | sage | 1800s |
| `entity_reconcile` | `cron(hour=2, minute=55)` | system | 1800s |
| `entity_resolution` | `cron(hour=3, minute=5)` | system | 900s |
| `episode_binder` | `cron(hour=3, minute=18)` | system | 900s |
| `eval_holdout_audit` | `cron(day_of_week=sun, hour=9, minute=15)` | jenna | 900s |
| `eval_holdout_graduate` | `cron(day_of_week=sun, hour=7, minute=30)` | system | 900s |
| `eval_holdout_promote` | `cron(day_of_week=sun, hour=8, minute=45)` | system | 900s |
| `eval_proposal_triage` | `cron(hour=4, minute=20)` | system | 900s |
| `eval_run` | `cron(hour=3, minute=30)` | system | 900s |
| `eval_run_extended` | `cron(hour=3, minute=50)` | system | 900s |
| `event_compressor` | `cron(day=1, hour=4, minute=20)` | system | 1800s |
| `feedback_aggregate` | `cron(day_of_week=sun, hour=6, minute=30)` | system | 900s |
| `focus_aggregate` | `cron(hour=4, minute=35)` | system | 600s |
| `fts_rebuild` | `cron(hour=4, minute=15)` | system | 900s |
| `gap_detection` | `cron(day_of_week=sun, hour=9, minute=0)` | system | 900s |
| `ghost_blog_ingest` | `cron(hour=5, minute=0)` | market | 300s |
| `git_activity_ingest` | `cron(hour=1, minute=45)` | ellie | 300s |
| `gmail_ingest` | `cron(hour=1, minute=30)` | jenna | 300s |
| `graph_backfill_co_mention` | `cron(day_of_week=sun, hour=3, minute=40)` | system | 900s |
| `graph_consolidation` | `cron(hour=2, minute=50)` | system | 900s |
| `graph_rebuild_mentions` | `cron(day_of_week=sun, hour=3, minute=30)` | system | 1800s |
| `habituation_prune` | `cron(hour=3, minute=20)` | system | 300s |
| `healthcheck` | `cron(hour=9, minute=0)` | ellie | 300s |
| `hnsw_adaptive` | `cron(day_of_week=sun, hour=4, minute=50)` | system | 900s |
| `image_ingest` | `cron(hour=5, minute=45)` | system | 1800s |
| `infra_validation` | `cron(day_of_week=sun, hour=7, minute=15)` | system | 300s |
| `intent_miss_scan` | `cron(hour=3, minute=28)` | system | 900s |
| `lint_memory` | `cron(day_of_week=sun, hour=5, minute=35)` | system | 900s |
| `live_state_snapshot` | `interval(0:10:00)` | system | 120s |
| `llm_backlog_drain` | `interval(0:30:00)` | system | 300s |
| `llm_usage_purge` | `cron(day_of_week=sun, hour=4, minute=55)` | system | 900s |
| `llm_usage_retention` | `cron(day=1, hour=4, minute=30)` | system | 1800s |
| `log_rotation` | `cron(hour=4, minute=0)` | system | 300s |
| `lora_ab_gate` | `cron(day_of_week=sun, hour=9, minute=30)` | system | 1800s |
| `ltr_train` | `cron(day_of_week=sun, hour=4, minute=20)` | system | 900s |
| `memory_consolidation` | `cron(hour=3, minute=45)` | system | 900s |
| `memory_health_report` | `cron(day_of_week=sun, hour=7, minute=30)` | system | 300s |
| `memory_leak_detector` | `cron(day_of_week=sun, hour=5, minute=50)` | system | 900s |
| `memory_lifecycle` | `cron(day_of_week=sun, hour=2, minute=30)` | system | 300s |
| `memory_nudge` | `cron(day_of_week=sun, hour=6, minute=50)` | system | 900s |
| `memory_observability` | `cron(day_of_week=sun, hour=5, minute=0)` | system | 900s |
| `memory_pruning` | `cron(day=15, hour=4, minute=10)` | system | 1800s |
| `memory_pruning_active` | `cron(day=15, hour=4, minute=15)` | system | 1800s |
| `monthly_synthesis` | `cron(day=1, hour=5, minute=0)` | sage | 300s |
| `near_dedup` | `cron(day_of_week=sun, hour=3, minute=20)` | system | 300s |
| `neo4j_backup` | `cron(hour=3, minute=15)` | system | 300s |
| `obsidian_sync` | `interval(1:00:00)` | jenna | 300s |
| `openclaw_sessions_ingest` | `cron(hour=0,3,6,19,21,23, minute=35)` | jenna | 300s |
| `outbox_drain` | `interval(0:05:00)` | system | 120s |
| `pdf_ingest` | `cron(hour=5, minute=30)` | system | 1800s |
| `personal_ingest` | `cron(hour=6,14,22, minute=0)` | jenna | 300s |
| `proactive_check` | `cron(hour=7,13,19,1, minute=30)` | sage | 300s |
| `proactive_insights` | `cron(hour=8, minute=0)` | system | 900s |
| `profile_regen` | `cron(day_of_week=sun, hour=4, minute=0)` | sage | 300s |
| `prune_raw_orphaned` | `cron(month=1,4,7,10, day=1, hour=4, minute=25)` | system | 1800s |
| `raptor_build` | `cron(day_of_week=sun, hour=7, minute=15)` | sage | 1800s |
| `re_examine_rejected` | `cron(day=2, hour=4, minute=30)` | system | 1800s |
| `reindex` | `cron(hour=3,23, minute=17)` | system | 900s |
| `retrieval_inhibition` | `cron(hour=3, minute=58)` | system | 600s |
| `schema_learner` | `cron(day_of_week=sun, hour=4, minute=40)` | system | 900s |
| `schema_revision` | `cron(day_of_week=sun, hour=8, minute=45)` | system | 900s |
| `screen_time_ingest` | `cron(day_of_week=sun, hour=4, minute=35)` | sage | 300s |
| `session_rotate` | `cron(day_of_week=sun, hour=4, minute=30)` | system | 900s |
| `shell_ingest` | `cron(hour=2, minute=15)` | ellie | 300s |
| `skill_extract` | `cron(day_of_week=sun, hour=7, minute=45)` | system | 900s |
| `skill_materialize_cleanup` | `cron(hour=4, minute=10)` | system | 900s |
| `sleep_consolidate` | `cron(hour=3, minute=55)` | system | 900s |
| `slo_monitor` | `cron(minute=30)` | system | 300s |
| `slos_check` | `interval(0:05:00)` | system | 120s |
| `sm2_nightly` | `cron(hour=3, minute=25)` | system | 900s |
| `stale_cleanup` | `cron(day_of_week=sun, hour=3, minute=10)` | system | 300s |
| `stale_superseded_cleanup` | `cron(day_of_week=sun, hour=6, minute=20)` | system | 900s |
| `supersession_chain_cleanup` | `cron(day_of_week=sun, hour=6, minute=10)` | system | 300s |
| `training_pairs_generate` | `cron(day_of_week=sun, hour=8, minute=0)` | system | 900s |
| `trust_recompute` | `cron(day_of_week=sun, hour=7, minute=0)` | system | 900s |
| `web_source_trust_recompute` | `cron(day_of_week=sun, hour=5, minute=15)` | system | 900s |
| `weekly_synthesis` | `cron(day_of_week=sun, hour=4, minute=15)` | sage | 300s |
