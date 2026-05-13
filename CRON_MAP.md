# Brain Scheduler Cron Map

> Auto-generated from `brain_core/job_definitions.py` by `cli/render_cron_map.py`.
> Do not hand-edit; run `.venv/bin/python cli/render_cron_map.py --write`.

**Total jobs**: 152
**Default `misfire_grace`**: 300s (5min). Heavy nightly jobs override per job.

## Jobs by owning agent

### ellie (3 jobs)

| Name | Trigger | Budget | Tags | Misfire Grace | Description |
|---|---|---|---|---|---|
| `git_activity_ingest` | `cron(hour=1, minute=45)` | standard | - | 300s | Git commit history distillation via Ellie -> raw/inbox (1:45am, after gmail_ingest) |
| `healthcheck` | `cron(hour=9, minute=0)` | standard | - | 300s | System + service health capture |
| `shell_ingest` | `cron(hour=2, minute=15)` | standard | - | 300s | Shell history -> experience collection |

### jenna (9 jobs)

| Name | Trigger | Budget | Tags | Misfire Grace | Description |
|---|---|---|---|---|---|
| `active_contacts_ingest` | `cron(day=1, hour=4, minute=0)` | standard | - | 300s | Active iMessage contacts via Jenna -> raw/inbox (monthly) |
| `claude_code_sessions_ingest` | `cron(hour=1, minute=15)` | heavy | llm, qdrant | 300s | Claude Code session distillation via Jenna -> raw/inbox |
| `daily_synthesis` | `cron(hour=21, minute=0)` | standard | llm | 300s | Daily narrative + reflection Q (Jenna) |
| `eval_holdout_audit` | `cron(day_of_week=sun, hour=9, minute=15)` | standard | - | 900s | Phase C2: Telegram digest of >=14d stuck candidates only (Sun 9:15am) |
| `gmail_ingest` | `cron(hour=1, minute=30)` | standard | - | 300s | Gmail signal classifier -> raw/inbox |
| `obsidian_sync` | `interval(1:00:00)` | standard | - | 300s | Obsidian vault ↔ CouchDB pull |
| `openclaw_sessions_ingest` | `cron(hour=0,3,6,19,21,23, minute=35)` | heavy | llm, qdrant | 300s | OpenClaw agent session distillation via Jenna -> raw/inbox (6x/day off-peak, respects 9am-6pm no-local-embedder rule) |
| `personal_ingest` | `cron(hour=6,14,22, minute=0)` | heavy | embedder, qdrant | 300s | Apple Notes + iMessage + Calendar + Reminders -> Qdrant personal (3x daily off-peak) |
| `recall_judge` | `cron(hour=4, minute=27)` | heavy | llm, qdrant, sqlite | 900s | Daily 4:27am — sample 30 recent recalls, LLM-judges relevance/groundedness via live re-recall, writes recall_judgments + back-fills action_audit.outcome (judged_good/judged_wrong). |

### market (1 job)

| Name | Trigger | Budget | Tags | Misfire Grace | Description |
|---|---|---|---|---|---|
| `ghost_blog_ingest` | `cron(hour=5, minute=0)` | standard | - | 300s | Ghost blog posts via Admin API -> knowledge collection |

### sage (13 jobs)

| Name | Trigger | Budget | Tags | Misfire Grace | Description |
|---|---|---|---|---|---|
| `brain_reflect` | `cron(hour=2, minute=45)` | heavy | llm, qdrant | 900s | Nightly Sage pattern/contradiction pass over last 7d of semantic_memory |
| `browser_ingest` | `cron(hour=2, minute=30)` | standard | - | 300s | Browser history -> experience collection |
| `canonical_merge_draft` | `cron(day_of_week=sun, hour=6, minute=15)` | heavy | llm, qdrant | 1800s | Weekly top-3 compaction cluster Sage drafts (Sunday 6:15am, after compaction report) |
| `community_summaries` | `cron(day_of_week=sun, hour=5, minute=0)` | heavy | llm, neo4j | 1800s | M8.5: Louvain community detection on entity graph + Sage summary per cluster (Sun 5:00am) |
| `counterfactual_replay` | `cron(hour=4, minute=45)` | standard | - | 1800s | Daily counterfactual what-if replay on top failed decision (D9, codex subscription) |
| `dream_replay` | `cron(hour=3, minute=48)` | heavy | llm, qdrant | 1800s | Nightly REM-like generative conjecture synthesis (03:48 PT - staggered off memory_consolidation @03:45 which contends for local embedder/Qdrant) |
| `entity_pages` | `cron(day_of_week=sun, hour=4, minute=33)` | heavy | llm, neo4j | 1800s | Weekly entity page generator - Sage synthesizes one hot entity per run (Sunday 4:33am - staggered off session_rotate @04:30) |
| `monthly_synthesis` | `cron(day=1, hour=5, minute=0)` | heavy | llm | 300s | Monthly arc (Sage, 1st of month 5am) |
| `proactive_check` | `cron(hour=7,20,1, minute=30)` | standard | llm | 300s | Proactive insights - schedule gaps, contradictions, trends (3x daily, off work hours) |
| `profile_regen` | `cron(day_of_week=sun, hour=4, minute=0)` | heavy | llm, qdrant | 300s | Sage regenerates Chris profile from canonical knowledge (Sunday 4am) |
| `raptor_build` | `cron(day_of_week=sun, hour=7, minute=15)` | heavy | embedder, index, qdrant | 1800s | Weekly RAPTOR hierarchical summary tree (Sun 07:15) |
| `screen_time_ingest` | `cron(day_of_week=sun, hour=4, minute=35)` | standard | - | 300s | Screen Time daily patterns via Sage -> raw/inbox (weekly) |
| `weekly_synthesis` | `cron(day_of_week=sun, hour=4, minute=15)` | heavy | llm | 300s | Weekly arc (Sage, Sunday 4:15am) |

### system (126 jobs)

