"""brain_core/job_definitions.py - the full cron JOB_SCHEDULE.

Split off from `brain_core/scheduler.py` on 2026-04-17: the schedule
grew past 100 jobs / ~880 lines of pure data in a 1400-line file, making
the actual scheduler logic hard to find. Scheduler code now lives in
`scheduler.py`; the job table lives here.

To add a job:
  1. Append a `ScheduledJob(...)` entry below.
  2. Add the job's subprocess argv to `server.py:JOB_REGISTRY`.
  3. Bounce the brain-server (launchctl kickstart).

Imports ScheduledJob from scheduler so types line up, and re-exports
JOB_SCHEDULE so callers can do either:
  from scheduler import JOB_SCHEDULE       (back-compat)
  from job_definitions import JOB_SCHEDULE (new direct import)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


@dataclass
class ScheduledJob:
    """Declarative spec for one cron job.

    Lives here (not scheduler.py) to keep job_definitions import-clean
    as a standalone module. scheduler.py re-exports for back-compat."""

    name: str  # must match a key in server.py JOB_REGISTRY
    description: str
    trigger: object  # CronTrigger or IntervalTrigger
    agent: str  # owning agent (jenna|sage|ellie|market|system)
    # 2026-04-16 fix: default dropped 3600->300 to prevent thundering-herd
    # after brain-server restart. Previously a 50-min downtime would
    # re-fire ~22 jobs simultaneously (every default-grace job) when the
    # server came back up, saturating local embedder+Neo4j. 5 min is enough slack
    # for a graceful restart; jobs that genuinely benefit from a longer
    # replay window (weekly Sage syntheses, monthly backups) set their
    # own misfire_grace explicitly (900, 1800).
    misfire_grace: int = 300
    resource_class: str = "standard"  # light|standard|heavy
    resource_tags: tuple[str, ...] = field(default_factory=tuple)

    def next_run_str(self, scheduler: AsyncIOScheduler) -> str:
        job = scheduler.get_job(self.name)
        if not job or not job.next_run_time:
            return "none"
        return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")


JOB_SCHEDULE: list[ScheduledJob] = [
    # Ingest - fixed off-hours schedule to avoid local embedder contention during work hours.
    # Was every 4h (interval); now 3x daily at 6am, 2pm, 10pm PST.
    # 2pm is borderline but personal data changes during the day need <8h lag.
    ScheduledJob(
        name="personal_ingest",
        description="Apple Notes + iMessage + Calendar + Reminders -> Qdrant personal (3x daily off-peak)",
        trigger=CronTrigger(hour="6,14,22", minute=0),
        agent="jenna",
    ),
    ScheduledJob(
        name="gmail_ingest",
        description="Gmail signal classifier -> raw/inbox",
        trigger=CronTrigger(hour=1, minute=30),
        agent="jenna",
    ),
    ScheduledJob(
        name="browser_ingest",
        description="Browser history -> experience collection",
        trigger=CronTrigger(hour=2, minute=30),
        agent="sage",
    ),
    ScheduledJob(
        name="shell_ingest",
        description="Shell history -> experience collection",
        trigger=CronTrigger(hour=2, minute=15),
        agent="ellie",
    ),
    ScheduledJob(
        name="obsidian_sync",
        description="Obsidian vault ↔ CouchDB pull",
        trigger=IntervalTrigger(hours=1),
        agent="jenna",
    ),
    ScheduledJob(
        name="healthcheck",
        description="System + service health capture",
        trigger=CronTrigger(hour=9, minute=0),
        agent="ellie",
    ),
    ScheduledJob(
        name="ghost_blog_ingest",
        description="Ghost blog posts via Admin API -> knowledge collection",
        trigger=CronTrigger(hour=5, minute=0),
        agent="market",
    ),
    # M7-WS2a: PDF ingestion via Docling (off-hours, daily)
    ScheduledJob(
        name="pdf_ingest",
        description="M7-WS2a: scan ~/Documents/PDFs, parse via Docling, embed -> knowledge",
        trigger=CronTrigger(hour=5, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    # M8.5: GraphRAG community summaries - Louvain on entity graph + Sage summary per cluster
    ScheduledJob(
        name="community_summaries",
        description="M8.5: Louvain community detection on entity graph + Sage summary per cluster (Sun 5:00am)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=0),
        agent="sage",
        misfire_grace=1800,
    ),
    # M7-WS2b: image OCR + caption ingestion (off-hours, daily, after PDF run)
    ScheduledJob(
        name="image_ingest",
        description="M7-WS2b: scan ~/Pictures/brain-ingest, OCR via Docling, embed captions -> knowledge",
        trigger=CronTrigger(hour=5, minute=45),
        agent="system",
        misfire_grace=1800,
    ),
    # Kuma heartbeat incident log — pulls state-change events daily
    ScheduledJob(
        name="kuma_heartbeats_ingest",
        description="Uptime Kuma incident state-changes -> raw/inbox (daily 6:00am, 24h window)",
        trigger=CronTrigger(hour=6, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    # Apple Health daily summary — tails iCloud Drive export from iOS Shortcut
    ScheduledJob(
        name="apple_health_ingest",
        description="Apple Health daily recovery signal (sleep/HRV/RHR/kcal) -> raw/inbox (8:00am, after iPhone 7:30 Shortcut + iCloud sync)",
        trigger=CronTrigger(hour=8, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    # Synthesis (daily/weekly/monthly)
    ScheduledJob(
        name="daily_synthesis",
        description="Daily narrative + reflection Q (Jenna)",
        trigger=CronTrigger(hour=21, minute=0),
        agent="jenna",
    ),
    ScheduledJob(
        name="weekly_synthesis",
        description="Weekly arc (Sage, Sunday 4:15am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=15),
        agent="sage",
    ),
    ScheduledJob(
        name="monthly_synthesis",
        description="Monthly arc (Sage, 1st of month 5am)",
        trigger=CronTrigger(day=1, hour=5, minute=0),
        agent="sage",
    ),
    # Self-learning reflection
    ScheduledJob(
        name="brain_reflect",
        description="Nightly Sage pattern/contradiction pass over last 7d of semantic_memory",
        trigger=CronTrigger(hour=2, minute=45),
        agent="sage",
        misfire_grace=900,
    ),
    # T2.10 auto-skill maintenance (2026-04-17): archive orphaned/stale auto-* skills
    # Runs daily at 4:10am - after 4:00 log_rotation, before 4:45 autonomy_proposer.
    ScheduledJob(
        name="skill_materialize_cleanup",
        description="T2.10: archive orphaned/stale auto-* SKILL.md files; enforce MAX_AUTO_SKILLS cap (daily 4:10am)",
        trigger=CronTrigger(hour=4, minute=10),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-17 session_rotate: archive OpenClaw agent session checkpoints
    # older than 14 days, alert on live sessions >100MB. Sunday 4:30am -
    # between skill_materialize_cleanup (4:10) and autonomy_proposer (4:45).
    # Added after a 103MB jenna session caused 42.5% empty-envelope rate
    # on brain_loop URGENT Telegram alerts.
    ScheduledJob(
        name="session_rotate",
        description="Weekly: archive old agent session checkpoints; alert on oversized live sessions (Sun 4:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    # T2.12 Contextual Retrieval (2026-04-17): weekly incremental re-embed of canonical
    # chunks whose parent doc changed. Sunday 5:00am - after Sunday memory_lifecycle (2:30)
    # and canonical_pipeline (2:00), before other Sunday jobs. ~20-30 min runtime on full
    # pass, much less for incremental.
    ScheduledJob(
        name="contextual_embed_weekly",
        description="T2.12: re-embed canonical chunks with Anthropic-style per-doc context prefix (Sun 5:10am - staggered off community_summaries @5:00)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=10),
        agent="system",
        misfire_grace=1800,
    ),
    # Long-term sustainability (2026-04-17) - keeps SQLite DBs healthy over 5+ years.
    ScheduledJob(
        name="db_vacuum_weekly",
        description="Weekly VACUUM + ANALYZE on brain.db/autonomy.db/llm_usage.db (Sun 5:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="action_audit_retention",
        description="Prune action_audit rows older than 90d (daily 4:20am)",
        trigger=CronTrigger(hour=4, minute=20),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="llm_usage_retention",
        description="Roll up llm_usage older than 90d into llm_usage_monthly (1st of month 4:30am)",
        trigger=CronTrigger(day=1, hour=4, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    # Maintenance
    ScheduledJob(
        name="memory_lifecycle",
        description="Age out + promote durable semantic memories (Sunday 2:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=2, minute=30),
        agent="system",
    ),
    # 2026-04-17 - habituation prune for attention_queue (daily 3:20am, after
    # sleep_consolidate at 3:15 captures co-activation edges).
    ScheduledJob(
        name="habituation_prune",
        description="Drop attention_queue rows with shown_count >= 300 (daily 3:20am)",
        trigger=CronTrigger(hour=3, minute=20),
        agent="system",
    ),
    # 2026-04-17 - LLM auto-triage for candidate eval_proposals (daily 4:20am).
    ScheduledJob(
        name="eval_proposal_triage",
        description="CLI codex auto-approves/rejects candidate eval_proposals (daily 4:25am - staggered off action_audit_retention @4:20 to avoid autonomy.db lock contention)",
        trigger=CronTrigger(hour=4, minute=25),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-17 - LLM triage for score=2 canonical_quality items
    # (Sun 07:00am, after canonical_quality_filter report at 06:35).
    ScheduledJob(
        name="canonical_quality_triage",
        description="LLM classifies score=2 canonical_quality items as archive/keep/uncertain",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=0),
        agent="system",
        misfire_grace=1800,
    ),
    # Eval - two-track gate (incident 2026-04-13)
    # stable  -> 138 timeless queries, strict 5pt gate + heal dispatch (legacy alias eval_run)
    # extended -> archived/current-truth trend set, loose-content tracking only (no heal, 10pt threshold)
    # full    -> 744-query union, trend tracking only
    ScheduledJob(
        name="eval_run",
        description="Stable-track eval (daily 3:30am) - strict 5pt gate, heal on regression",
        trigger=CronTrigger(hour=3, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="eval_run_extended",
        description="Extended-track eval (daily 3:50am) - loose-content trend only, no heal, 10pt threshold",
        trigger=CronTrigger(hour=3, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    # Phase 4: SM-2 nightly review scheduler (3:25am, before memory_consolidation)
    ScheduledJob(
        name="sm2_nightly",
        description="SM-2 nightly: seed next_review_at + obsolete stale atoms (3:25am)",
        trigger=CronTrigger(hour=3, minute=25),
        agent="system",
        misfire_grace=900,
    ),
    # Phase 7: closed-loop self-learning
    ScheduledJob(
        name="autonomy_proposer",
        description="Phase 7: surface autonomy level promote/demote proposals (4:45am)",
        trigger=CronTrigger(hour=4, minute=45),
        agent="system",
    ),
    # v3 Phase 1.8: scan action_audit for /recall/active misses and queue
    # intent_route candidates into eval_proposals for the weekly route learner.
    ScheduledJob(
        name="intent_miss_scan",
        description="v3: scan active_recall misses via correction regex (daily 3:28am)",
        trigger=CronTrigger(hour=3, minute=28),
        agent="system",
        misfire_grace=900,
    ),
    # v3 Phase 2: continuous executive cortex. Every 60s, runs the
    # perceive -> reflect -> decide -> act -> journal cycle. Hard 10s wall-clock
    # budget per tick. Every action gated by autonomy.authorize().
    # Rate-limited 3x/hour per (kind, subject) pair.
    ScheduledJob(
        name="brain_loop_tick",
        description="v3: brain_loop executive cortex tick (every 90s — relaxed from 60s 2026-04-22 to cut 33% of ticks)",
        trigger=IntervalTrigger(seconds=90),
        agent="system",
        misfire_grace=30,
    ),
    # v3 Phase 4.5: canonical design drift detector. Catches divergence between
    # ~/design-standard/DESIGN.md and ~/server/knowledge/canonical/design/personal_standard.md
    # before Chris's next frontend work depends on stale context.
    ScheduledJob(
        name="canonical_design_drift",
        description="v3: weekly design source vs canonical mirror SHA check (Sun 05:25 - off db_vacuum_weekly @5:30 to avoid VACUUM lock contention)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=25),
        agent="system",
        misfire_grace=900,
    ),
    # v3 F41: nightly entity extraction reconciliation. The hot-path bg
    # pool (atoms_store._submit_bg_extract) drops extractions when the 64-
    # inflight cap is hit to protect Neo4j+local embedder under burst. This job
    # catches those drops by finding fresh atoms with no atom_entity rows
    # and re-running extraction serially.
    ScheduledJob(
        name="entity_reconcile",
        description="v3: nightly catch-up for atoms with missing entity extraction (02:55)",
        trigger=CronTrigger(hour=2, minute=55),
        agent="system",
        misfire_grace=1800,
    ),
    # v3 llm_backlog: unified catch-up for LLM work dropped during quota
    # outage or circuit-breaker-open windows. Runs every 30 min. Aborts
    # immediately if llm.dispatch breaker is still open (fast path - no
    # retries against unavailable LLM). brain_loop also fires this on the
    # breaker_closed transition for event-driven catch-up within 60 s of
    # quota returning.
    ScheduledJob(
        name="llm_backlog_drain",
        description="v3: LLM backlog catch-up queue drain (every 30 min)",
        trigger=IntervalTrigger(minutes=30),
        agent="system",
        misfire_grace=300,
    ),
    # v3 Phase 6: live state snapshot - captures docker/launchd/goals/commits/sessions
    # current state every 10 minutes so "what's running" queries return reality,
    # not historical atoms. Written to ~/server/knowledge/canonical/live_state/*.md,
    # surfaced via active_recall's live_state intent route.
    ScheduledJob(
        name="live_state_snapshot",
        description="v3: snapshot current docker/launchd/goals/commits/sessions state (every 10min)",
        trigger=IntervalTrigger(minutes=10),
        agent="system",
        misfire_grace=120,
    ),
    # v3 Phase 6: weekly entity canonicalization. Walks Neo4j entities,
    # embeds names, merges cross-language duplicates above cosine 0.92.
    # Runs Sunday 06:45 (after daily entity extraction settled) with dry-run
    # only - writes proposals to eval_proposals for Chris's review. Apply
    # manually via cli/canonicalize_entities.py --apply.
    ScheduledJob(
        name="canonicalize_entities_dryrun",
        description="v3: weekly entity dedup proposal scan (Sun 06:45, dry-run)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="lora_ab_gate",
        description="Phase 7: weekly LoRA A/B gate + deploy (Sun 9:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=9, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    # Phase N3: auto-graduation of holdout candidates back into eval_set.json.
    # Runs BEFORE the existing promote job so this week's graduates exit the
    # pending file before new candidates arrive. Removes the Telegram tap
    # gate from the routine path - only stuck candidates (>=14d) still ping
    # eval_holdout_audit.
    ScheduledJob(
        name="eval_holdout_graduate",
        description="Phase N3: auto-graduate consistently-passing holdout candidates (Sun 7:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    # Phase C: eval auto-growth pipeline (Sun 8:45 promote -> Sun 9:15 audit)
    ScheduledJob(
        name="eval_holdout_promote",
        description="Phase C1: novelty-score eval candidates, promote top-N to pending file (Sun 8:45am)",
        trigger=CronTrigger(day_of_week="sun", hour=8, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="eval_holdout_audit",
        description="Phase C2: Telegram digest of >=14d stuck candidates only (Sun 9:15am)",
        trigger=CronTrigger(day_of_week="sun", hour=9, minute=15),
        agent="jenna",
        misfire_grace=900,
    ),
    # Phase N3: LoRA training - was missing from the cron entirely, so the
    # A/B gate always ran against stale weights. Sat 23:30 PT is ~10h before
    # lora_ab_gate Sun 9:30 so fresh weights are ready for the A/B decision.
    ScheduledJob(
        name="embed_finetune",
        description="Phase N3: weekly LoRA training on accumulated feedback pairs (Sat 23:30)",
        trigger=CronTrigger(day_of_week="sat", hour=23, minute=30),
        agent="system",
        misfire_grace=3600,
    ),
    # Phase E: SLO check loop - every 5 min, alerts on breach
    ScheduledJob(
        name="slos_check",
        description="Phase E1: SLO budget check + Telegram alert on breach (every 5 min)",
        trigger=IntervalTrigger(minutes=5),
        agent="system",
        misfire_grace=120,
    ),
    # Phase J2: HNSW ef_search adaptive tuning (weekly Sunday 4:15am, off-hours)
    # Removed 2026-04-17: duplicate of `hnsw_adaptive` (Sun 4:50am), both
    # called the same adaptive_tune() function 35 minutes apart on the
    # same local embedder/Qdrant. Keeping hnsw_adaptive since it uses the CLI
    # entry point (--adaptive flag) consistent with the tuner's module.
    # Phase 2D: SessionEnd outbox replay - every 5 min, drains any envelopes
    # the inline post_session.sh hook missed. CRON_MAP and RUNBOOK already
    # documented this cadence; the schedule entry was missing until 2026-04-13.
    ScheduledJob(
        name="outbox_drain",
        description="Phase 2D: drain SessionEnd outbox envelopes (every 5 min)",
        trigger=IntervalTrigger(minutes=5),
        agent="system",
        misfire_grace=120,
    ),
    # Phase M6: weekly web_source_trust recompute - aggregates per-domain
    # useful/wrong outcomes from web_search_results into the trust score table.
    ScheduledJob(
        name="web_source_trust_recompute",
        description="Phase M6: recompute per-domain web search trust scores (Sun 5:15)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=15),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="content_quality_slo",
        description="Daily content quality SLO check (4:00am, after eval_run)",
        trigger=CronTrigger(hour=4, minute=5),
        agent="system",
    ),
    # Profile regen (weekly Sunday 4am, after canonical pipeline accumulates a week of notes)
    ScheduledJob(
        name="profile_regen",
        description="Sage regenerates Chris profile from canonical knowledge (Sunday 4am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=0),
        agent="sage",
    ),
    # 2026-04-20 DMN-like self-model: unified daily compile of identity +
    # state + top-valence + top-reinforced atoms. Raichle 2001 / Northoff
    # 2006 - medial PFC maintains a continuous self-model that scores
    # every incoming signal for personal relevance. Runs 05:25 PT - after
    # canonical_pipeline (03:00) and profile_regen (Sun 04:00) so it sees
    # fresh state; before morning queries start. Zero LLM cost (pure SQL
    # + file read), so nightly is free.
    ScheduledJob(
        name="self_model_regen",
        description="Nightly DMN-like unified self-model atom regen (05:25 PT)",
        trigger=CronTrigger(hour=5, minute=25),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="canonical_index",
        description="Rebuild canonical knowledge index.md (weekly Sunday 4:45am, no LLM)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=45),
        agent="system",
    ),
    ScheduledJob(
        name="graph_consolidation",
        description="Nightly graph sleep: decay, prune, promote, cluster (2:50am)",
        trigger=CronTrigger(hour=2, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="entity_resolution",
        description="Nightly entity merge: embedding similarity + co-occurrence (3:05am)",
        trigger=CronTrigger(hour=3, minute=5),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="stale_cleanup",
        description="Weekly incremental stale doc cleanup across collections (Sun 3:10am)",
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=10),
        agent="system",
    ),
    ScheduledJob(
        name="near_dedup",
        description="Daily retroactive near-duplicate scan of semantic_memory (3:22am). "
        "Bumped weekly->daily 2026-04-23 after bilingual preference atoms "
        "accumulated past the weekly gate. Moved off 3:20 to avoid collision "
        "with habituation_prune and off 3:25 to avoid sm2_nightly brain.db/Qdrant contention.",
        trigger=CronTrigger(hour=3, minute=22),
        agent="system",
    ),
    ScheduledJob(
        name="brain_speak_digest",
        description="Brain's morning digest to Chris — drives observe, composer ranks, top 3 via Telegram (07:55 PT, scheduler runs in local tz).",
        trigger=CronTrigger(hour=7, minute=55),
        agent="system",
    ),
    ScheduledJob(
        name="brain_speak_urgent",
        description="Every 5 min: scan drives for severity>=7.5 observations, write to active Claude Code session doorbells. This is brain's interrupt channel.",
        trigger=CronTrigger(minute="*/5"),
        agent="system",
    ),
    ScheduledJob(
        name="canonical_staleness_check",
        description="Daily 04:30 PT: scan distilled/*.md for invalidated claims (missing imports / NameErrors that the code has since fixed). Retire stale files and delete their Qdrant atoms so brain stops surfacing already-fixed bugs.",
        trigger=CronTrigger(hour=4, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="self_eval",
        description="Nightly 03:37 PT: sample recent /recall queries, re-run, measure top-3 overlap drift. Populates self_eval_drift_7d SLO.",
        trigger=CronTrigger(hour=3, minute=37),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="auto_resolve_contradictions",
        description="Daily auto-resolve stale/low-confidence contradictions (6:00am) - "
        "v3 bumped from weekly to daily after finding 20-item pending "
        "backlog that should have been closed overnight",
        trigger=CronTrigger(hour=6, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="supersession_chain_cleanup",
        description="Weekly cleanup of orphaned supersession chains (Sun 6:10am)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=10),
        agent="system",
    ),
    ScheduledJob(
        name="memory_provenance_lint",
        description="Daily read-only lint of canonical/distilled provenance and supersession metadata (06:25 PT)",
        trigger=CronTrigger(hour=6, minute=25),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="feedback_aggregate",
        description="Weekly search feedback aggregation (Sun 6:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="recall_outcome_label",
        description="Hourly — mark action_audit recalls 'restated' when same session re-asks within 120s (cosine ≥0.85). Converts the ~24k/week pending recall signal into training data.",
        trigger=CronTrigger(minute=17),
        agent="system",
    ),
    ScheduledJob(
        name="recall_judge",
        description="Daily 4:27am — sample 30 recent recalls, LLM-judges relevance/groundedness via live re-recall, writes recall_judgments + back-fills action_audit.outcome (judged_good/judged_wrong).",
        trigger=CronTrigger(hour=4, minute=27),
        agent="jenna",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="cross_agent_lessons",
        description="Daily 5:10am — scan atoms from last 48h for cross-agent lesson signals (failure/correction keywords + named agents). Flags atoms.lesson_candidate=1 + lesson_agents list so skill_materializer can seed procedural skills from them.",
        trigger=CronTrigger(hour=5, minute=10),
        agent="system",
    ),
    ScheduledJob(
        name="prompt_survival_report",
        description="Weekly Sun 5:38am — per-prompt 7-day atom survival rate. Substrate for prompt A/B: produce two prompt_versions in parallel, this report shows which one's atoms the system kept. Slot picked to dodge db_vacuum_weekly (Sun 5:30am exclusive lock on brain.db).",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=38),
        agent="system",
    ),
    ScheduledJob(
        name="neo4j_backup",
        description="Nightly Neo4j data backup to MinIO (14-day retention)",
        trigger=CronTrigger(hour=3, minute=15),
        agent="system",
    ),
    ScheduledJob(
        name="backup_verify",
        description="Monthly backup restore smoke test (1st of month, 4:45am - staggered off llm_usage_retention @04:30 which also touches SQLite / MinIO)",
        trigger=CronTrigger(day=1, hour=4, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    # Canonical pipeline - 3x daily (02:00 / 07:00 / 22:00 PT) post 2026-04-17.
    # Was 1x nightly at 02:00, which caused `atoms_write_throughput_1h` SLO
    # flapping during natural morning idle windows (input queue drained by 2am
    # run -> zero new atoms 08:00-17:00 until work-hours restriction expired).
    # Triple-split spreads atom production across waking hours:
    #   02:00  - nightly catchup (existing)
    #   07:00  - morning digest (gmail/calendar overnight ingest)
    #   22:00  - evening rollup (session/activity during the day)
    # All three outside the 9am-6pm local embedder/Qdrant hot-work block.
    ScheduledJob(
        name="canonical_pipeline",
        description="Automated canonical promotion (3x daily: 02:00 / 07:00 / 22:00 PT)",
        trigger=CronTrigger(hour="2,7,22", minute=0),
        agent="system",
        misfire_grace=900,
    ),
    # Proactive reasoning (3x daily, off 9-18 PT work-hours block so the
    # Sage LLM call doesn't contend with Chris's hands-on Claude sessions).
    ScheduledJob(
        name="proactive_check",
        description="Proactive insights - schedule gaps, contradictions, trends (3x daily, off work hours)",
        trigger=CronTrigger(hour="7,20,1", minute=30),
        agent="sage",
    ),
    ScheduledJob(
        name="proactive_insights",
        description="Daily proactive insights surfacing (8:00am PST)",
        trigger=CronTrigger(hour=8, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    # Maintenance
    ScheduledJob(
        name="log_rotation",
        description="Truncate job/server logs >3d or >512KB (keeps last 100 lines)",
        trigger=CronTrigger(hour=4, minute=0),
        agent="system",
    ),
    ScheduledJob(
        name="embed_cache_prune",
        description="Prune embed cache: drop legacy rows, age >60d, cap 25k (daily 4:08am - staggered off content_quality_slo @4:05)",
        trigger=CronTrigger(hour=4, minute=8),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="memory_consolidation",
        description="Nightly memory tier promotion/demotion (3:45am, Phase 1D)",
        trigger=CronTrigger(hour=3, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    # Phase N4 - CLS sleep consolidation. Runs AFTER memory_consolidation 3:45
    # (it depends on the freshly-classified tiers). Coactivation matrix, A-MEM
    # auto-linking, episodic -> semantic promotion. Outside the 9am-6pm work
    # hours rule. 900s misfire grace matches the other heavy nightly jobs.
    ScheduledJob(
        name="sleep_consolidate",
        description="CLS sleep consolidation: coactivation + A-MEM + promotion (3:55am, Phase N4)",
        trigger=CronTrigger(hour=3, minute=55),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="llm_usage_purge",
        description="Weekly purge of llm_usage.db >90 days (Sun 4:55am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=55),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="memory_observability",
        description="Weekly memory observability report (Sunday 5:20am - staggered off community_summaries @5:00 / contextual_embed @5:10)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=20),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="lint_memory",
        description="Weekly memory lint pass (Sunday 5:35am - staggered off canonical_design_drift at 05:30)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=35),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="canonical_lint",
        description="Weekly structural lint: orphan canonical notes (Sunday 5:45am)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="entity_pages",
        description="Weekly entity page generator - Sage synthesizes one hot entity per run (Sunday 4:33am - staggered off session_rotate @04:30)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=33),
        agent="sage",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="answer_canonicalize",
        description="Nightly query->canonical promoter (04:02am - staggered off sleep_consolidate @03:55 which contends for local embedder/LLM)",
        trigger=CronTrigger(hour=4, minute=2),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="canonical_compaction",
        description="Weekly compaction candidate clustering report (Sunday 6:00am, after canonical_lint)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=0),
        agent="system",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="graph_rebuild_mentions",
        description="Weekly rebuild of atom->entity MENTIONS edges in Neo4j (Sunday 3:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="graph_backfill_co_mention",
        description="Weekly co-occurrence RELATES_TO backfill from shared MemoryAccess (Sunday 3:40am)",
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=40),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="canonical_merge_draft",
        description="Weekly top-3 compaction cluster Sage drafts (Sunday 6:15am, after compaction report)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=15),
        agent="sage",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="canonical_quality_filter_report",
        description="Weekly quality filter dry-run report (Sunday 6:35am, review only)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=35),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="memory_nudge",
        description="Weekly memory review nudge (Sunday 6:50am - staggered off canonicalize_entities_dryrun at 06:45)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    # Reindex - off-hours only to avoid competing with local embedder/Qdrant during work hours.
    # Was 5x daily (3,9,13,18,22); moved to 2x daily at 3:17 AM and 11:17 PM PST.
    ScheduledJob(
        name="reindex",
        description="Full Qdrant reindex (2x daily, off-hours)",
        trigger=CronTrigger(hour="3,23", minute=17),
        agent="system",
        misfire_grace=900,
    ),
    # ── New data source ingest (agent-distilled) ──────────
    ScheduledJob(
        name="openclaw_sessions_ingest",
        description="OpenClaw agent session distillation via Jenna -> raw/inbox (6x/day off-peak, respects 9am-6pm no-local-embedder rule)",
        trigger=CronTrigger(hour="0,3,6,19,21,23", minute=35),
        agent="jenna",
    ),
    ScheduledJob(
        name="claude_code_sessions_ingest",
        description="Claude Code session distillation via Jenna -> raw/inbox",
        trigger=CronTrigger(hour=1, minute=15),
        agent="jenna",
    ),
    ScheduledJob(
        name="git_activity_ingest",
        description="Git commit history distillation via Ellie -> raw/inbox (1:45am, after gmail_ingest)",
        trigger=CronTrigger(hour=1, minute=45),
        agent="ellie",
    ),
    ScheduledJob(
        name="screen_time_ingest",
        description="Screen Time daily patterns via Sage -> raw/inbox (weekly)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=35),
        agent="sage",
    ),
    ScheduledJob(
        name="active_contacts_ingest",
        description="Active iMessage contacts via Jenna -> raw/inbox (monthly)",
        trigger=CronTrigger(day=1, hour=4, minute=0),
        agent="jenna",
    ),
    ScheduledJob(
        name="infra_validation",
        description="Weekly infra fact cross-check against live state (Sunday 7:10am - staggered off raptor_build @7:15 which is heavy LLM)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=10),
        agent="system",
    ),
    ScheduledJob(
        name="memory_health_report",
        description="Weekly memory health report (Sunday 7:35am - staggered off eval_holdout_graduate @7:30)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=35),
        agent="system",
    ),
    ScheduledJob(
        name="skill_extract",
        description="Weekly skill graph indexing (Sunday 7:45am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    # Registry reconciliation + auto-attach — runs 5min after skill_extract
    # so any new brain-learned-* skills get registered in skills.entries and
    # attached to every agent without a manual `openclaw skills install`.
    ScheduledJob(
        name="skill_sync",
        description="Reconcile ~/.openclaw/skills disk ↔ openclaw.json entries + agent attach (Sunday 7:50am, after skill_extract)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    # Phase B - scale & observability
    ScheduledJob(
        name="event_compressor",
        description="Monthly event compression for old experience events (1st of month, 4:20am)",
        trigger=CronTrigger(day=1, hour=4, minute=20),
        agent="system",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="slo_monitor",
        description="Hourly SLO check with Telegram alerts on 3+ violations",
        trigger=CronTrigger(minute=30),
        agent="system",
        misfire_grace=300,
    ),
    ScheduledJob(
        name="hnsw_adaptive",
        description="Weekly adaptive HNSW ef_search tuning (Sunday 4:50am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="memory_leak_detector",
        description="Weekly memory leak detection (Sunday 5:50am - staggered off canonical_lint at 05:45)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="training_pairs_generate",
        description="Weekly training pair generation from feedback (Sunday 8:00am)",
        trigger=CronTrigger(day_of_week="sun", hour=8, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    # Round 9 - Tier 2 capabilities
    ScheduledJob(
        name="code_index_refresh",
        description="Daily incremental code function indexer (3:35am - staggered off sm2_nightly at 03:25)",
        trigger=CronTrigger(hour=3, minute=35),
        agent="system",
        misfire_grace=1200,
    ),
    ScheduledJob(
        name="gap_detection",
        description="Weekly knowledge gap detection from recall failures (Sunday 9:00am)",
        trigger=CronTrigger(day_of_week="sun", hour=9, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="trust_recompute",
        description="Weekly cross-source corroboration trust score refresh (Sunday 7:05am - staggered off canonical_quality_triage @7:00)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=5),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="focus_aggregate",
        description="Daily energy/focus data layer aggregation (4:35am)",
        trigger=CronTrigger(hour=4, minute=35),
        agent="system",
        misfire_grace=600,
    ),
    # 2026-04-17 ECC-style skill evolution - weekly Sun 04:55, after
    # profile_regen (04:00) + canonical_index (04:45) so atom tier state
    # is fresh. Non-destructive: only writes SKILL.md files under
    # ~/.claude/skills/brain-learned-*. No LLM calls.
    ScheduledJob(
        name="atoms_to_skills",
        description="Promote high-confidence atoms -> domain Claude Code skills (Sun 04:58 - staggered off llm_usage_purge @4:55)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=58),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-17 CLS schema learner - spectral clustering on atom_coactivation.
    # Runs Sun 04:40 before canonical_compaction (06:00) so its human-review
    # queue has clustering candidates to evaluate. Non-destructive.
    ScheduledJob(
        name="schema_learner",
        description="CLS spectral clustering on atom coactivation -> compaction candidates (Sun 04:40)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=40),
        agent="system",
        misfire_grace=900,
    ),
    # Round 10 Wave 2 - episodic memory binding
    ScheduledJob(
        name="episode_binder",
        description="Daily episode clustering + Hebbian boost (3:18am, after entity_resolution)",
        trigger=CronTrigger(hour=3, minute=18),
        agent="system",
        misfire_grace=900,
    ),
    # Round 10 Wave 3 - synaptic pruning (default dry-run; flip the JOB_REGISTRY entry to dry_run=False after first review)
    ScheduledJob(
        name="memory_pruning",
        description="Monthly atrophied-memory dry-run (15th 4:10am)",
        trigger=CronTrigger(day=15, hour=4, minute=10),
        agent="system",
        misfire_grace=1800,
    ),
    # Active forgetting - real pruning + stale superseded cleanup
    ScheduledJob(
        name="memory_pruning_active",
        description="Monthly REAL atrophied-memory pruning (15th 4:15am, dry_run=False)",
        trigger=CronTrigger(day=15, hour=4, minute=15),
        agent="system",
        misfire_grace=1800,
    ),
    # 2026-04-16 Tier 2: quarterly prune_raw_orphaned - deletes entries in
    # raw/orphaned older than 180 days. Runs on 1st of Jan/Apr/Jul/Oct at
    # 04:25 local (well off the nightly window so it can't contend for
    # Qdrant or local embedder).
    ScheduledJob(
        name="prune_raw_orphaned",
        description="Quarterly raw/orphaned prune (180d retention; 1st of Jan/Apr/Jul/Oct @ 04:25)",
        trigger=CronTrigger(month="1,4,7,10", day=1, hour=4, minute=25),
        agent="system",
        misfire_grace=1800,
    ),
    # 2026-04-16 Tier 2: monthly re-examine of rejected proposals. If new
    # high-trust corroboration arrives after a rejection, restore the
    # proposal to the pending queue for human review. Runs 2nd of month
    # at 04:30 (quiet hours, post-month-boundary so end-of-month syntheses
    # have settled).
    ScheduledJob(
        name="re_examine_rejected",
        description="Monthly rejected-proposal re-examination (2nd of month @ 04:30)",
        trigger=CronTrigger(day=2, hour=4, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    # 2026-04-16 Tier 3 #4: nightly retrieval-induced inhibition (Bjork
    # 1994). Applies small confidence decrements to atoms that consistently
    # lose top-rank competitions on the same query cue. Runs 3:55am -
    # between answer_canonicalize (03:50) and focus_aggregate (04:35).
    # 2026-04-17: shifted 03:55 -> 03:58 to deconflict with sleep_consolidate
    # (also at 03:55) - both are local-embedder-heavy and were contending for GPU.
    ScheduledJob(
        name="retrieval_inhibition",
        description="Nightly Bjork-style inhibition of consistent retrieval losers (03:58am)",
        trigger=CronTrigger(hour=3, minute=58),
        agent="system",
        misfire_grace=600,
    ),
    # 2026-04-16 Tier 3 #3: weekly Platt confidence calibration - fits
    # logistic transform over eval holdout + atoms.confidence pairs.
    # Sun 04:10 (post-eval, pre-weekly-synthesis).
    ScheduledJob(
        name="confidence_calibration",
        description="Weekly Platt calibration of atoms.confidence vs eval outcomes (Sun 04:10)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=10),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-17 Phase 3: weekly learned-to-rank logistic fit. Sun 04:20
    # (between confidence_calibration 04:10 and dream_replay 08:30).
    ScheduledJob(
        name="ltr_train",
        description="Weekly LogisticRegression LtR fit on recall feedback (Sun 04:20)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=20),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-20 upgraded weekly -> nightly. Biology runs REM 4-6x/night;
    # weekly was too sparse to seed cross-domain insight. Now 03:45 PT every
    # night between sleep_consolidate (03:15) and code_index_refresh (03:35).
    # MAX_PAIRS raised from 5 -> 15 in dream_replay.py so nightly still
    # generates meaningful recombination volume. All conjectures stay at
    # confidence 0.3 (never promoted without corroboration) - superhuman
    # brain keeps every dream, never deletes, ranks low by default.
    ScheduledJob(
        name="dream_replay",
        description="Nightly REM-like generative conjecture synthesis (03:48 PT - staggered off memory_consolidation @03:45 which contends for local embedder/Qdrant)",
        trigger=CronTrigger(hour=3, minute=48),
        agent="sage",
        misfire_grace=1800,
    ),
    # 2026-04-16 Tier 3 #5: weekly Friston schema-revision signal - emits
    # raw/inbox proposals for clusters of prediction errors instead of
    # silent per-atom punishment. Sun 08:45 (between dream_replay and
    # gap_detection so proposals land in the same nightly pipeline).
    ScheduledJob(
        name="schema_revision",
        description="Weekly free-energy schema revision (Sun 08:50 - staggered off eval_holdout_promote @8:45)",
        trigger=CronTrigger(day_of_week="sun", hour=8, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-16 Tier 3 #9: weekly RAPTOR tree build (Sarthi 2024). Runs
    # after canonical_compaction (Sun 06:00) so it sees the freshest
    # canonical state. Heaviest weekly Sage job - budget up to 20 min.
    ScheduledJob(
        name="raptor_build",
        description="Weekly RAPTOR hierarchical summary tree (Sun 07:15)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=15),
        agent="sage",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="stale_superseded_cleanup",
        description="Weekly stale superseded memory cleanup (Sun 6:20am - staggered off canonical_merge_draft at 06:15)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=20),
        agent="system",
        misfire_grace=900,
    ),
]


RESOURCE_BUDGET_OVERRIDES: dict[str, tuple[str, tuple[str, ...]]] = {
    # LLM/subscription CLI budget: serialize these so subscription sessions
    # stay stable and no API-billed fallback is encouraged.
    "daily_synthesis": ("standard", ("llm",)),
    "weekly_synthesis": ("heavy", ("llm",)),
    "monthly_synthesis": ("heavy", ("llm",)),
    "brain_reflect": ("heavy", ("llm", "qdrant")),
    "community_summaries": ("heavy", ("llm", "neo4j")),
    "eval_proposal_triage": ("standard", ("llm", "sqlite")),
    "canonical_quality_triage": ("heavy", ("llm", "sqlite")),
    "llm_backlog_drain": ("standard", ("llm",)),
    "profile_regen": ("heavy", ("llm", "qdrant")),
    "recall_judge": ("heavy", ("llm", "qdrant", "sqlite")),
    "proactive_check": ("standard", ("llm",)),
    "entity_pages": ("heavy", ("llm", "neo4j")),
    "answer_canonicalize": ("heavy", ("llm", "qdrant")),
    "canonical_merge_draft": ("heavy", ("llm", "qdrant")),
    "openclaw_sessions_ingest": ("heavy", ("llm", "qdrant")),
    "claude_code_sessions_ingest": ("heavy", ("llm", "qdrant")),
    "skill_extract": ("heavy", ("llm", "sqlite")),
    "atoms_to_skills": ("heavy", ("llm", "sqlite")),
    "schema_learner": ("heavy", ("llm", "sqlite")),
    "dream_replay": ("heavy", ("llm", "qdrant")),
    "schema_revision": ("heavy", ("llm", "sqlite")),
    # Embedding / vector search budget: one-time heavy is fine, but routine
    # background jobs should not overlap against the local embedder/Qdrant.
    "personal_ingest": ("heavy", ("embedder", "qdrant")),
    "pdf_ingest": ("heavy", ("embedder", "qdrant")),
    "image_ingest": ("heavy", ("embedder", "qdrant")),
    "contextual_embed_weekly": ("heavy", ("embedder", "qdrant", "index")),
    "eval_run": ("heavy", ("embedder", "qdrant", "eval")),
    "eval_run_extended": ("heavy", ("embedder", "qdrant", "eval")),
    "entity_reconcile": ("heavy", ("embedder", "neo4j", "sqlite")),
    "canonicalize_entities_dryrun": ("heavy", ("embedder", "neo4j")),
    "lora_ab_gate": ("heavy", ("embedder", "qdrant", "eval")),
    "embed_finetune": ("heavy", ("embedder", "training")),
    "entity_resolution": ("heavy", ("embedder", "neo4j")),
    "near_dedup": ("heavy", ("embedder", "qdrant", "sqlite")),
    "self_eval": ("heavy", ("embedder", "qdrant", "eval")),
    "reindex": ("heavy", ("embedder", "qdrant", "index")),
    "code_index_refresh": ("heavy", ("embedder", "qdrant", "index")),
    "hnsw_adaptive": ("heavy", ("qdrant", "eval")),
    "training_pairs_generate": ("standard", ("qdrant", "training")),
    "trust_recompute": ("heavy", ("embedder", "qdrant")),
    "ltr_train": ("heavy", ("qdrant", "training")),
    "raptor_build": ("heavy", ("embedder", "qdrant", "index")),
    "episode_binder": ("heavy", ("embedder", "qdrant", "sqlite")),
    "confidence_calibration": ("heavy", ("eval", "sqlite")),
    # Exclusive-ish local maintenance budget.
    "db_vacuum_weekly": ("heavy", ("sqlite",)),
    "memory_lifecycle": ("heavy", ("sqlite", "qdrant")),
    "canonical_pipeline": ("heavy", ("sqlite", "qdrant")),
    "sleep_consolidate": ("heavy", ("sqlite", "qdrant")),
    "canonical_compaction": ("heavy", ("sqlite", "qdrant")),
    "graph_rebuild_mentions": ("heavy", ("neo4j", "sqlite")),
    "graph_backfill_co_mention": ("heavy", ("neo4j", "sqlite")),
    "neo4j_backup": ("heavy", ("neo4j", "backup")),
    "backup_verify": ("heavy", ("backup", "sqlite")),
    "memory_pruning": ("heavy", ("sqlite", "qdrant")),
    "memory_pruning_active": ("heavy", ("sqlite", "qdrant")),
    "re_examine_rejected": ("heavy", ("sqlite", "qdrant")),
    "retrieval_inhibition": ("standard", ("sqlite", "qdrant")),
}


def _apply_resource_budgets() -> None:
    for job in JOB_SCHEDULE:
        resource_class, tags = RESOURCE_BUDGET_OVERRIDES.get(
            job.name, (job.resource_class, job.resource_tags)
        )
        job.resource_class = resource_class
        job.resource_tags = tuple(sorted(set(tags)))


_apply_resource_budgets()
