"""brain_core/scheduler.py — the brain's own cron.

Replaces 15 launchd plists with an AsyncIOScheduler that runs inside the
FastAPI event loop. Jobs execute as subprocess fire-and-forget (same semantics
as the POST /jobs/{name} route, which this scheduler reuses) so a long-running
ingest never blocks the server's request handlers.

Why in-process (and not launchd)?
  - No Python cold start per cron tick (brain_core modules stay hot)
  - One place to see job state (/jobs endpoints)
  - Cron edits are a Python constant, not a plist reload
  - Job dependencies can be expressed in code

Jobs are defined declaratively in JOB_SCHEDULE below. Each entry maps to a job
in server.py's JOB_REGISTRY, so the scheduler is just a cron → POST /jobs/{name}
bridge. No business logic lives here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger("brain.scheduler")


@dataclass
class ScheduledJob:
    """Declarative spec for one cron job."""

    name: str  # must match a key in server.py JOB_REGISTRY
    description: str
    trigger: object  # CronTrigger or IntervalTrigger
    agent: str  # owning agent (jenna|sage|ellie|market|system)
    # 2026-04-16 fix: default dropped 3600→300 to prevent thundering-herd
    # after brain-server restart. Previously a 50-min downtime would
    # re-fire ~22 jobs simultaneously (every default-grace job) when the
    # server came back up, saturating Ollama+Neo4j. 5 min is enough slack
    # for a graceful restart; jobs that genuinely benefit from a longer
    # replay window (weekly Sage syntheses, monthly backups) set their
    # own misfire_grace explicitly (900, 1800).
    misfire_grace: int = 300

    def next_run_str(self, scheduler: AsyncIOScheduler) -> str:
        job = scheduler.get_job(self.name)
        if not job or not job.next_run_time:
            return "none"
        return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")


# ── Schedule (mirrors the pre-consolidation launchd plists) ─────────────
# Timezone = local (America/Los_Angeles for Chris). APScheduler picks this up
# from the system when timezone is omitted.

JOB_SCHEDULE: list[ScheduledJob] = [
    # Ingest — fixed off-hours schedule to avoid Ollama contention during work hours.
    # Was every 4h (interval); now 3x daily at 6am, 2pm, 10pm PST.
    # 2pm is borderline but personal data changes during the day need <8h lag.
    ScheduledJob(
        name="personal_ingest",
        description="Apple Notes + iMessage + Calendar + Reminders → ChromaDB (3x daily off-peak)",
        trigger=CronTrigger(hour="6,14,22", minute=0),
        agent="jenna",
    ),
    ScheduledJob(
        name="gmail_ingest",
        description="Gmail signal classifier → raw/inbox",
        trigger=CronTrigger(hour=1, minute=30),
        agent="jenna",
    ),
    ScheduledJob(
        name="browser_ingest",
        description="Browser history → experience collection",
        trigger=CronTrigger(hour=2, minute=30),
        agent="sage",
    ),
    ScheduledJob(
        name="shell_ingest",
        description="Shell history → experience collection",
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
        description="Ghost blog posts via Admin API → knowledge collection",
        trigger=CronTrigger(hour=5, minute=0),
        agent="market",
    ),
    # M7-WS2a: PDF ingestion via Docling (off-hours, daily)
    ScheduledJob(
        name="pdf_ingest",
        description="M7-WS2a: scan ~/Documents/PDFs, parse via Docling, embed → knowledge",
        trigger=CronTrigger(hour=5, minute=30),
        agent="system",
        misfire_grace=1800,
    ),
    # M8.5: GraphRAG community summaries — Louvain on entity graph + Sage summary per cluster
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
        description="M7-WS2b: scan ~/Pictures/brain-ingest, OCR via Docling, embed captions → knowledge",
        trigger=CronTrigger(hour=5, minute=45),
        agent="system",
        misfire_grace=1800,
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
    # Runs daily at 4:10am — after 4:00 log_rotation, before 4:45 autonomy_proposer.
    ScheduledJob(
        name="skill_materialize_cleanup",
        description="T2.10: archive orphaned/stale auto-* SKILL.md files; enforce MAX_AUTO_SKILLS cap (daily 4:10am)",
        trigger=CronTrigger(hour=4, minute=10),
        agent="system",
        misfire_grace=900,
    ),
    # T2.12 Contextual Retrieval (2026-04-17): weekly incremental re-embed of canonical
    # chunks whose parent doc changed. Sunday 5:00am — after Sunday memory_lifecycle (2:30)
    # and canonical_pipeline (2:00), before other Sunday jobs. ~20-30 min runtime on full
    # pass, much less for incremental.
    ScheduledJob(
        name="contextual_embed_weekly",
        description="T2.12: re-embed canonical chunks with Anthropic-style per-doc context prefix (Sun 5:00am)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=0),
        agent="system",
        misfire_grace=1800,
    ),
    # Long-term sustainability (2026-04-17) — keeps SQLite DBs healthy over 5+ years.
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
    # 2026-04-17 — habituation prune for attention_queue (daily 3:20am, after
    # sleep_consolidate at 3:15 captures co-activation edges).
    ScheduledJob(
        name="habituation_prune",
        description="Drop attention_queue rows with shown_count ≥ 300 (daily 3:20am)",
        trigger=CronTrigger(hour=3, minute=20),
        agent="system",
    ),
    # 2026-04-17 — LLM auto-triage for candidate eval_proposals (daily 4:20am).
    ScheduledJob(
        name="eval_proposal_triage",
        description="CLI codex auto-approves/rejects candidate eval_proposals (daily 4:20am)",
        trigger=CronTrigger(hour=4, minute=20),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-17 — LLM triage for score=2 canonical_quality items
    # (Sun 07:00am, after canonical_quality_filter report at 06:35).
    ScheduledJob(
        name="canonical_quality_triage",
        description="LLM classifies score=2 canonical_quality items as archive/keep/uncertain",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=0),
        agent="system",
        misfire_grace=1800,
    ),
    # Eval — two-track gate (incident 2026-04-13)
    # stable  → 138 timeless queries, strict 5pt gate + heal dispatch (legacy alias eval_run)
    # extended → 606 timestamp/temporal queries, trend tracking only (no heal, 10pt threshold)
    # full    → 744-query union, trend tracking only
    ScheduledJob(
        name="eval_run",
        description="Stable-track eval (daily 3:30am) — strict 5pt gate, heal on regression",
        trigger=CronTrigger(hour=3, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="eval_run_extended",
        description="Extended-track eval (daily 3:50am) — trend only, no heal, 10pt threshold",
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
    # perceive → reflect → decide → act → journal cycle. Hard 10s wall-clock
    # budget per tick. Every action gated by autonomy.authorize().
    # Rate-limited 3x/hour per (kind, subject) pair.
    ScheduledJob(
        name="brain_loop_tick",
        description="v3: brain_loop executive cortex tick (every 60s)",
        trigger=IntervalTrigger(seconds=60),
        agent="system",
        misfire_grace=30,
    ),
    # v3 Phase 4.5: canonical design drift detector. Catches divergence between
    # ~/design-standard/DESIGN.md and ~/server/knowledge/canonical/design/personal_standard.md
    # before Chris's next frontend work depends on stale context.
    ScheduledJob(
        name="canonical_design_drift",
        description="v3: weekly design source vs canonical mirror SHA check (Sun 05:30)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    # v3 F41: nightly entity extraction reconciliation. The hot-path bg
    # pool (atoms_store._submit_bg_extract) drops extractions when the 64-
    # inflight cap is hit to protect Neo4j+Ollama under burst. This job
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
    # immediately if llm.dispatch breaker is still open (fast path — no
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
    # v3 Phase 6: live state snapshot — captures docker/launchd/goals/commits/sessions
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
    # only — writes proposals to eval_proposals for Chris's review. Apply
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
    # gate from the routine path — only stuck candidates (>=14d) still ping
    # eval_holdout_audit.
    ScheduledJob(
        name="eval_holdout_graduate",
        description="Phase N3: auto-graduate consistently-passing holdout candidates (Sun 7:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    # Phase C: eval auto-growth pipeline (Sun 8:45 promote → Sun 9:15 audit)
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
    # Phase N3: LoRA training — was missing from the cron entirely, so the
    # A/B gate always ran against stale weights. Sat 23:30 PT is ~10h before
    # lora_ab_gate Sun 9:30 so fresh weights are ready for the A/B decision.
    ScheduledJob(
        name="embed_finetune",
        description="Phase N3: weekly LoRA training on accumulated feedback pairs (Sat 23:30)",
        trigger=CronTrigger(day_of_week="sat", hour=23, minute=30),
        agent="system",
        misfire_grace=3600,
    ),
    # Phase E: SLO check loop — every 5 min, alerts on breach
    ScheduledJob(
        name="slos_check",
        description="Phase E1: SLO budget check + Telegram alert on breach (every 5 min)",
        trigger=IntervalTrigger(minutes=5),
        agent="system",
        misfire_grace=120,
    ),
    # Phase J2: HNSW ef_search adaptive tuning (weekly Sunday 4:15am, off-hours)
    # Advisory only — writes hnsw:search_ef metadata, picked up on next collection load.
    ScheduledJob(
        name="hnsw_tune",
        description="Phase J2: adaptive HNSW ef_search tuning based on measured p95 (Sun 4:15am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=15),
        agent="system",
        misfire_grace=900,
    ),
    # Phase 2D: SessionEnd outbox replay — every 5 min, drains any envelopes
    # the inline post_session.sh hook missed. CRON_MAP and RUNBOOK already
    # documented this cadence; the schedule entry was missing until 2026-04-13.
    ScheduledJob(
        name="outbox_drain",
        description="Phase 2D: drain SessionEnd outbox envelopes (every 5 min)",
        trigger=IntervalTrigger(minutes=5),
        agent="system",
        misfire_grace=120,
    ),
    # Phase M6: weekly web_source_trust recompute — aggregates per-domain
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
        description="Weekly retroactive near-duplicate scan of semantic_memory (Sun 3:20am)",
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=20),
        agent="system",
    ),
    ScheduledJob(
        name="auto_resolve_contradictions",
        description="Daily auto-resolve stale/low-confidence contradictions (6:00am) — "
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
        name="feedback_aggregate",
        description="Weekly search feedback aggregation (Sun 6:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="neo4j_backup",
        description="Nightly Neo4j data backup to MinIO (14-day retention)",
        trigger=CronTrigger(hour=3, minute=15),
        agent="system",
    ),
    ScheduledJob(
        name="backup_verify",
        description="Monthly backup restore smoke test (1st of month, 4:30am)",
        trigger=CronTrigger(day=1, hour=4, minute=30),
        agent="system",
        misfire_grace=900,
    ),
    # Canonical pipeline — 3× daily (02:00 / 07:00 / 22:00 PT) post 2026-04-17.
    # Was 1× nightly at 02:00, which caused `atoms_write_throughput_1h` SLO
    # flapping during natural morning idle windows (input queue drained by 2am
    # run → zero new atoms 08:00–17:00 until work-hours restriction expired).
    # Triple-split spreads atom production across waking hours:
    #   02:00  — nightly catchup (existing)
    #   07:00  — morning digest (gmail/calendar overnight ingest)
    #   22:00  — evening rollup (session/activity during the day)
    # All three outside the 9am-6pm Ollama/ChromaDB hot-work block.
    ScheduledJob(
        name="canonical_pipeline",
        description="Automated canonical promotion (3× daily: 02:00 / 07:00 / 22:00 PT)",
        trigger=CronTrigger(hour="2,7,22", minute=0),
        agent="system",
        misfire_grace=900,
    ),
    # Proactive reasoning (4x daily)
    ScheduledJob(
        name="proactive_check",
        description="Proactive insights — schedule gaps, contradictions, trends (4x daily)",
        trigger=CronTrigger(hour="7,13,19,1", minute=30),
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
        description="Prune embed cache: drop legacy rows, age >60d, cap 25k (daily 4:05am)",
        trigger=CronTrigger(hour=4, minute=5),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="fts_rebuild",
        description="Nightly SQLite FTS5 keyword index rebuild (4:15am)",
        trigger=CronTrigger(hour=4, minute=15),
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
    # Phase N4 — CLS sleep consolidation. Runs AFTER memory_consolidation 3:45
    # (it depends on the freshly-classified tiers). Coactivation matrix, A-MEM
    # auto-linking, episodic → semantic promotion. Outside the 9am-6pm work
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
        name="chroma_integrity",
        description="Weekly PRAGMA integrity_check on ChromaDB SQLite (Sun 3:35am)",
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=35),
        agent="system",
    ),
    ScheduledJob(
        name="memory_observability",
        description="Weekly memory observability report (Sunday 5am)",
        trigger=CronTrigger(day_of_week="sun", hour=5, minute=0),
        agent="system",
        misfire_grace=900,
    ),
    ScheduledJob(
        name="lint_memory",
        description="Weekly memory lint pass (Sunday 5:35am — staggered off canonical_design_drift at 05:30)",
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
        description="Weekly entity page generator — Sage synthesizes one hot entity per run (Sunday 4:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=30),
        agent="sage",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="answer_canonicalize",
        description="Nightly query→canonical promoter (03:50am — staggered off neo4j_backup at 03:15 to avoid reading while backup writes)",
        trigger=CronTrigger(hour=3, minute=50),
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
        description="Weekly rebuild of atom→entity MENTIONS edges in Neo4j (Sunday 3:30am)",
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
        description="Weekly memory review nudge (Sunday 6:50am — staggered off canonicalize_entities_dryrun at 06:45)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=50),
        agent="system",
        misfire_grace=900,
    ),
    # Reindex — off-hours only to avoid competing with Ollama/ChromaDB during work hours.
    # Was 5x daily (3,9,13,18,22); moved to 2x daily at 3:17 AM and 11:17 PM PST.
    ScheduledJob(
        name="reindex",
        description="Full ChromaDB reindex (2x daily, off-hours)",
        trigger=CronTrigger(hour="3,23", minute=17),
        agent="system",
        misfire_grace=900,
    ),
    # ── New data source ingest (agent-distilled) ──────────
    ScheduledJob(
        name="openclaw_sessions_ingest",
        description="OpenClaw agent session distillation via Jenna → raw/inbox (6×/day off-peak, respects 9am-6pm no-Ollama rule)",
        trigger=CronTrigger(hour="0,3,6,19,21,23", minute=35),
        agent="jenna",
    ),
    ScheduledJob(
        name="claude_code_sessions_ingest",
        description="Claude Code session distillation via Jenna → raw/inbox",
        trigger=CronTrigger(hour=1, minute=15),
        agent="jenna",
    ),
    ScheduledJob(
        name="git_activity_ingest",
        description="Git commit history distillation via Ellie → raw/inbox (1:45am, after gmail_ingest)",
        trigger=CronTrigger(hour=1, minute=45),
        agent="ellie",
    ),
    ScheduledJob(
        name="screen_time_ingest",
        description="Screen Time daily patterns via Sage → raw/inbox (weekly)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=35),
        agent="sage",
    ),
    ScheduledJob(
        name="active_contacts_ingest",
        description="Active iMessage contacts via Jenna → raw/inbox (monthly)",
        trigger=CronTrigger(day=1, hour=4, minute=0),
        agent="jenna",
    ),
    ScheduledJob(
        name="infra_validation",
        description="Weekly infra fact cross-check against live state (Sunday 7:15am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=15),
        agent="system",
    ),
    ScheduledJob(
        name="memory_health_report",
        description="Weekly memory health report (Sunday 7:30am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=30),
        agent="system",
    ),
    ScheduledJob(
        name="skill_extract",
        description="Weekly skill graph indexing (Sunday 7:45am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    # Phase B — scale & observability
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
        description="Weekly memory leak detection (Sunday 5:50am — staggered off canonical_lint at 05:45)",
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
    # Round 9 — Tier 2 capabilities
    ScheduledJob(
        name="code_index_refresh",
        description="Daily incremental code function indexer (3:35am — staggered off sm2_nightly at 03:25)",
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
        description="Weekly cross-source corroboration trust score refresh (Sunday 7:00am)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=0),
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
    # 2026-04-17 ECC-style skill evolution — weekly Sun 04:55, after
    # profile_regen (04:00) + canonical_index (04:45) so atom tier state
    # is fresh. Non-destructive: only writes SKILL.md files under
    # ~/.claude/skills/brain-learned-*. No LLM calls.
    ScheduledJob(
        name="atoms_to_skills",
        description="Promote high-confidence atoms → domain Claude Code skills (Sun 04:55)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=55),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-17 CLS schema learner — spectral clustering on atom_coactivation.
    # Runs Sun 04:40 before canonical_compaction (06:00) so its human-review
    # queue has clustering candidates to evaluate. Non-destructive.
    ScheduledJob(
        name="schema_learner",
        description="CLS spectral clustering on atom coactivation → compaction candidates (Sun 04:40)",
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=40),
        agent="system",
        misfire_grace=900,
    ),
    # Round 10 Wave 2 — episodic memory binding
    ScheduledJob(
        name="episode_binder",
        description="Daily episode clustering + Hebbian boost (3:18am, after entity_resolution)",
        trigger=CronTrigger(hour=3, minute=18),
        agent="system",
        misfire_grace=900,
    ),
    # Round 10 Wave 3 — synaptic pruning (default dry-run; flip the JOB_REGISTRY entry to dry_run=False after first review)
    ScheduledJob(
        name="memory_pruning",
        description="Monthly atrophied-memory dry-run (15th 4:10am)",
        trigger=CronTrigger(day=15, hour=4, minute=10),
        agent="system",
        misfire_grace=1800,
    ),
    # Active forgetting — real pruning + stale superseded cleanup
    ScheduledJob(
        name="memory_pruning_active",
        description="Monthly REAL atrophied-memory pruning (15th 4:15am, dry_run=False)",
        trigger=CronTrigger(day=15, hour=4, minute=15),
        agent="system",
        misfire_grace=1800,
    ),
    # 2026-04-16 Tier 2: quarterly prune_raw_orphaned — deletes entries in
    # raw/orphaned older than 180 days. Runs on 1st of Jan/Apr/Jul/Oct at
    # 04:25 local (well off the nightly window so it can't contend for
    # ChromaDB or Ollama).
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
    # lose top-rank competitions on the same query cue. Runs 3:55am —
    # between answer_canonicalize (03:50) and focus_aggregate (04:35).
    ScheduledJob(
        name="retrieval_inhibition",
        description="Nightly Bjork-style inhibition of consistent retrieval losers (03:55am)",
        trigger=CronTrigger(hour=3, minute=55),
        agent="system",
        misfire_grace=600,
    ),
    # 2026-04-16 Tier 3 #3: weekly Platt confidence calibration — fits
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
    # 2026-04-16 Tier 3 #7: weekly dream replay (Wagner 2004). Sage
    # hypothesizes novel connections between distant entity pairs. Small
    # Sage-dispatch budget (5 pairs) so it doesn't compete with weekly
    # syntheses. Sun 08:30 (after trust_recompute 07:00, before
    # gap_detection 09:00).
    ScheduledJob(
        name="dream_replay",
        description="Weekly REM-like generative conjecture synthesis (Sun 08:30)",
        trigger=CronTrigger(day_of_week="sun", hour=8, minute=30),
        agent="sage",
        misfire_grace=1800,
    ),
    # 2026-04-16 Tier 3 #5: weekly Friston schema-revision signal — emits
    # raw/inbox proposals for clusters of prediction errors instead of
    # silent per-atom punishment. Sun 08:45 (between dream_replay and
    # gap_detection so proposals land in the same nightly pipeline).
    ScheduledJob(
        name="schema_revision",
        description="Weekly free-energy schema revision (Sun 08:45)",
        trigger=CronTrigger(day_of_week="sun", hour=8, minute=45),
        agent="system",
        misfire_grace=900,
    ),
    # 2026-04-16 Tier 3 #9: weekly RAPTOR tree build (Sarthi 2024). Runs
    # after canonical_compaction (Sun 06:00) so it sees the freshest
    # canonical state. Heaviest weekly Sage job — budget up to 20 min.
    ScheduledJob(
        name="raptor_build",
        description="Weekly RAPTOR hierarchical summary tree (Sun 07:15)",
        trigger=CronTrigger(day_of_week="sun", hour=7, minute=15),
        agent="sage",
        misfire_grace=1800,
    ),
    ScheduledJob(
        name="stale_superseded_cleanup",
        description="Weekly stale superseded memory cleanup (Sun 6:20am — staggered off canonical_merge_draft at 06:15)",
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=20),
        agent="system",
        misfire_grace=900,
    ),
]


class BrainScheduler:
    """Wraps APScheduler. Each job triggers a registered command in the brain.

    The command dispatcher is passed in at start() time so this module stays
    free of any server.py import (avoids circular dependency).
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="America/Los_Angeles")
        self._dispatcher: Callable[[str], int] | None = None
        self._history: dict[str, list[dict]] = {}
        self._running_jobs: dict[str, int] = {}  # job_name -> pid
        self._MAX_HISTORY = 20
        self._alerted_jobs: set[str] = set()
        self._pending_completions: dict[str, tuple[float, int | None]] = {}  # job_name -> (start_ts, row_id)
        self._db_path = Path(__file__).resolve().parent.parent / "logs" / "scheduler_history.db"
        self._load_history_from_db()

    def _load_history_from_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("""CREATE TABLE IF NOT EXISTS job_history (
                id INTEGER PRIMARY KEY, job_name TEXT, started_at TEXT,
                pid INTEGER, error TEXT, manual INTEGER DEFAULT 0,
                finished_at TEXT DEFAULT NULL, duration_ms INTEGER DEFAULT NULL)""")
            # Migrate existing databases missing new columns
            for col, typedef in [
                ("finished_at", "TEXT DEFAULT NULL"),
                ("duration_ms", "INTEGER DEFAULT NULL"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE job_history ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            cur = conn.execute(
                "SELECT job_name, started_at, pid, error, manual, finished_at, duration_ms "
                "FROM job_history ORDER BY id DESC LIMIT 400"
            )
            for name, started, pid, error, manual, finished, duration in cur.fetchall():
                entry = {
                    "started_at": started,
                    "pid": pid,
                    "error": error,
                    "finished_at": finished,
                    "duration_ms": duration,
                }
                if manual:
                    entry["manual"] = True
                history = self._history.setdefault(name, [])
                history.insert(0, entry)
            for name in self._history:
                self._history[name] = self._history[name][: self._MAX_HISTORY]
            conn.close()
        except Exception:
            pass

    def _persist_entry(self, job_name: str, entry: dict) -> int | None:
        """Insert a history row. Returns the row id (used to update on completion)."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            cur = conn.execute(
                "INSERT INTO job_history (job_name, started_at, pid, error, manual) VALUES (?, ?, ?, ?, ?)",
                (
                    job_name,
                    entry.get("started_at"),
                    entry.get("pid", -1),
                    entry.get("error"),
                    1 if entry.get("manual") else 0,
                ),
            )
            row_id = cur.lastrowid
            conn.commit()
            conn.close()
            return row_id
        except Exception:
            return None

    def record_completion(
        self, job_name: str, row_id: int | None, start_ts: float, error: str | None = None
    ) -> None:
        """Called by _wait_for_job after a subprocess finishes."""
        finished_at = datetime.now(UTC).isoformat()
        duration_ms = int((time.time() - start_ts) * 1000)

        # Update in-memory history (find the matching entry by row_id or last unfinished)
        for entry in reversed(self._history.get(job_name, [])):
            if entry.get("finished_at") is None:
                entry["finished_at"] = finished_at
                entry["duration_ms"] = duration_ms
                if error and not entry.get("error"):
                    entry["error"] = error[:200]
                break

        # Update SQLite row
        if row_id is not None:
            try:
                conn = sqlite3.connect(str(self._db_path))
                conn.execute(
                    "UPDATE job_history SET finished_at=?, duration_ms=?, error=COALESCE(error, ?) WHERE id=?",
                    (finished_at, duration_ms, error[:200] if error else None, row_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

    def _reconcile_orphans(self) -> int:
        """2026-04-17 reindex-silent-death fix: on server startup, reconcile
        `job_history` rows that were left with finished_at=NULL by the prior
        brain-server instance. Those rows are orphans — their `_wait_for_job`
        thread died with the old process, so completion never recorded.

        Two classes of orphans:
          1. PID still alive → process survived restart (subprocess was
             detached via start_new_session=True). Keep the row but do nothing
             yet; the next reaper tick will catch it when the process exits.
          2. PID gone → the subprocess also died. Mark the row as completed
             with an 'orphaned_by_restart' error so UI / SLO stop showing it
             as "running forever".

        Returns the number of rows reconciled.
        """
        n_reconciled = 0
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, job_name, pid, started_at FROM job_history " "WHERE finished_at IS NULL"
            ).fetchall()
            for row in rows:
                pid = row["pid"] or -1
                alive = False
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False
                if alive:
                    # Subprocess survived the restart — rebuild tracking so
                    # the reaper can catch its eventual exit.
                    try:
                        started_at = row["started_at"]
                        if started_at and "T" in started_at:
                            start_ts = datetime.fromisoformat(started_at).timestamp()
                        else:
                            start_ts = time.time() - 60.0
                    except Exception:
                        start_ts = time.time() - 60.0
                    self._running_jobs[row["job_name"]] = pid
                    self._pending_completions[row["job_name"]] = (start_ts, row["id"])
                    continue
                # Dead — record completion with orphan marker
                finished_at = datetime.now(UTC).isoformat()
                conn.execute(
                    "UPDATE job_history SET finished_at=?, error=COALESCE(error, ?) WHERE id=?",
                    (finished_at, "orphaned_by_restart", row["id"]),
                )
                n_reconciled += 1
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("orphan reconcile failed: %s", exc)
        if n_reconciled:
            log.info("reconciled %d orphaned job rows from prior brain-server instance", n_reconciled)
        return n_reconciled

    def start(self, dispatcher: Callable[[str], int]) -> None:
        """Start the scheduler with a job dispatcher callback.

        dispatcher(job_name) -> pid  — called when a cron fires, same contract
        as the existing POST /jobs/{name} route handler.
        """
        self._dispatcher = dispatcher
        # 2026-04-17: reconcile orphans left by previous brain-server instance.
        # Must run before we start adding scheduler jobs — otherwise a freshly
        # fired cron could collide with a "running" orphan row and the dedup
        # in _dispatch_job (check for existing _running_jobs[name]) would fail
        # because that in-memory state is rebuilt from SQLite orphans first.
        self._reconcile_orphans()
        for job in JOB_SCHEDULE:
            self._scheduler.add_job(
                self._fire,
                trigger=job.trigger,
                id=job.name,
                args=[job.name],
                name=job.description,
                replace_existing=True,
                misfire_grace_time=job.misfire_grace,
                coalesce=True,  # collapse missed runs into 1
            )
        # In-process task executor (runs every 30s, not as subprocess)
        self._scheduler.add_job(
            self._tick_executor,
            trigger=IntervalTrigger(seconds=30),
            id="task_executor",
            name="Task executor tick (30s, in-process)",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
        )
        # 2026-04-16 fix: completion reaper. Previously _pending_completions
        # was populated at dispatch but never drained — the missing
        # _wait_for_job docstring-referenced method was never implemented.
        # Result: finished_at/duration_ms stayed NULL for every scheduled
        # run and the dict grew unbounded. This 15s-interval reaper polls
        # each PID with kill(0); dead processes get record_completion +
        # removed from _running_jobs and _pending_completions.
        self._scheduler.add_job(
            self._reap_completions,
            trigger=IntervalTrigger(seconds=15),
            id="completion_reaper",
            name="Scheduler completion reaper (15s, in-process)",
            replace_existing=True,
            misfire_grace_time=30,
            coalesce=True,
        )
        self._scheduler.start()
        log.info("brain scheduler started with %d jobs + task_executor", len(JOB_SCHEDULE))
        for job in JOB_SCHEDULE:
            log.info("  [%s] next=%s", job.name, job.next_run_str(self._scheduler))

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)

    def schedule_inprocess(
        self,
        func: Callable[[], None],
        name: str,
        seconds: int,
        description: str = "",
    ) -> None:
        """Register an in-process callable on a fixed interval.

        Bypasses the subprocess dispatcher so callers can observe or mutate
        in-process state (e.g. metrics_buf snapshot persistence). The callable
        runs on the FastAPI event loop thread via APScheduler's job executor;
        keep it fast and non-blocking.
        """
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=seconds),
            id=name,
            name=description or name,
            replace_existing=True,
            misfire_grace_time=min(seconds, 60),
            coalesce=True,
        )

    def _tick_executor(self) -> None:
        """In-process task executor tick. Runs every 30s.

        Two phases:
        1. process_pending — auto-approve tasks above confidence threshold
        2. process_ready — dispatch approved tasks to OpenClaw agents
        """
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from autopilot import is_enabled

            if not is_enabled():
                return
            from task_queue import task_queue

            task_queue.process_pending()  # returns (approved, escalated) — escalation self-dispatches
            task_queue.process_ready()
        except Exception as e:
            log.warning("task_executor tick failed: %s", e)

    _MAX_PENDING_AGE_S = 3600  # reap entries older than 1h even if PID still alive

    def _reap_completions(self) -> None:
        """Drain _pending_completions for any subprocess that has exited.

        Checks each tracked PID with kill(0). Three outcomes per entry:
          - process still alive, age < MAX → keep pending
          - process gone → record_completion + drop from pending + running
          - process stuck beyond MAX age → record_completion with timeout
            error + drop (prevents unbounded dict growth on stuck jobs)
        """
        if not self._pending_completions:
            return
        now = time.time()
        to_drop: list[str] = []
        for job_name, (start_ts, row_id) in list(self._pending_completions.items()):
            pid = self._running_jobs.get(job_name)
            if not pid or pid <= 0:
                self.record_completion(job_name, row_id, start_ts)
                to_drop.append(job_name)
                continue
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
            age = now - start_ts
            if not alive:
                self.record_completion(job_name, row_id, start_ts)
                to_drop.append(job_name)
            elif age > self._MAX_PENDING_AGE_S:
                self.record_completion(
                    job_name,
                    row_id,
                    start_ts,
                    error=f"reaper_timeout_{int(age)}s",
                )
                to_drop.append(job_name)
        for name in to_drop:
            self._pending_completions.pop(name, None)
            self._running_jobs.pop(name, None)

    _ALERT_THRESHOLD = 3  # consecutive failures before alerting

    def _fire(self, job_name: str) -> None:
        """APScheduler callback — dispatch the job and record to history."""
        # Skip if a prior run (scheduled or manual) is still alive. Prevents
        # two concurrent drains racing on the same `status='pending'` rows
        # with no SKIP LOCKED semantics → duplicate LLM calls / side effects.
        if job_name in self._running_jobs:
            old_pid = self._running_jobs[job_name]
            try:
                os.kill(old_pid, 0)
                log.info("scheduler: skip %s — already running (pid=%d)", job_name, old_pid)
                return
            except (ProcessLookupError, PermissionError):
                self._running_jobs.pop(job_name, None)  # stale, fall through

        start_ts = time.time()
        started = datetime.now().isoformat(timespec="seconds")
        pid = -1
        error = None
        try:
            if self._dispatcher is None:
                raise RuntimeError("dispatcher not registered")
            pid = self._dispatcher(job_name)
            if pid > 0:
                self._running_jobs[job_name] = pid
        except Exception as e:
            error = str(e)[:200]
            log.warning("job %s dispatch failed: %s", job_name, error)

        entry = {
            "started_at": started,
            "pid": pid,
            "error": error,
            "finished_at": None,
            "duration_ms": None,
        }
        history = self._history.setdefault(job_name, [])
        history.append(entry)
        if len(history) > self._MAX_HISTORY:
            history.pop(0)
        row_id = self._persist_entry(job_name, entry)

        if error:
            # Dispatch failed — mark completed immediately
            self.record_completion(job_name, row_id, start_ts, error)
        elif pid > 0:
            self._pending_completions[job_name] = (start_ts, row_id)

        # Alert on consecutive failures
        if error:
            recent_errors = sum(1 for h in history[-self._ALERT_THRESHOLD :] if h.get("error"))
            if recent_errors >= self._ALERT_THRESHOLD and job_name not in self._alerted_jobs:
                self._alerted_jobs.add(job_name)
                self._alert_failure(job_name, error)
        else:
            self._alerted_jobs.discard(job_name)  # reset on success

    def _alert_failure(self, job_name: str, last_error: str) -> None:
        """Send Telegram alert via Jenna when a job fails 3+ times consecutively."""
        try:
            from cli_llm import dispatch

            dispatch(
                agent="jenna",
                message=f"[BRAIN ALERT] Job '{job_name}' has failed {self._ALERT_THRESHOLD} consecutive times. Last error: {last_error}",
                thinking="off",
                timeout=30,
            )
        except Exception:
            log.error("failed to send job failure alert for %s", job_name)

    def list_jobs(self) -> list[dict]:
        jobs = []
        for spec in JOB_SCHEDULE:
            aps_job = self._scheduler.get_job(spec.name) if self._scheduler.running else None
            next_run = aps_job.next_run_time.isoformat() if aps_job and aps_job.next_run_time else None
            history = self._history.get(spec.name, [])
            last = history[-1] if history else None
            jobs.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "agent": spec.agent,
                    "next_run": next_run,
                    "last_run": last,
                    "run_count": len(history),
                }
            )
        return jobs

    def get_history(self, job_name: str) -> list[dict]:
        return list(self._history.get(job_name, []))

    def trigger_now(self, job_name: str) -> int:
        """Run a job immediately (manual trigger). Returns pid."""
        if self._dispatcher is None:
            raise RuntimeError("scheduler not started")
        # Check if already running
        if job_name in self._running_jobs:
            old_pid = self._running_jobs[job_name]
            try:
                os.kill(old_pid, 0)  # check if process exists
                raise ValueError(f"{job_name} already running (pid={old_pid})")
            except (ProcessLookupError, PermissionError):
                del self._running_jobs[job_name]  # stale entry, clean up
        start_ts = time.time()
        pid = self._dispatcher(job_name)
        if pid > 0:
            self._running_jobs[job_name] = pid
        entry = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "pid": pid,
            "error": None,
            "manual": True,
            "finished_at": None,
            "duration_ms": None,
        }
        history = self._history.setdefault(job_name, [])
        history.append(entry)
        if len(history) > self._MAX_HISTORY:
            history.pop(0)
        row_id = self._persist_entry(job_name, entry)
        if pid > 0:
            self._pending_completions[job_name] = (start_ts, row_id)
        return pid


# Module-level singleton (server.py imports this)
brain_scheduler = BrainScheduler()