| Name | Trigger | Budget | Tags | Misfire Grace | Description |
|---|---|---|---|---|---|
| `action_audit_retention` | `cron(hour=4, minute=20)` | standard | - | 900s | Prune action_audit rows older than 90d (daily 4:20am) |
| `adversarial_memory_eval` | `cron(day_of_week=sun, hour=5, minute=5)` | medium | eval, memory, qdrant | 900s | Weekly adversarial memory eval for stale facts, multilingual recall, handoff state, and source coverage (Sun 05:05) |
| `answer_canonicalize` | `cron(hour=4, minute=2)` | heavy | llm, qdrant | 900s | Nightly query->canonical promoter (04:02am - staggered off sleep_consolidate @03:55 which contends for local embedder/LLM) |
| `apple_health_ingest` | `cron(hour=8, minute=0)` | standard | - | 900s | Apple Health daily recovery signal (sleep/HRV/RHR/kcal) -> raw/inbox (8:00am, after iPhone 7:30 Shortcut + iCloud sync) |
| `atom_recall_quality` | `cron(hour=4, minute=35)` | standard | - | 900s | Daily per-atom recall accuracy aggregation (D7 predictive coding signal) |
| `atoms_to_skills` | `cron(day_of_week=sun, hour=4, minute=58)` | heavy | llm, sqlite | 900s | Promote high-confidence atoms -> domain Claude Code skills (Sun 04:58 - staggered off llm_usage_purge @4:55) |
| `auto_resolve_contradictions` | `cron(hour=6, minute=0)` | standard | - | 900s | Daily auto-resolve stale/low-confidence contradictions (6:00am) - v3 bumped from weekly to daily after finding 20-item pending backlog that should have been closed overnight |
| `autonomy_decisions_retention` | `cron(hour=4, minute=35)` | standard | - | 900s | Prune autonomy_decisions rows older than 14d (daily 4:35am) |
| `autonomy_proposer` | `cron(hour=4, minute=45)` | standard | - | 300s | Phase 7: surface autonomy level promote/demote proposals (4:45am) |
| `backup_restore_drill` | `cron(day_of_week=sat, hour=4, minute=35)` | standard | backup, neo4j, qdrant, sqlite | 900s | Weekly backup restore-readiness drill (SQLite integrity + Qdrant temp restore + Neo4j archive validation) |
| `backup_verify` | `cron(day=1, hour=4, minute=45)` | heavy | backup, qdrant, sqlite | 900s | Monthly Qdrant backup restore smoke test (1st of month, 4:45am - staggered off llm_usage_retention @04:30 which also touches SQLite / MinIO) |
| `brain_doctor_daily` | `cron(hour=5, minute=0)` | standard | http, sqlite | 900s | Write brain-doctor health snapshot to logs/brain_doctor_daily.json (daily 5:00am) |
| `brain_loop_tick` | `interval(0:01:30)` | standard | - | 30s | v3: brain_loop executive cortex tick (every 90s — relaxed from 60s 2026-04-22 to cut 33% of ticks) |
| `brain_speak_digest` | `cron(hour=7, minute=55)` | standard | - | 300s | Brain's morning digest to Chris — drives observe, composer ranks, top 3 via Telegram (07:55 PT, scheduler runs in local tz). |
| `brain_speak_urgent` | `cron(minute=*/5)` | standard | - | 300s | Every 5 min: scan drives for severity>=7.5 observations, write to active Claude Code session doorbells. This is brain's interrupt channel. |
| `canonical_compaction` | `cron(day_of_week=sun, hour=6, minute=0)` | heavy | qdrant, sqlite | 1800s | Weekly compaction candidate clustering report (Sunday 6:00am, after canonical_lint) |
| `canonical_design_drift` | `cron(day_of_week=sun, hour=5, minute=25)` | standard | - | 900s | v3: weekly design source vs canonical mirror SHA check (Sun 05:25 - off db_vacuum_weekly @5:30 to avoid VACUUM lock contention) |
| `canonical_index` | `cron(day_of_week=sun, hour=4, minute=45)` | standard | - | 300s | Rebuild canonical knowledge index.md (weekly Sunday 4:45am, no LLM) |
| `canonical_lint` | `cron(day_of_week=sun, hour=5, minute=45)` | standard | - | 900s | Weekly structural lint: orphan canonical notes (Sunday 5:45am) |
| `canonical_pipeline` | `cron(hour=2,7,22, minute=0)` | heavy | qdrant, sqlite | 900s | Automated canonical promotion (3x daily: 02:00 / 07:00 / 22:00 PT) |
| `canonical_quality_filter_report` | `cron(day_of_week=sun, hour=6, minute=35)` | standard | - | 900s | Weekly quality filter dry-run report (Sunday 6:35am, review only) |
| `canonical_quality_triage` | `cron(day_of_week=sun, hour=7, minute=0)` | heavy | llm, sqlite | 1800s | LLM classifies score=2 canonical_quality items as archive/keep/uncertain |
| `canonical_staleness_check` | `cron(hour=4, minute=30)` | standard | - | 900s | Daily 04:30 PT: scan distilled/*.md for invalidated code claims and active canonical notes for stale current-truth supersession claims. Retire fixed-bug files and fail on current-truth blockers so brain stops surfacing obsolete facts. |
| `canonicalize_entities_dryrun` | `cron(day_of_week=sun, hour=6, minute=45)` | heavy | embedder, neo4j | 900s | v3: weekly entity dedup proposal scan (Sun 06:45, dry-run) |
| `code_index_refresh` | `cron(hour=3, minute=35)` | heavy | embedder, index, qdrant | 1200s | Daily incremental code function indexer (3:35am - staggered off sm2_nightly at 03:25) |
| `confidence_calibration` | `cron(day_of_week=sun, hour=4, minute=10)` | heavy | eval, sqlite | 900s | Weekly Platt calibration of atoms.confidence vs eval outcomes (Sun 04:10) |
| `config_secret_audit` | `cron(hour=6, minute=47)` | light | config, secrets | 900s | Daily safe audit of required Brain/OpenClaw config and secret sources without printing values (06:47 PT) |
| `conjecture_validate` | `cron(hour=4, minute=25)` | standard | - | 900s | Daily validation pass over dream_replay conjectures (promote with evidence, expire after 21d barren) |
| `content_quality_slo` | `cron(hour=4, minute=5)` | standard | - | 300s | Daily content quality SLO check (4:00am, after eval_run) |
| `contextual_embed_weekly` | `cron(day_of_week=sun, hour=5, minute=10)` | heavy | embedder, index, qdrant | 1800s | T2.12: re-embed canonical chunks with Anthropic-style per-doc context prefix (Sun 5:10am - staggered off community_summaries @5:00) |
| `crag_correction_regression` | `cron(hour=7, minute=7)` | standard | crag, eval, qdrant | 900s | Daily CRAG correction-quality gate over deterministic rewrite/recovery holdout (07:07 PT) |
| `crag_llm_correction_sample` | `cron(day_of_week=sun, hour=7, minute=12)` | heavy | crag, eval, llm, qdrant | 1800s | Weekly CRAG live LLM rewrite sample over correction holdout (Sun 07:12 PT) |
| `crag_regression` | `cron(hour=7, minute=2)` | standard | eval, qdrant | 900s | Daily CRAG retrieval-confidence safety gate over stable eval queries (07:02 PT) |
| `cross_agent_lessons` | `cron(hour=5, minute=10)` | standard | - | 300s | Daily 5:10am — scan atoms from last 48h for cross-agent lesson signals (failure/correction keywords + named agents). Flags atoms.lesson_candidate=1 + lesson_agents list so skill_materializer can seed procedural skills from them. |
| `db_vacuum_weekly` | `cron(day_of_week=sun, hour=5, minute=30)` | heavy | sqlite | 1800s | Weekly VACUUM + ANALYZE on brain.db/autonomy.db/llm_usage.db (Sun 5:30am) |
| `docker_volumes_backup_retention` | `cron(hour=4, minute=24)` | light | backup | 900s | Daily 4:24am — keep newest 7 daily tarballs per docker-volumes family |
| `embed_cache_prune` | `cron(hour=4, minute=8)` | standard | - | 900s | Prune embed cache: drop legacy rows, age >30d, cap 15k (daily 4:08am - staggered off content_quality_slo @4:05) |
| `embed_finetune` | `cron(day_of_week=sat, hour=23, minute=30)` | heavy | embedder, training | 3600s | Phase N3: weekly LoRA training on accumulated feedback pairs (Sat 23:30) |
| `entity_reconcile` | `cron(hour=2, minute=55)` | heavy | embedder, neo4j, sqlite | 1800s | v3: nightly catch-up for atoms with missing entity extraction (02:55) |
| `entity_resolution` | `cron(hour=3, minute=5)` | heavy | embedder, neo4j | 900s | Nightly entity merge: embedding similarity + co-occurrence (3:05am) |
| `entry_contract_audit` | `cron(hour=6, minute=37)` | standard | qdrant | 900s | Daily live Qdrant v2 entry-contract coverage audit (06:37 PT) |
| `episode_binder` | `cron(hour=3, minute=18)` | heavy | embedder, qdrant, sqlite | 900s | Daily episode clustering + Hebbian boost (3:18am, after entity_resolution) |
| `eval_holdout_graduate` | `cron(day_of_week=sun, hour=7, minute=30)` | standard | - | 900s | Phase N3: auto-graduate consistently-passing holdout candidates (Sun 7:30am) |
| `eval_holdout_promote` | `cron(day_of_week=sun, hour=8, minute=45)` | standard | - | 900s | Phase C1: novelty-score eval candidates, promote top-N to pending file (Sun 8:45am) |
| `eval_proposal_triage` | `cron(hour=4, minute=25)` | standard | llm, sqlite | 900s | CLI codex auto-approves/rejects candidate eval_proposals (daily 4:25am - staggered off action_audit_retention @4:20 to avoid autonomy.db lock contention) |
| `eval_run` | `cron(hour=3, minute=30)` | heavy | embedder, eval, qdrant | 900s | Stable-track eval (daily 3:30am) - strict 5pt gate, heal on regression |
| `eval_run_extended` | `cron(hour=3, minute=50)` | heavy | embedder, eval, qdrant | 900s | Extended-track eval (daily 3:50am) - loose-content trend only, no heal, 10pt threshold |
| `event_compressor` | `cron(day=1, hour=4, minute=20)` | standard | - | 1800s | Monthly event compression for old experience events (1st of month, 4:20am) |
| `feedback_aggregate` | `cron(day_of_week=sun, hour=6, minute=30)` | standard | - | 900s | Weekly search feedback aggregation (Sun 6:30am) |
| `focus_aggregate` | `cron(hour=4, minute=35)` | standard | - | 600s | Daily energy/focus data layer aggregation (4:35am) |
| `gap_detection` | `cron(day_of_week=sun, hour=9, minute=0)` | standard | - | 900s | Weekly knowledge gap detection from recall failures (Sunday 9:00am) |
| `goal_subtask_scaffold_brain_quality` | `cron(hour=4, minute=34)` | light | sqlite | 900s | Daily 4:34am — ensure the top brain-quality goal has measurable subtasks (no LLM) |
| `graph_backfill_co_mention` | `cron(day_of_week=sun, hour=3, minute=40)` | heavy | neo4j, sqlite | 900s | Weekly co-occurrence RELATES_TO backfill from shared MemoryAccess (Sunday 3:40am) |
| `graph_consolidation` | `cron(hour=2, minute=50)` | standard | - | 900s | Nightly graph sleep: decay, prune, promote, cluster (2:50am) |
| `graph_rebuild_mentions` | `cron(day_of_week=sun, hour=3, minute=30)` | heavy | neo4j, sqlite | 1800s | Weekly rebuild of atom->entity MENTIONS edges in Neo4j (Sunday 3:30am) |
| `habituation_prune` | `cron(hour=3, minute=20)` | standard | - | 300s | Drop attention_queue rows with shown_count >= 300 (daily 3:20am) |
| `hnsw_adaptive` | `cron(day_of_week=sun, hour=4, minute=50)` | heavy | eval, qdrant | 900s | Weekly adaptive HNSW ef_search tuning (Sunday 4:50am) |
| `holdout_rotation_eval` | `cron(day_of_week=sun, hour=5, minute=18)` | medium | eval, holdout, qdrant | 900s | Weekly rotating holdout retrieval eval disjoint from generated-answer RAGAS seed (Sun 05:18) |
| `image_ingest` | `cron(hour=5, minute=45)` | heavy | embedder, qdrant | 1800s | M7-WS2b: scan ~/Pictures/brain-ingest, OCR via Docling, embed captions -> knowledge |
| `infra_validation` | `cron(day_of_week=sun, hour=7, minute=10)` | standard | - | 300s | Weekly infra fact cross-check against live state (Sunday 7:10am - staggered off raptor_build @7:15 which is heavy LLM) |
| `intent_miss_scan` | `cron(hour=3, minute=28)` | standard | - | 900s | v3: scan active_recall misses via correction regex (daily 3:28am) |
| `kuma_heartbeats_ingest` | `cron(hour=6, minute=0)` | standard | - | 900s | Uptime Kuma incident state-changes -> raw/inbox (daily 6:00am, 24h window) |
| `lint_memory` | `cron(day_of_week=sun, hour=5, minute=35)` | standard | - | 900s | Weekly memory lint pass (Sunday 5:35am - staggered off canonical_design_drift at 05:30) |
| `live_state_snapshot` | `interval(0:10:00)` | standard | - | 120s | v3: snapshot current docker/launchd/goals/commits/sessions state (every 10min) |
| `llm_backlog_drain` | `interval(0:30:00)` | standard | llm | 300s | v3: LLM backlog catch-up queue drain (every 30 min) |
| `llm_usage_purge` | `cron(day_of_week=sun, hour=4, minute=55)` | standard | - | 900s | Weekly purge of llm_usage.db >90 days (Sun 4:55am) |
| `llm_usage_retention` | `cron(day=1, hour=4, minute=30)` | standard | - | 1800s | Roll up llm_usage older than 90d into llm_usage_monthly (1st of month 4:30am) |
| `log_rotation` | `cron(hour=4, minute=0)` | standard | - | 300s | Truncate job/server logs >3d or >512KB (keeps last 100 lines) |
| `lora_ab_gate` | `cron(day_of_week=sun, hour=9, minute=30)` | heavy | embedder, eval, qdrant | 1800s | Phase 7: weekly LoRA A/B gate + deploy (Sun 9:30am) |
| `ltr_train` | `cron(day_of_week=sun, hour=4, minute=20)` | heavy | qdrant, training | 900s | Weekly LogisticRegression LtR fit on recall feedback (Sun 04:20) |
| `memory_consolidation` | `cron(hour=3, minute=45)` | standard | - | 900s | Nightly memory tier promotion/demotion (3:45am, Phase 1D) |
| `memory_health_report` | `cron(day_of_week=sun, hour=7, minute=35)` | standard | - | 300s | Weekly memory health report (Sunday 7:35am - staggered off eval_holdout_graduate @7:30) |
| `memory_leak_detector` | `cron(day_of_week=sun, hour=5, minute=50)` | standard | - | 900s | Weekly memory leak detection (Sunday 5:50am - staggered off canonical_lint at 05:45) |
| `memory_lifecycle` | `cron(day_of_week=sun, hour=2, minute=30)` | heavy | qdrant, sqlite | 300s | Age out + promote durable semantic memories (Sunday 2:30am) |
| `memory_nudge` | `cron(day_of_week=sun, hour=6, minute=50)` | standard | - | 900s | Weekly memory review nudge (Sunday 6:50am - staggered off canonicalize_entities_dryrun at 06:45) |
| `memory_observability` | `cron(day_of_week=sun, hour=5, minute=20)` | standard | - | 900s | Weekly memory observability report (Sunday 5:20am - staggered off community_summaries @5:00 / contextual_embed @5:10) |
| `memory_provenance_lint` | `cron(hour=6, minute=25)` | standard | - | 900s | Daily read-only lint of canonical/distilled provenance and supersession metadata (06:25 PT) |
| `memory_pruning` | `cron(day=15, hour=4, minute=10)` | heavy | qdrant, sqlite | 1800s | Monthly atrophied-memory dry-run (15th 4:10am) |
| `memory_pruning_active` | `cron(day=15, hour=5, minute=15)` | heavy | qdrant, sqlite | 1800s | Monthly REAL atrophied-memory pruning (15th 5:15am, 1h after dry-run, dry_run=False) |
| `metric_trend_snapshot` | `cron(hour=4, minute=38)` | light | sqlite | 900s | Daily 4:38am — append today's brain-quality metric vector for 7d-drift alerts (no LLM) |
| `metrics_history_retention` | `cron(hour=4, minute=40)` | standard | - | 900s | Prune metrics_snapshots rows older than 14d (daily 4:40am) |
| `near_dedup` | `cron(hour=3, minute=22)` | heavy | embedder, qdrant, sqlite | 300s | Daily retroactive near-duplicate scan of semantic_memory (3:22am). Bumped weekly->daily 2026-04-23 after bilingual preference atoms accumulated past the weekly gate. Moved off 3:20 to avoid collision with habituation_prune and off 3:25 to avoid sm2_nightly brain.db/Qdrant contention. |
| `neo4j_backup` | `cron(hour=3, minute=15)` | heavy | backup, neo4j | 300s | Nightly Neo4j data backup to MinIO (14-day retention) |
| `obsolete_expired_atoms` | `cron(hour=4, minute=50)` | standard | - | 900s | Mark superseded+expired+unaccessed atoms tier=obsolete (daily 4:50am, 60d window) |
| `openclaw_telegram_target_audit` | `cron(hour=6, minute=42)` | light | openclaw, telegram | 900s | Daily audit that OpenClaw Telegram cron delivery uses Chris's numeric chat id (06:42 PT) |
| `outbox_drain` | `interval(0:05:00)` | standard | - | 120s | Phase 2D: drain SessionEnd outbox envelopes (every 5 min) |
| `outcome_feedback_review` | `cron(hour=4, minute=32)` | light | sqlite | 900s | Daily 4:32am — surface chris_override patterns as review tasks (no policy mutation, no LLM) |
| `pdf_ingest` | `cron(hour=5, minute=30)` | heavy | embedder, qdrant | 1800s | M7-WS2a: scan ~/Documents/PDFs, parse via Docling, embed -> knowledge |
| `privacy_negative_audit` | `cron(hour=6, minute=39)` | standard | privacy, qdrant | 900s | Daily personal-source privacy negative sample audit without printing content (06:39 PT) |
| `proactive_insights` | `cron(hour=8, minute=0)` | standard | - | 900s | Daily proactive insights surfacing (8:00am PST) |
| `prompt_survival_report` | `cron(day_of_week=sun, hour=5, minute=38)` | standard | - | 300s | Weekly Sun 5:38am — per-prompt 7-day atom survival rate. Substrate for prompt A/B: produce two prompt_versions in parallel, this report shows which one's atoms the system kept. Slot picked to dodge db_vacuum_weekly (Sun 5:30am exclusive lock on brain.db). |
| `prune_raw_orphaned` | `cron(month=1,4,7,10, day=1, hour=4, minute=25)` | standard | - | 1800s | Quarterly raw/orphaned prune (180d retention; 1st of Jan/Apr/Jul/Oct @ 04:25) |
| `qdrant_write_audit` | `cron(hour=6, minute=32)` | light | - | 900s | Daily source audit: fail on raw qdrant_client mutating writes outside approved boundaries (06:32 PT) |
| `ragas_eval_gate` | `cron(day_of_week=sun, hour=4, minute=45)` | heavy | eval, llm, qdrant | 1800s | Weekly generated-answer RAGAS faithfulness/relevance gate over answer-oriented eval set (Sun 04:45) |
| `raw_events_retention` | `cron(hour=4, minute=22)` | standard | sqlite | 900s | Prune unreferenced raw_events older than 14d (daily 4:22am) |
| `re_examine_rejected` | `cron(day=2, hour=4, minute=30)` | heavy | qdrant, sqlite | 1800s | Monthly rejected-proposal re-examination (2nd of month @ 04:30) |
| `recall_outcome_label` | `cron(minute=17)` | standard | - | 300s | Hourly — mark action_audit recalls 'restated' when same session re-asks within 120s (cosine ≥0.85). Converts the ~24k/week pending recall signal into training data. |
| `recall_structural_judge_hourly` | `cron(minute=47)` | light | sqlite | 600s | Every hour at :47 — deterministically score unlabeled /recall outcomes (no LLM) |
| `reindex` | `cron(hour=3,23, minute=17)` | heavy | embedder, index, qdrant | 900s | Full Qdrant reindex (2x daily, off-hours) |
| `release_readiness` | `cron(hour=6, minute=52)` | light | git, release | 900s | Daily non-mutating release hygiene snapshot for changed-file lanes and required evidence (06:52 PT) |
| `retrieval_inhibition` | `cron(hour=3, minute=58)` | standard | qdrant, sqlite | 600s | Nightly Bjork-style inhibition of consistent retrieval losers (03:58am) |
| `retrieval_regression` | `cron(hour=6, minute=57)` | standard | eval, qdrant | 900s | Daily bounded retrieval regression gate over stable eval queries (06:57 PT) |
| `review_task_dispatcher` | `cron(hour=6, minute=30)` | standard | llm, openclaw | 900s | Daily 6:30am — dispatch up to 2 brain-generated review tasks to Sage |
| `schema_learner` | `cron(day_of_week=sun, hour=4, minute=40)` | heavy | llm, sqlite | 900s | CLS spectral clustering on atom coactivation -> compaction candidates (Sun 04:40) |
| `schema_revision` | `cron(day_of_week=sun, hour=8, minute=50)` | heavy | llm, sqlite | 900s | Weekly free-energy schema revision (Sun 08:50 - staggered off eval_holdout_promote @8:45) |
| `self_eval` | `cron(hour=3, minute=37)` | heavy | embedder, eval, qdrant | 900s | Nightly 03:37 PT: sample recent /recall queries, re-run, measure top-3 overlap drift. Populates self_eval_drift_7d SLO. |
| `self_model_regen` | `cron(hour=5, minute=25)` | standard | - | 900s | Nightly DMN-like unified self-model atom regen (05:25 PT) |
| `session_context_retention` | `cron(hour=4, minute=43)` | standard | - | 900s | Prune orphaned session_context rows older than 30d (daily 4:43am) |
| `session_rotate` | `cron(day_of_week=sun, hour=4, minute=30)` | standard | - | 900s | Weekly: archive old agent session checkpoints; alert on oversized live sessions (Sun 4:30am) |
| `skill_extract` | `cron(day_of_week=sun, hour=7, minute=45)` | heavy | llm, sqlite | 900s | Weekly skill graph indexing (Sunday 7:45am) |
| `skill_materialize_cleanup` | `cron(hour=4, minute=10)` | standard | - | 900s | T2.10: archive orphaned/stale auto-* SKILL.md files; enforce MAX_AUTO_SKILLS cap (daily 4:10am) |
| `skill_sync` | `cron(day_of_week=sun, hour=7, minute=50)` | standard | - | 900s | Reconcile ~/.openclaw/skills disk ↔ openclaw.json entries + agent attach (Sunday 7:50am, after skill_extract) |
| `sleep_consolidate` | `cron(hour=3, minute=55)` | heavy | qdrant, sqlite | 900s | CLS sleep consolidation: coactivation + A-MEM + promotion (3:55am, Phase N4) |
| `slo_monitor` | `cron(minute=30)` | standard | - | 300s | Hourly SLO check with Telegram alerts on 3+ violations |
| `slos_check` | `interval(0:05:00)` | standard | - | 120s | Phase E1: SLO budget check + Telegram alert on breach (every 5 min) |
| `sm2_nightly` | `cron(hour=3, minute=25)` | standard | - | 900s | SM-2 nightly: seed next_review_at + obsolete stale atoms (3:25am) |
| `stale_cleanup` | `cron(day_of_week=sun, hour=3, minute=10)` | standard | - | 300s | Weekly incremental stale doc cleanup across collections (Sun 3:10am) |
| `stale_superseded_cleanup` | `cron(day_of_week=sun, hour=6, minute=20)` | standard | - | 900s | Weekly stale superseded memory cleanup (Sun 6:20am - staggered off canonical_merge_draft at 06:15) |
| `subtask_evaluator_brain_quality` | `cron(hour=4, minute=36)` | light | sqlite | 900s | Daily 4:36am — auto-complete brain-quality subtasks whose metric cleared target (no LLM) |
| `supersession_chain_cleanup` | `cron(day_of_week=sun, hour=6, minute=10)` | standard | - | 300s | Weekly cleanup of orphaned supersession chains (Sun 6:10am) |
| `training_pairs_generate` | `cron(day_of_week=sun, hour=8, minute=0)` | standard | qdrant, training | 900s | Weekly training pair generation from feedback (Sunday 8:00am) |
| `trust_recompute` | `cron(day_of_week=sun, hour=7, minute=5)` | heavy | embedder, qdrant | 900s | Weekly cross-source corroboration trust score refresh (Sunday 7:05am - staggered off canonical_quality_triage @7:00) |
| `ui_parity_audit` | `cron(hour=6, minute=54)` | light | readiness, ui | 900s | Daily static API-to-UI parity audit for world-level Brain dashboard coverage (06:54 PT) |
| `wal_checkpoint_daily` | `cron(hour=4, minute=55)` | standard | sqlite | 900s | Daily PRAGMA wal_checkpoint(TRUNCATE) on hot DBs (4:55am) |
| `wal_checkpoint_intraday` | `cron(hour=0,4,8,12,16,20, minute=35)` | light | sqlite | 600s | Intra-day WAL checkpoint(TRUNCATE) on hot DBs (every 4h at :35) |
| `web_source_trust_recompute` | `cron(day_of_week=sun, hour=5, minute=15)` | standard | - | 900s | Phase M6: recompute per-domain web search trust scores (Sun 5:15) |

## All jobs

| Name | Trigger | Agent | Budget | Tags | Misfire Grace | Description |
|---|---|---|---|---|---|---|
| `action_audit_retention` | `cron(hour=4, minute=20)` | system | standard | - | 900s | Prune action_audit rows older than 90d (daily 4:20am) |
| `active_contacts_ingest` | `cron(day=1, hour=4, minute=0)` | jenna | standard | - | 300s | Active iMessage contacts via Jenna -> raw/inbox (monthly) |
| `adversarial_memory_eval` | `cron(day_of_week=sun, hour=5, minute=5)` | system | medium | eval, memory, qdrant | 900s | Weekly adversarial memory eval for stale facts, multilingual recall, handoff state, and source coverage (Sun 05:05) |
| `answer_canonicalize` | `cron(hour=4, minute=2)` | system | heavy | llm, qdrant | 900s | Nightly query->canonical promoter (04:02am - staggered off sleep_consolidate @03:55 which contends for local embedder/LLM) |
| `apple_health_ingest` | `cron(hour=8, minute=0)` | system | standard | - | 900s | Apple Health daily recovery signal (sleep/HRV/RHR/kcal) -> raw/inbox (8:00am, after iPhone 7:30 Shortcut + iCloud sync) |
| `atom_recall_quality` | `cron(hour=4, minute=35)` | system | standard | - | 900s | Daily per-atom recall accuracy aggregation (D7 predictive coding signal) |
| `atoms_to_skills` | `cron(day_of_week=sun, hour=4, minute=58)` | system | heavy | llm, sqlite | 900s | Promote high-confidence atoms -> domain Claude Code skills (Sun 04:58 - staggered off llm_usage_purge @4:55) |
| `auto_resolve_contradictions` | `cron(hour=6, minute=0)` | system | standard | - | 900s | Daily auto-resolve stale/low-confidence contradictions (6:00am) - v3 bumped from weekly to daily after finding 20-item pending backlog that should have been closed overnight |
| `autonomy_decisions_retention` | `cron(hour=4, minute=35)` | system | standard | - | 900s | Prune autonomy_decisions rows older than 14d (daily 4:35am) |
| `autonomy_proposer` | `cron(hour=4, minute=45)` | system | standard | - | 300s | Phase 7: surface autonomy level promote/demote proposals (4:45am) |
| `backup_restore_drill` | `cron(day_of_week=sat, hour=4, minute=35)` | system | standard | backup, neo4j, qdrant, sqlite | 900s | Weekly backup restore-readiness drill (SQLite integrity + Qdrant temp restore + Neo4j archive validation) |
| `backup_verify` | `cron(day=1, hour=4, minute=45)` | system | heavy | backup, qdrant, sqlite | 900s | Monthly Qdrant backup restore smoke test (1st of month, 4:45am - staggered off llm_usage_retention @04:30 which also touches SQLite / MinIO) |
| `brain_doctor_daily` | `cron(hour=5, minute=0)` | system | standard | http, sqlite | 900s | Write brain-doctor health snapshot to logs/brain_doctor_daily.json (daily 5:00am) |
| `brain_loop_tick` | `interval(0:01:30)` | system | standard | - | 30s | v3: brain_loop executive cortex tick (every 90s — relaxed from 60s 2026-04-22 to cut 33% of ticks) |
| `brain_reflect` | `cron(hour=2, minute=45)` | sage | heavy | llm, qdrant | 900s | Nightly Sage pattern/contradiction pass over last 7d of semantic_memory |
| `brain_speak_digest` | `cron(hour=7, minute=55)` | system | standard | - | 300s | Brain's morning digest to Chris — drives observe, composer ranks, top 3 via Telegram (07:55 PT, scheduler runs in local tz). |
| `brain_speak_urgent` | `cron(minute=*/5)` | system | standard | - | 300s | Every 5 min: scan drives for severity>=7.5 observations, write to active Claude Code session doorbells. This is brain's interrupt channel. |
| `browser_ingest` | `cron(hour=2, minute=30)` | sage | standard | - | 300s | Browser history -> experience collection |
| `canonical_compaction` | `cron(day_of_week=sun, hour=6, minute=0)` | system | heavy | qdrant, sqlite | 1800s | Weekly compaction candidate clustering report (Sunday 6:00am, after canonical_lint) |
| `canonical_design_drift` | `cron(day_of_week=sun, hour=5, minute=25)` | system | standard | - | 900s | v3: weekly design source vs canonical mirror SHA check (Sun 05:25 - off db_vacuum_weekly @5:30 to avoid VACUUM lock contention) |
| `canonical_index` | `cron(day_of_week=sun, hour=4, minute=45)` | system | standard | - | 300s | Rebuild canonical knowledge index.md (weekly Sunday 4:45am, no LLM) |
| `canonical_lint` | `cron(day_of_week=sun, hour=5, minute=45)` | system | standard | - | 900s | Weekly structural lint: orphan canonical notes (Sunday 5:45am) |
| `canonical_merge_draft` | `cron(day_of_week=sun, hour=6, minute=15)` | sage | heavy | llm, qdrant | 1800s | Weekly top-3 compaction cluster Sage drafts (Sunday 6:15am, after compaction report) |
| `canonical_pipeline` | `cron(hour=2,7,22, minute=0)` | system | heavy | qdrant, sqlite | 900s | Automated canonical promotion (3x daily: 02:00 / 07:00 / 22:00 PT) |
| `canonical_quality_filter_report` | `cron(day_of_week=sun, hour=6, minute=35)` | system | standard | - | 900s | Weekly quality filter dry-run report (Sunday 6:35am, review only) |
| `canonical_quality_triage` | `cron(day_of_week=sun, hour=7, minute=0)` | system | heavy | llm, sqlite | 1800s | LLM classifies score=2 canonical_quality items as archive/keep/uncertain |
| `canonical_staleness_check` | `cron(hour=4, minute=30)` | system | standard | - | 900s | Daily 04:30 PT: scan distilled/*.md for invalidated code claims and active canonical notes for stale current-truth supersession claims. Retire fixed-bug files and fail on current-truth blockers so brain stops surfacing obsolete facts. |
| `canonicalize_entities_dryrun` | `cron(day_of_week=sun, hour=6, minute=45)` | system | heavy | embedder, neo4j | 900s | v3: weekly entity dedup proposal scan (Sun 06:45, dry-run) |
| `claude_code_sessions_ingest` | `cron(hour=1, minute=15)` | jenna | heavy | llm, qdrant | 300s | Claude Code session distillation via Jenna -> raw/inbox |
| `code_index_refresh` | `cron(hour=3, minute=35)` | system | heavy | embedder, index, qdrant | 1200s | Daily incremental code function indexer (3:35am - staggered off sm2_nightly at 03:25) |
| `community_summaries` | `cron(day_of_week=sun, hour=5, minute=0)` | sage | heavy | llm, neo4j | 1800s | M8.5: Louvain community detection on entity graph + Sage summary per cluster (Sun 5:00am) |
| `confidence_calibration` | `cron(day_of_week=sun, hour=4, minute=10)` | system | heavy | eval, sqlite | 900s | Weekly Platt calibration of atoms.confidence vs eval outcomes (Sun 04:10) |
| `config_secret_audit` | `cron(hour=6, minute=47)` | system | light | config, secrets | 900s | Daily safe audit of required Brain/OpenClaw config and secret sources without printing values (06:47 PT) |
| `conjecture_validate` | `cron(hour=4, minute=25)` | system | standard | - | 900s | Daily validation pass over dream_replay conjectures (promote with evidence, expire after 21d barren) |
| `content_quality_slo` | `cron(hour=4, minute=5)` | system | standard | - | 300s | Daily content quality SLO check (4:00am, after eval_run) |
| `contextual_embed_weekly` | `cron(day_of_week=sun, hour=5, minute=10)` | system | heavy | embedder, index, qdrant | 1800s | T2.12: re-embed canonical chunks with Anthropic-style per-doc context prefix (Sun 5:10am - staggered off community_summaries @5:00) |
| `counterfactual_replay` | `cron(hour=4, minute=45)` | sage | standard | - | 1800s | Daily counterfactual what-if replay on top failed decision (D9, codex subscription) |
| `crag_correction_regression` | `cron(hour=7, minute=7)` | system | standard | crag, eval, qdrant | 900s | Daily CRAG correction-quality gate over deterministic rewrite/recovery holdout (07:07 PT) |
| `crag_llm_correction_sample` | `cron(day_of_week=sun, hour=7, minute=12)` | system | heavy | crag, eval, llm, qdrant | 1800s | Weekly CRAG live LLM rewrite sample over correction holdout (Sun 07:12 PT) |
| `crag_regression` | `cron(hour=7, minute=2)` | system | standard | eval, qdrant | 900s | Daily CRAG retrieval-confidence safety gate over stable eval queries (07:02 PT) |
| `cross_agent_lessons` | `cron(hour=5, minute=10)` | system | standard | - | 300s | Daily 5:10am — scan atoms from last 48h for cross-agent lesson signals (failure/correction keywords + named agents). Flags atoms.lesson_candidate=1 + lesson_agents list so skill_materializer can seed procedural skills from them. |
| `daily_synthesis` | `cron(hour=21, minute=0)` | jenna | standard | llm | 300s | Daily narrative + reflection Q (Jenna) |
| `db_vacuum_weekly` | `cron(day_of_week=sun, hour=5, minute=30)` | system | heavy | sqlite | 1800s | Weekly VACUUM + ANALYZE on brain.db/autonomy.db/llm_usage.db (Sun 5:30am) |
| `docker_volumes_backup_retention` | `cron(hour=4, minute=24)` | system | light | backup | 900s | Daily 4:24am — keep newest 7 daily tarballs per docker-volumes family |
| `dream_replay` | `cron(hour=3, minute=48)` | sage | heavy | llm, qdrant | 1800s | Nightly REM-like generative conjecture synthesis (03:48 PT - staggered off memory_consolidation @03:45 which contends for local embedder/Qdrant) |
| `embed_cache_prune` | `cron(hour=4, minute=8)` | system | standard | - | 900s | Prune embed cache: drop legacy rows, age >30d, cap 15k (daily 4:08am - staggered off content_quality_slo @4:05) |
| `embed_finetune` | `cron(day_of_week=sat, hour=23, minute=30)` | system | heavy | embedder, training | 3600s | Phase N3: weekly LoRA training on accumulated feedback pairs (Sat 23:30) |
| `entity_pages` | `cron(day_of_week=sun, hour=4, minute=33)` | sage | heavy | llm, neo4j | 1800s | Weekly entity page generator - Sage synthesizes one hot entity per run (Sunday 4:33am - staggered off session_rotate @04:30) |
| `entity_reconcile` | `cron(hour=2, minute=55)` | system | heavy | embedder, neo4j, sqlite | 1800s | v3: nightly catch-up for atoms with missing entity extraction (02:55) |
| `entity_resolution` | `cron(hour=3, minute=5)` | system | heavy | embedder, neo4j | 900s | Nightly entity merge: embedding similarity + co-occurrence (3:05am) |
| `entry_contract_audit` | `cron(hour=6, minute=37)` | system | standard | qdrant | 900s | Daily live Qdrant v2 entry-contract coverage audit (06:37 PT) |
| `episode_binder` | `cron(hour=3, minute=18)` | system | heavy | embedder, qdrant, sqlite | 900s | Daily episode clustering + Hebbian boost (3:18am, after entity_resolution) |
| `eval_holdout_audit` | `cron(day_of_week=sun, hour=9, minute=15)` | jenna | standard | - | 900s | Phase C2: Telegram digest of >=14d stuck candidates only (Sun 9:15am) |
| `eval_holdout_graduate` | `cron(day_of_week=sun, hour=7, minute=30)` | system | standard | - | 900s | Phase N3: auto-graduate consistently-passing holdout candidates (Sun 7:30am) |
| `eval_holdout_promote` | `cron(day_of_week=sun, hour=8, minute=45)` | system | standard | - | 900s | Phase C1: novelty-score eval candidates, promote top-N to pending file (Sun 8:45am) |
| `eval_proposal_triage` | `cron(hour=4, minute=25)` | system | standard | llm, sqlite | 900s | CLI codex auto-approves/rejects candidate eval_proposals (daily 4:25am - staggered off action_audit_retention @4:20 to avoid autonomy.db lock contention) |
| `eval_run` | `cron(hour=3, minute=30)` | system | heavy | embedder, eval, qdrant | 900s | Stable-track eval (daily 3:30am) - strict 5pt gate, heal on regression |
| `eval_run_extended` | `cron(hour=3, minute=50)` | system | heavy | embedder, eval, qdrant | 900s | Extended-track eval (daily 3:50am) - loose-content trend only, no heal, 10pt threshold |
| `event_compressor` | `cron(day=1, hour=4, minute=20)` | system | standard | - | 1800s | Monthly event compression for old experience events (1st of month, 4:20am) |
| `feedback_aggregate` | `cron(day_of_week=sun, hour=6, minute=30)` | system | standard | - | 900s | Weekly search feedback aggregation (Sun 6:30am) |
| `focus_aggregate` | `cron(hour=4, minute=35)` | system | standard | - | 600s | Daily energy/focus data layer aggregation (4:35am) |
| `gap_detection` | `cron(day_of_week=sun, hour=9, minute=0)` | system | standard | - | 900s | Weekly knowledge gap detection from recall failures (Sunday 9:00am) |
| `ghost_blog_ingest` | `cron(hour=5, minute=0)` | market | standard | - | 300s | Ghost blog posts via Admin API -> knowledge collection |
| `git_activity_ingest` | `cron(hour=1, minute=45)` | ellie | standard | - | 300s | Git commit history distillation via Ellie -> raw/inbox (1:45am, after gmail_ingest) |
| `gmail_ingest` | `cron(hour=1, minute=30)` | jenna | standard | - | 300s | Gmail signal classifier -> raw/inbox |
| `goal_subtask_scaffold_brain_quality` | `cron(hour=4, minute=34)` | system | light | sqlite | 900s | Daily 4:34am — ensure the top brain-quality goal has measurable subtasks (no LLM) |
| `graph_backfill_co_mention` | `cron(day_of_week=sun, hour=3, minute=40)` | system | heavy | neo4j, sqlite | 900s | Weekly co-occurrence RELATES_TO backfill from shared MemoryAccess (Sunday 3:40am) |
| `graph_consolidation` | `cron(hour=2, minute=50)` | system | standard | - | 900s | Nightly graph sleep: decay, prune, promote, cluster (2:50am) |
| `graph_rebuild_mentions` | `cron(day_of_week=sun, hour=3, minute=30)` | system | heavy | neo4j, sqlite | 1800s | Weekly rebuild of atom->entity MENTIONS edges in Neo4j (Sunday 3:30am) |
| `habituation_prune` | `cron(hour=3, minute=20)` | system | standard | - | 300s | Drop attention_queue rows with shown_count >= 300 (daily 3:20am) |
| `healthcheck` | `cron(hour=9, minute=0)` | ellie | standard | - | 300s | System + service health capture |
| `hnsw_adaptive` | `cron(day_of_week=sun, hour=4, minute=50)` | system | heavy | eval, qdrant | 900s | Weekly adaptive HNSW ef_search tuning (Sunday 4:50am) |
| `holdout_rotation_eval` | `cron(day_of_week=sun, hour=5, minute=18)` | system | medium | eval, holdout, qdrant | 900s | Weekly rotating holdout retrieval eval disjoint from generated-answer RAGAS seed (Sun 05:18) |
| `image_ingest` | `cron(hour=5, minute=45)` | system | heavy | embedder, qdrant | 1800s | M7-WS2b: scan ~/Pictures/brain-ingest, OCR via Docling, embed captions -> knowledge |
| `infra_validation` | `cron(day_of_week=sun, hour=7, minute=10)` | system | standard | - | 300s | Weekly infra fact cross-check against live state (Sunday 7:10am - staggered off raptor_build @7:15 which is heavy LLM) |
| `intent_miss_scan` | `cron(hour=3, minute=28)` | system | standard | - | 900s | v3: scan active_recall misses via correction regex (daily 3:28am) |
| `kuma_heartbeats_ingest` | `cron(hour=6, minute=0)` | system | standard | - | 900s | Uptime Kuma incident state-changes -> raw/inbox (daily 6:00am, 24h window) |
| `lint_memory` | `cron(day_of_week=sun, hour=5, minute=35)` | system | standard | - | 900s | Weekly memory lint pass (Sunday 5:35am - staggered off canonical_design_drift at 05:30) |
| `live_state_snapshot` | `interval(0:10:00)` | system | standard | - | 120s | v3: snapshot current docker/launchd/goals/commits/sessions state (every 10min) |
| `llm_backlog_drain` | `interval(0:30:00)` | system | standard | llm | 300s | v3: LLM backlog catch-up queue drain (every 30 min) |
| `llm_usage_purge` | `cron(day_of_week=sun, hour=4, minute=55)` | system | standard | - | 900s | Weekly purge of llm_usage.db >90 days (Sun 4:55am) |
| `llm_usage_retention` | `cron(day=1, hour=4, minute=30)` | system | standard | - | 1800s | Roll up llm_usage older than 90d into llm_usage_monthly (1st of month 4:30am) |
| `log_rotation` | `cron(hour=4, minute=0)` | system | standard | - | 300s | Truncate job/server logs >3d or >512KB (keeps last 100 lines) |
| `lora_ab_gate` | `cron(day_of_week=sun, hour=9, minute=30)` | system | heavy | embedder, eval, qdrant | 1800s | Phase 7: weekly LoRA A/B gate + deploy (Sun 9:30am) |
| `ltr_train` | `cron(day_of_week=sun, hour=4, minute=20)` | system | heavy | qdrant, training | 900s | Weekly LogisticRegression LtR fit on recall feedback (Sun 04:20) |
| `memory_consolidation` | `cron(hour=3, minute=45)` | system | standard | - | 900s | Nightly memory tier promotion/demotion (3:45am, Phase 1D) |
| `memory_health_report` | `cron(day_of_week=sun, hour=7, minute=35)` | system | standard | - | 300s | Weekly memory health report (Sunday 7:35am - staggered off eval_holdout_graduate @7:30) |
| `memory_leak_detector` | `cron(day_of_week=sun, hour=5, minute=50)` | system | standard | - | 900s | Weekly memory leak detection (Sunday 5:50am - staggered off canonical_lint at 05:45) |
| `memory_lifecycle` | `cron(day_of_week=sun, hour=2, minute=30)` | system | heavy | qdrant, sqlite | 300s | Age out + promote durable semantic memories (Sunday 2:30am) |
| `memory_nudge` | `cron(day_of_week=sun, hour=6, minute=50)` | system | standard | - | 900s | Weekly memory review nudge (Sunday 6:50am - staggered off canonicalize_entities_dryrun at 06:45) |
| `memory_observability` | `cron(day_of_week=sun, hour=5, minute=20)` | system | standard | - | 900s | Weekly memory observability report (Sunday 5:20am - staggered off community_summaries @5:00 / contextual_embed @5:10) |
| `memory_provenance_lint` | `cron(hour=6, minute=25)` | system | standard | - | 900s | Daily read-only lint of canonical/distilled provenance and supersession metadata (06:25 PT) |
| `memory_pruning` | `cron(day=15, hour=4, minute=10)` | system | heavy | qdrant, sqlite | 1800s | Monthly atrophied-memory dry-run (15th 4:10am) |
| `memory_pruning_active` | `cron(day=15, hour=5, minute=15)` | system | heavy | qdrant, sqlite | 1800s | Monthly REAL atrophied-memory pruning (15th 5:15am, 1h after dry-run, dry_run=False) |
| `metric_trend_snapshot` | `cron(hour=4, minute=38)` | system | light | sqlite | 900s | Daily 4:38am — append today's brain-quality metric vector for 7d-drift alerts (no LLM) |
| `metrics_history_retention` | `cron(hour=4, minute=40)` | system | standard | - | 900s | Prune metrics_snapshots rows older than 14d (daily 4:40am) |
| `monthly_synthesis` | `cron(day=1, hour=5, minute=0)` | sage | heavy | llm | 300s | Monthly arc (Sage, 1st of month 5am) |
| `near_dedup` | `cron(hour=3, minute=22)` | system | heavy | embedder, qdrant, sqlite | 300s | Daily retroactive near-duplicate scan of semantic_memory (3:22am). Bumped weekly->daily 2026-04-23 after bilingual preference atoms accumulated past the weekly gate. Moved off 3:20 to avoid collision with habituation_prune and off 3:25 to avoid sm2_nightly brain.db/Qdrant contention. |
| `neo4j_backup` | `cron(hour=3, minute=15)` | system | heavy | backup, neo4j | 300s | Nightly Neo4j data backup to MinIO (14-day retention) |
| `obsidian_sync` | `interval(1:00:00)` | jenna | standard | - | 300s | Obsidian vault ↔ CouchDB pull |
| `obsolete_expired_atoms` | `cron(hour=4, minute=50)` | system | standard | - | 900s | Mark superseded+expired+unaccessed atoms tier=obsolete (daily 4:50am, 60d window) |
| `openclaw_sessions_ingest` | `cron(hour=0,3,6,19,21,23, minute=35)` | jenna | heavy | llm, qdrant | 300s | OpenClaw agent session distillation via Jenna -> raw/inbox (6x/day off-peak, respects 9am-6pm no-local-embedder rule) |
| `openclaw_telegram_target_audit` | `cron(hour=6, minute=42)` | system | light | openclaw, telegram | 900s | Daily audit that OpenClaw Telegram cron delivery uses Chris's numeric chat id (06:42 PT) |
| `outbox_drain` | `interval(0:05:00)` | system | standard | - | 120s | Phase 2D: drain SessionEnd outbox envelopes (every 5 min) |
| `outcome_feedback_review` | `cron(hour=4, minute=32)` | system | light | sqlite | 900s | Daily 4:32am — surface chris_override patterns as review tasks (no policy mutation, no LLM) |
| `pdf_ingest` | `cron(hour=5, minute=30)` | system | heavy | embedder, qdrant | 1800s | M7-WS2a: scan ~/Documents/PDFs, parse via Docling, embed -> knowledge |
| `personal_ingest` | `cron(hour=6,14,22, minute=0)` | jenna | heavy | embedder, qdrant | 300s | Apple Notes + iMessage + Calendar + Reminders -> Qdrant personal (3x daily off-peak) |
| `privacy_negative_audit` | `cron(hour=6, minute=39)` | system | standard | privacy, qdrant | 900s | Daily personal-source privacy negative sample audit without printing content (06:39 PT) |
| `proactive_check` | `cron(hour=7,20,1, minute=30)` | sage | standard | llm | 300s | Proactive insights - schedule gaps, contradictions, trends (3x daily, off work hours) |
| `proactive_insights` | `cron(hour=8, minute=0)` | system | standard | - | 900s | Daily proactive insights surfacing (8:00am PST) |
| `profile_regen` | `cron(day_of_week=sun, hour=4, minute=0)` | sage | heavy | llm, qdrant | 300s | Sage regenerates Chris profile from canonical knowledge (Sunday 4am) |
| `prompt_survival_report` | `cron(day_of_week=sun, hour=5, minute=38)` | system | standard | - | 300s | Weekly Sun 5:38am — per-prompt 7-day atom survival rate. Substrate for prompt A/B: produce two prompt_versions in parallel, this report shows which one's atoms the system kept. Slot picked to dodge db_vacuum_weekly (Sun 5:30am exclusive lock on brain.db). |
| `prune_raw_orphaned` | `cron(month=1,4,7,10, day=1, hour=4, minute=25)` | system | standard | - | 1800s | Quarterly raw/orphaned prune (180d retention; 1st of Jan/Apr/Jul/Oct @ 04:25) |
| `qdrant_write_audit` | `cron(hour=6, minute=32)` | system | light | - | 900s | Daily source audit: fail on raw qdrant_client mutating writes outside approved boundaries (06:32 PT) |
| `ragas_eval_gate` | `cron(day_of_week=sun, hour=4, minute=45)` | system | heavy | eval, llm, qdrant | 1800s | Weekly generated-answer RAGAS faithfulness/relevance gate over answer-oriented eval set (Sun 04:45) |
| `raptor_build` | `cron(day_of_week=sun, hour=7, minute=15)` | sage | heavy | embedder, index, qdrant | 1800s | Weekly RAPTOR hierarchical summary tree (Sun 07:15) |
| `raw_events_retention` | `cron(hour=4, minute=22)` | system | standard | sqlite | 900s | Prune unreferenced raw_events older than 14d (daily 4:22am) |
| `re_examine_rejected` | `cron(day=2, hour=4, minute=30)` | system | heavy | qdrant, sqlite | 1800s | Monthly rejected-proposal re-examination (2nd of month @ 04:30) |
| `recall_judge` | `cron(hour=4, minute=27)` | jenna | heavy | llm, qdrant, sqlite | 900s | Daily 4:27am — sample 30 recent recalls, LLM-judges relevance/groundedness via live re-recall, writes recall_judgments + back-fills action_audit.outcome (judged_good/judged_wrong). |
| `recall_outcome_label` | `cron(minute=17)` | system | standard | - | 300s | Hourly — mark action_audit recalls 'restated' when same session re-asks within 120s (cosine ≥0.85). Converts the ~24k/week pending recall signal into training data. |
| `recall_structural_judge_hourly` | `cron(minute=47)` | system | light | sqlite | 600s | Every hour at :47 — deterministically score unlabeled /recall outcomes (no LLM) |
| `reindex` | `cron(hour=3,23, minute=17)` | system | heavy | embedder, index, qdrant | 900s | Full Qdrant reindex (2x daily, off-hours) |
| `release_readiness` | `cron(hour=6, minute=52)` | system | light | git, release | 900s | Daily non-mutating release hygiene snapshot for changed-file lanes and required evidence (06:52 PT) |
| `retrieval_inhibition` | `cron(hour=3, minute=58)` | system | standard | qdrant, sqlite | 600s | Nightly Bjork-style inhibition of consistent retrieval losers (03:58am) |
| `retrieval_regression` | `cron(hour=6, minute=57)` | system | standard | eval, qdrant | 900s | Daily bounded retrieval regression gate over stable eval queries (06:57 PT) |
| `review_task_dispatcher` | `cron(hour=6, minute=30)` | system | standard | llm, openclaw | 900s | Daily 6:30am — dispatch up to 2 brain-generated review tasks to Sage |
| `schema_learner` | `cron(day_of_week=sun, hour=4, minute=40)` | system | heavy | llm, sqlite | 900s | CLS spectral clustering on atom coactivation -> compaction candidates (Sun 04:40) |
| `schema_revision` | `cron(day_of_week=sun, hour=8, minute=50)` | system | heavy | llm, sqlite | 900s | Weekly free-energy schema revision (Sun 08:50 - staggered off eval_holdout_promote @8:45) |
| `screen_time_ingest` | `cron(day_of_week=sun, hour=4, minute=35)` | sage | standard | - | 300s | Screen Time daily patterns via Sage -> raw/inbox (weekly) |
| `self_eval` | `cron(hour=3, minute=37)` | system | heavy | embedder, eval, qdrant | 900s | Nightly 03:37 PT: sample recent /recall queries, re-run, measure top-3 overlap drift. Populates self_eval_drift_7d SLO. |
| `self_model_regen` | `cron(hour=5, minute=25)` | system | standard | - | 900s | Nightly DMN-like unified self-model atom regen (05:25 PT) |
| `session_context_retention` | `cron(hour=4, minute=43)` | system | standard | - | 900s | Prune orphaned session_context rows older than 30d (daily 4:43am) |
| `session_rotate` | `cron(day_of_week=sun, hour=4, minute=30)` | system | standard | - | 900s | Weekly: archive old agent session checkpoints; alert on oversized live sessions (Sun 4:30am) |
| `shell_ingest` | `cron(hour=2, minute=15)` | ellie | standard | - | 300s | Shell history -> experience collection |
| `skill_extract` | `cron(day_of_week=sun, hour=7, minute=45)` | system | heavy | llm, sqlite | 900s | Weekly skill graph indexing (Sunday 7:45am) |
| `skill_materialize_cleanup` | `cron(hour=4, minute=10)` | system | standard | - | 900s | T2.10: archive orphaned/stale auto-* SKILL.md files; enforce MAX_AUTO_SKILLS cap (daily 4:10am) |
| `skill_sync` | `cron(day_of_week=sun, hour=7, minute=50)` | system | standard | - | 900s | Reconcile ~/.openclaw/skills disk ↔ openclaw.json entries + agent attach (Sunday 7:50am, after skill_extract) |
| `sleep_consolidate` | `cron(hour=3, minute=55)` | system | heavy | qdrant, sqlite | 900s | CLS sleep consolidation: coactivation + A-MEM + promotion (3:55am, Phase N4) |
| `slo_monitor` | `cron(minute=30)` | system | standard | - | 300s | Hourly SLO check with Telegram alerts on 3+ violations |
| `slos_check` | `interval(0:05:00)` | system | standard | - | 120s | Phase E1: SLO budget check + Telegram alert on breach (every 5 min) |
| `sm2_nightly` | `cron(hour=3, minute=25)` | system | standard | - | 900s | SM-2 nightly: seed next_review_at + obsolete stale atoms (3:25am) |
| `stale_cleanup` | `cron(day_of_week=sun, hour=3, minute=10)` | system | standard | - | 300s | Weekly incremental stale doc cleanup across collections (Sun 3:10am) |
| `stale_superseded_cleanup` | `cron(day_of_week=sun, hour=6, minute=20)` | system | standard | - | 900s | Weekly stale superseded memory cleanup (Sun 6:20am - staggered off canonical_merge_draft at 06:15) |
| `subtask_evaluator_brain_quality` | `cron(hour=4, minute=36)` | system | light | sqlite | 900s | Daily 4:36am — auto-complete brain-quality subtasks whose metric cleared target (no LLM) |
| `supersession_chain_cleanup` | `cron(day_of_week=sun, hour=6, minute=10)` | system | standard | - | 300s | Weekly cleanup of orphaned supersession chains (Sun 6:10am) |
| `training_pairs_generate` | `cron(day_of_week=sun, hour=8, minute=0)` | system | standard | qdrant, training | 900s | Weekly training pair generation from feedback (Sunday 8:00am) |
| `trust_recompute` | `cron(day_of_week=sun, hour=7, minute=5)` | system | heavy | embedder, qdrant | 900s | Weekly cross-source corroboration trust score refresh (Sunday 7:05am - staggered off canonical_quality_triage @7:00) |
| `ui_parity_audit` | `cron(hour=6, minute=54)` | system | light | readiness, ui | 900s | Daily static API-to-UI parity audit for world-level Brain dashboard coverage (06:54 PT) |
| `wal_checkpoint_daily` | `cron(hour=4, minute=55)` | system | standard | sqlite | 900s | Daily PRAGMA wal_checkpoint(TRUNCATE) on hot DBs (4:55am) |
| `wal_checkpoint_intraday` | `cron(hour=0,4,8,12,16,20, minute=35)` | system | light | sqlite | 600s | Intra-day WAL checkpoint(TRUNCATE) on hot DBs (every 4h at :35) |
| `web_source_trust_recompute` | `cron(day_of_week=sun, hour=5, minute=15)` | system | standard | - | 900s | Phase M6: recompute per-domain web search trust scores (Sun 5:15) |
| `weekly_synthesis` | `cron(day_of_week=sun, hour=4, minute=15)` | sage | heavy | llm | 300s | Weekly arc (Sage, Sunday 4:15am) |
