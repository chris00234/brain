"""JOB_REGISTRY + dispatcher for brain scheduler cron jobs.

Moved out of server.py so /jobs routes can live in routes/jobs.py without
circular imports. `dispatch_job()` is imported by both the scheduler
(via brain_scheduler.start(dispatch_job)) and the routes module.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from pathlib import Path

import boot_context
from metrics_buffer import metrics_buffer as _metrics_buf
from scheduler import brain_scheduler

from config import BRAIN_DIR, PYTHON

_py = PYTHON
_bd = str(BRAIN_DIR)
JOB_REGISTRY: dict[str, list[str]] = {
    # Brain's outbound voice — daily digest to Chris via Jenna/Telegram.
    "brain_speak_digest": [_py, f"{_bd}/brain_core/speak.py", "run"],
    # Real-time urgent path: scan every 5 min for severity >= 7.5 observations
    # and write to /tmp/.brain_doorbell.<sid>.jsonl for each active Claude Code
    # or Codex session. Boot hooks read + consume those on the next turn.
    "brain_speak_urgent": [_py, f"{_bd}/brain_core/speak.py", "urgent_scan"],
    # Canonical staleness detector: daily scan of distilled/*.md for claims
    # invalidated by the current code, plus active canonical notes for
    # stale current-truth supersession claims. Retires fixed-bug files,
    # deletes corresponding Qdrant atoms, and fails on current-truth blockers.
    "canonical_staleness_check": [_py, f"{_bd}/brain_core/canonical_staleness.py"],
    "memory_provenance_lint": [
        _py,
        f"{_bd}/cli/lint_memory_provenance.py",
        "--write-report",
        "--json",
    ],
    "qdrant_write_audit": [_py, f"{_bd}/cli/audit_qdrant_writes.py"],
    "entry_contract_audit": [_py, f"{_bd}/cli/entry_contract_audit.py", "--json"],
    "privacy_negative_audit": [_py, f"{_bd}/cli/privacy_negative_audit.py"],
    "config_secret_audit": [_py, f"{_bd}/cli/config_secret_audit.py"],
    "release_readiness": [_py, f"{_bd}/cli/release_readiness.py"],
    "ui_parity_audit": [_py, f"{_bd}/cli/ui_parity_audit.py"],
    "retrieval_regression": [_py, f"{_bd}/cli/retrieval_regression.py", "--limit", "20", "--json"],
    "crag_regression": [_py, f"{_bd}/cli/crag_regression.py", "--limit", "40", "--json"],
    "crag_correction_regression": [_py, f"{_bd}/cli/crag_correction_regression.py", "--json"],
    "crag_llm_correction_sample": [
        _py,
        f"{_bd}/cli/crag_correction_regression.py",
        "--json",
        "--rewrite-source",
        "llm",
        "--limit",
        "7",
        "--llm-timeout-s",
        "8",
    ],
    # Self-eval: nightly sample of recent /recall calls; measures top-3
    # overlap drift when re-run. Surfaces via self_eval_drift_7d SLO.
    "self_eval": [_py, f"{_bd}/brain_core/self_eval.py"],
    # Ingestion
    "personal_ingest": ["/bin/bash", f"{_bd}/ingest/run_personal.sh"],
    "gmail_ingest": [_py, f"{_bd}/ingest/gmail.py"],
    "browser_ingest": [_py, f"{_bd}/ingest/browser.py"],
    "shell_ingest": [_py, f"{_bd}/ingest/shell_history.py"],
    "obsidian_sync": [_py, f"{_bd}/ingest/obsidian.py", "pull"],
    "healthcheck": [_py, f"{_bd}/ingest/healthcheck.py"],
    "ghost_blog_ingest": [_py, f"{_bd}/ingest/ghost_blog.py"],
    "kuma_heartbeats_ingest": [_py, f"{_bd}/ingest/kuma_heartbeats.py"],
    "apple_health_ingest": [_py, f"{_bd}/ingest/apple_health.py"],
    # New data source ingest (agent-distilled)
    "openclaw_sessions_ingest": [_py, f"{_bd}/ingest/openclaw_sessions.py"],
    "openclaw_sessions_ingest_market": [
        _py,
        f"{_bd}/ingest/openclaw_sessions.py",
        "--agents",
        "market",
        "--max-sessions",
        "20",
    ],
    "openclaw_sessions_ingest_jenna": [
        _py,
        f"{_bd}/ingest/openclaw_sessions.py",
        "--agents",
        "jenna",
        "--max-sessions",
        "20",
    ],
    "openclaw_sessions_ingest_liz": [
        _py,
        f"{_bd}/ingest/openclaw_sessions.py",
        "--agents",
        "liz",
        "--max-sessions",
        "20",
    ],
    "openclaw_sessions_ingest_ellie": [
        _py,
        f"{_bd}/ingest/openclaw_sessions.py",
        "--agents",
        "ellie",
        "--max-sessions",
        "20",
    ],
    "openclaw_sessions_ingest_sage": [
        _py,
        f"{_bd}/ingest/openclaw_sessions.py",
        "--agents",
        "sage",
        "--max-sessions",
        "20",
    ],
    "claude_code_sessions_ingest": [_py, f"{_bd}/ingest/claude_code_sessions.py"],
    "git_activity_ingest": [_py, f"{_bd}/ingest/git_activity.py"],
    "screen_time_ingest": [_py, f"{_bd}/ingest/screen_time.py"],
    "active_contacts_ingest": [_py, f"{_bd}/ingest/active_contacts.py"],
    # Synthesis
    "daily_synthesis": [_py, f"{_bd}/synthesis/daily.py"],
    "weekly_synthesis": [_py, f"{_bd}/synthesis/weekly.py"],
    "monthly_synthesis": [_py, f"{_bd}/synthesis/monthly.py"],
    "brain_reflect": [_py, f"{_bd}/synthesis/reflect.py"],
    "profile_regen": [_py, f"{_bd}/synthesis/profile_regen.py"],
    # 2026-04-20 DMN-like unified self-model atom. Nightly compile of identity +
    # state + top-valence + top-reinforced into canonical/chris/_self_model.md.
    # Next canonical_pipeline run turns it into the default retrieval anchor.
    "self_model_regen": [_py, f"{_bd}/synthesis/self_model_regen.py"],
    # 2026-04-17 ECC-style skill evolution: convert high-confidence atoms
    # (tier=core/semantic, kind=preference/decision/correction) into
    # domain-scoped Claude Code skills at ~/.claude/skills/brain-learned-*
    "atoms_to_skills": [_py, f"{_bd}/cli/atoms_to_skills.py"],
    # 2026-04-17 CLS schema learner — spectral clustering on atom_coactivation
    # → canonical_compaction candidates (non-destructive; destructive merge
    # remains human-gated via canonical_compaction Sun 06:00).
    "schema_learner": [_py, f"{_bd}/brain_core/pipeline/schema_learner.py"],
    # 2026-04-17 habituation prune — drops attention_queue rows with
    # shown_count >= 300. Biological analog: synaptic habituation.
    "habituation_prune": [_py, f"{_bd}/brain_core/pipeline/habituation_prune.py"],
    # 2026-04-17 LLM auto-triage for candidate eval_proposals — CLI codex
    # classifies approve/reject with confidence; >=0.8 auto-marks.
    "eval_proposal_triage": [_py, f"{_bd}/cli/eval_proposal_triage.py", "--apply"],
    # 2026-04-17 LLM triage for score=2 canonical_quality items — session-log
    # vs genuine knowledge classifier. >=0.8 verdict + archive → reversible
    # move to canonical/archived/. Runs weekly after canonical_quality report.
    "canonical_quality_triage": [_py, f"{_bd}/cli/canonical_quality_triage.py", "--apply"],
    "proactive_check": [_py, f"{_bd}/brain_core/proactive.py"],
    # Maintenance
    "memory_lifecycle": [_py, f"{_bd}/brain_core/memory_lifecycle.py"],
    "canonical_pipeline": [_py, f"{_bd}/pipeline/pipeline_auto.py"],
    # Two-track eval (incident 2026-04-13): stable=strict gate+heal, extended=trend only.
    # `eval_run` aliases to the stable track so legacy scheduled triggers keep working.
    "eval_run": [
        _py,
        f"{_bd}/cli/eval_gate.py",
        "--eval-set",
        f"{_bd}/cli/eval_set_stable.json",
        "--baseline",
        f"{_bd}/cli/eval_baseline_stable.json",
        "--track",
        "stable",
    ],
    "eval_run_stable": [
        _py,
        f"{_bd}/cli/eval_gate.py",
        "--eval-set",
        f"{_bd}/cli/eval_set_stable.json",
        "--baseline",
        f"{_bd}/cli/eval_baseline_stable.json",
        "--track",
        "stable",
    ],
    "eval_run_extended": [
        _py,
        f"{_bd}/cli/eval_gate.py",
        "--eval-set",
        f"{_bd}/cli/eval_set_extended_v2.json",
        "--baseline",
        f"{_bd}/cli/eval_baseline_extended.json",
        "--track",
        "extended",
        "--no-heal",
        "--content-metric",
        "loose",
        "--threshold",
        "10",
    ],
    "ragas_eval_gate": [
        _py,
        f"{_bd}/cli/eval_compare.py",
        "--json",
        "--ragas",
        "--ragas-answer-source",
        "generated",
        "--limit",
        "20",
        "--eval-set",
        f"{_bd}/cli/eval_set_ragas_answers.json",
        "--persist-track",
        "ragas",
        "--content-metric",
        "loose",
    ],
    "adversarial_memory_eval": [
        _py,
        f"{_bd}/cli/eval_compare.py",
        "--json",
        "--include-per-test",
        "--eval-set",
        f"{_bd}/cli/eval_set_adversarial.json",
        "--persist-track",
        "adversarial",
        "--content-metric",
        "loose",
    ],
    "holdout_rotation_eval": [
        _py,
        f"{_bd}/cli/eval_compare.py",
        "--json",
        "--include-per-test",
        "--eval-set",
        f"{_bd}/cli/eval_set_holdout_rotation.json",
        "--persist-track",
        "holdout",
        "--content-metric",
        "loose",
    ],
    "eval_run_full": [_py, f"{_bd}/cli/eval_gate.py", "--track", "full", "--no-heal", "--threshold", "10"],
    # Phase 4: SM-2 nightly review scheduler — seeds null next_review_at + obsoletes stale atoms
    "sm2_nightly": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from sm2 import nightly_pass; import json; print(json.dumps(nightly_pass()))",
    ],
    # Phase 7: closed-loop self-learning jobs
    "autonomy_proposer": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from autonomy_proposer import run; import json; print(json.dumps(run()))",
    ],
    # v3 Phase 1.8: active_recall miss detection (daily 03:28)
    "intent_miss_scan": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from intent_miss_scan import run; import json; print(json.dumps(run()))",
    ],
    # v3 Phase 2: continuous executive cortex tick (every 60s).
    "brain_loop_tick": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from brain_loop import run; import json; print(json.dumps(run()))",
    ],
    # v3 Phase 4.5: canonical design drift check (weekly Sun 05:30).
    "canonical_design_drift": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from canonical_design_drift import run; import json; print(json.dumps(run()))",
    ],
    # v3 F41: nightly entity-extraction reconciliation (nightly 02:55).
    # Catches atoms whose hot-path entity extraction was dropped by the
    # bounded bg pool during ingest bursts.
    "entity_reconcile": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from entity_reconcile import run; import json; print(json.dumps(run()))",
    ],
    # v3 llm_backlog drain (every 30 min) — unified catch-up for LLM work
    # that was dropped during quota outage.
    "llm_backlog_drain": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from llm_backlog import run; import json; print(json.dumps(run()))",
    ],
    # v3 Phase 6: live state snapshot — runs every 10 min.
    "live_state_snapshot": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from live_state_snapshot import run; import json; print(json.dumps(run(), ensure_ascii=False))",
    ],
    # 2026-04-17 T2.10: Voyager/Hermes auto-skill materialization maintenance.
    # Daily archive of orphaned/stale auto-* SKILL.md files, enforces MAX_AUTO_SKILLS cap.
    "skill_materialize_cleanup": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from skill_materializer import cleanup_stale_auto_skills; import json; print(json.dumps(cleanup_stale_auto_skills()))",
    ],
    # 2026-04-17 session_rotate: archive OpenClaw agent session checkpoints > 14d,
    # alert on live sessions > 100MB. Triggered after 103MB jenna session caused
    # 42.5% empty-envelope rate on Telegram alerts.
    "session_rotate": [
        _py,
        f"{_bd}/cli/session_rotate.py",
    ],
    # 2026-04-17 T2.12: Contextual Retrieval (Anthropic 2024) weekly incremental.
    # Re-embed canonical chunks whose parent doc content_hash changed this week.
    # Directly targets extended eval 64% literal-wording gap. Gated by
    # BRAIN_CONTEXTUAL_EMBED_ENABLED env var (default off until first batch verified).
    "contextual_embed_weekly": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from contextual_embed import run; import json; print(json.dumps(run()))",
    ],
    # 2026-04-17 long-term sustainability: weekly VACUUM + ANALYZE across
    # brain/autonomy/llm_usage DBs to reclaim pages and refresh query stats.
    "db_vacuum_weekly": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_vacuum; import json; print(json.dumps(run_vacuum()))",
    ],
    # 2026-04-30: daily WAL checkpoint(TRUNCATE) on hot DBs. Between weekly
    # vacuums the WAL grew unbounded (embedding_cache 224MB, autonomy 176MB)
    # and breached logs_dir_total_mb SLO. Daily TRUNCATE keeps WAL bounded.
    "wal_checkpoint_daily": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_wal_checkpoint; import json; print(json.dumps(run_wal_checkpoint()))",
    ],
    # 2026-05-13 intra-day WAL checkpoint (every 4h). Same TRUNCATE op, but
    # skips the dir-size snapshot — that one stays on the daily cadence so
    # the growth-rate SLO baseline pairs are stable.
    "wal_checkpoint_intraday": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_wal_checkpoint_intraday; import json; print(json.dumps(run_wal_checkpoint_intraday()))",
    ],
    # 2026-05-13 outcome_feedback daily — read-only override pattern detector
    # that materializes review tasks for repeated overrides. No LLM, no policy
    # mutation. Caps at 5 tasks/day; subsequent runs dedupe by signature.
    "outcome_feedback_review": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from outcome_feedback import create_override_review_tasks; import json; print(json.dumps(create_override_review_tasks(hours=168, min_overrides=2, max_tasks=5), default=str))",
    ],
    # 2026-05-13 brain self-quality goal scaffold. Deterministic, LLM-free —
    # materializes measurable subtasks under the top brain-improvement goal
    # so goal progress becomes computable and next-best-action surfaces a
    # concrete target instead of "observe".
    "goal_subtask_scaffold_brain_quality": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from goal_subtask_scaffold import ensure_brain_quality_subtasks; import json; print(json.dumps(ensure_brain_quality_subtasks(max_create=8), default=str))",
    ],
    # 2026-05-13 subtask metric evaluator. Closes the self-learning loop —
    # subtasks auto-complete when the metric clears the target.
    "subtask_evaluator_brain_quality": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from subtask_evaluator import evaluate_brain_quality_subtasks; import json; print(json.dumps(evaluate_brain_quality_subtasks(), default=str))",
    ],
    # 2026-05-13 metric trend snapshot — feeds belief_state.trend_alerts.
    "metric_trend_snapshot": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from metric_trend_tracker import snapshot_now; import json; print(json.dumps(snapshot_now(), default=str))",
    ],
    # 2026-05-13 docker-volumes backup retention.
    "docker_volumes_backup_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from backup_retention import run_backup_retention; import json; print(json.dumps(run_backup_retention(keep_per_family=7), default=str))",
    ],
    # 2026-05-13 hourly structural recall judge (no LLM).
    "recall_structural_judge_hourly": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from recall_structural_judge import run; import json; print(json.dumps(run(hours=2, limit=500), default=str))",
    ],
    # 2026-05-13 review task dispatcher — closes outcome_feedback +
    # goal_subtask_scaffold → sage execution loop.
    "review_task_dispatcher": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from review_task_dispatcher import dispatch_pending_review_tasks; import json; print(json.dumps(dispatch_pending_review_tasks(), default=str))",
    ],
    # 2026-04-17 long-term sustainability: action_audit retention (90d).
    # Currently ~48K rows, growing per brain_store call. Keep 90d for
    # provenance; older data summarized in canonical if significant.
    "action_audit_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_action_audit_retention; import json; print(json.dumps(run_action_audit_retention()))",
    ],
    # 2026-05-12 raw_events retention. Prunes unreferenced rows older than 90d
    # while protecting sources that maintain sidecar links (coding_event,
    # atoms_hot_path). FTS shadow table auto-syncs via AFTER DELETE triggers.
    "raw_events_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_raw_events_retention; import json; print(json.dumps(run_raw_events_retention()))",
    ],
    # 2026-05-12 brain-doctor daily snapshot. Writes the health report to
    # logs/brain_doctor_daily.json so SessionStart hooks / dashboards can
    # surface drift without manually running the CLI.
    "brain_doctor_daily": [
        _py,
        f"{_bd}/cli/brain_doctor.py",
    ],
    # 2026-04-17 long-term sustainability: llm_usage rollup to monthly.
    # Keep 90d detail, archive older to llm_usage_monthly (month, agent) aggregates.
    "llm_usage_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_llm_usage_retention; import json; print(json.dumps(run_llm_usage_retention()))",
    ],
    # 2026-04-26 retention: autonomy_decisions writes ~48K rows/day; the table
    # grew 600KB → 81MB in 8 days. 14d window keeps gate-check audit for
    # incident review while bounding steady state ~670K rows.
    "autonomy_decisions_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_autonomy_decisions_retention; import json; print(json.dumps(run_autonomy_decisions_retention()))",
    ],
    # 2026-04-26 retention: metrics_snapshots safety net (slos.py only reads
    # last 20 rows; everything else is trend history). Existing 90d DELETE in
    # metrics_buffer.persist remains; this trims more aggressively to 14d so
    # the weekly VACUUM has reclaimable pages.
    "metrics_history_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_metrics_history_retention; import json; print(json.dumps(run_metrics_history_retention()))",
    ],
    # 2026-04-26 retention: session_context orphans (sessions that crashed
    # or never sent SessionEnd). Normal lifecycle is wm_consolidate per-session;
    # this 30d sweep is the catch-all safety net.
    "session_context_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_session_context_retention; import json; print(json.dumps(run_session_context_retention()))",
    ],
    # 2026-05-14 retention: ad-hoc repair sidecars (`*.pre_*` / `*.pre-*`)
    # at the top of logs/. Each one is hot-DB-sized; without retention they
    # stack to GB. 7d window lets a migration prove itself before pruning.
    "sidecar_backup_retention": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_sidecar_backup_retention; import json; print(json.dumps(run_sidecar_backup_retention()))",
    ],
    # 2026-04-26 stale-atoms auto-obsolete: only targets atoms with a real
    # supersede chain + 60d expired + never reinforced. Conservative.
    "obsolete_expired_atoms": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from db_maintenance import run_obsolete_expired_atoms; import json; print(json.dumps(run_obsolete_expired_atoms()))",
    ],
    # v3 Phase 6: weekly entity canonicalization proposal (dry-run only).
    "canonicalize_entities_dryrun": [
        _py,
        f"{_bd}/cli/canonicalize_entities.py",
        "--threshold",
        "0.92",
    ],
    "lora_ab_gate": [_py, f"{_bd}/cli/lora_ab_gate.py"],
    # Phase C: eval auto-growth pipeline (run after lora_ab_gate but before sm2_nightly)
    "eval_holdout_promote": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from eval_holdout_promote import run; import json; print(json.dumps(run()))",
    ],
    "eval_holdout_audit": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from eval_holdout_audit import run; import json; print(json.dumps(run()))",
    ],
    # Phase E: SLO check job — runs every 5 min, dispatches Telegram alerts on breach
    "slos_check": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from slos import run; import json; print(json.dumps(run()))",
    ],
    # hnsw_tune dispatcher retired 2026-04-17 — duplicate of hnsw_adaptive.
    # See brain_core/scheduler.py for the removal rationale.
    # Phase 2D: SessionEnd outbox drainer — replays envelopes that the
    # post_session.sh fire-and-forget call missed (brain down, orphan inflight,
    # no SessionEnd at all). Documented as 5-min job in CRON_MAP/RUNBOOK; the
    # schedule entry was missing prior to this commit, so failed envelopes
    # silently piled up in pending/.
    "outbox_drain": [_py, f"{_bd}/cli/outbox_drain.py"],
    # M7-WS2a: Docling-based PDF ingestion (daily 5:30am).
    "pdf_ingest": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/ingest'); from pdfs import run; import json; print(json.dumps(run()))",
    ],
    # M8.5: GraphRAG community summaries (Sun 5:00am).
    "community_summaries": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from community_summaries import run; import json; print(json.dumps(run()))",
    ],
    # M7-WS2b: image OCR + caption ingestion (daily 5:45am).
    "image_ingest": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/ingest'); from images import run; import json; print(json.dumps(run()))",
    ],
    # Phase M6: weekly trust score recompute for web_source_trust table —
    # aggregates per-domain useful/wrong outcomes from web_search_results.
    "web_source_trust_recompute": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from web_search import recompute_domain_trust; import json; print(json.dumps(recompute_domain_trust()))",
    ],
    "reindex": ["/bin/zsh", f"{_bd}/cli/reindex.sh"],
    # Maintenance
    "log_rotation": [_py, f"{_bd}/brain_core/maintenance.py", "all_cleanup"],
    "embed_cache_prune": [_py, f"{_bd}/brain_core/embed_cache.py"],
    # 2026-04-16 Tier 2: quarterly prune of raw/orphaned/ — previously grew
    # without bound because pipeline_auto only ever MOVED inbox → orphaned.
    "prune_raw_orphaned": [_py, f"{_bd}/brain_core/maintenance.py", "prune_raw_orphaned"],
    # 2026-04-16 Tier 2: monthly re-examine of rejected proposals against
    # fresh corroborating evidence. Rejections are no longer permanent.
    "re_examine_rejected": [_py, f"{_bd}/pipeline/re_examine_rejected.py"],
    # 2026-04-16 Tier 3 #4: nightly retrieval-induced inhibition (Bjork).
    # Decrements confidence of atoms that consistently lose top-rank to
    # another atom on the same query cue. Breaks rich-get-richer spiral.
    "retrieval_inhibition": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from retrieval_inhibition import run_inhibition_pass; import json; print(json.dumps(run_inhibition_pass()))",
    ],
    # 2026-04-16 Tier 3 #3: weekly Platt confidence calibration.
    "confidence_calibration": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from confidence_calibration import run; import json; print(json.dumps(run()))",
    ],
    # 2026-04-17 Phase 3: weekly learned-to-rank (LogisticRegression) fit.
    "ltr_train": [_py, f"{_bd}/cli/ltr_train.py"],
    # 2026-04-16 Tier 3 #7: weekly dream replay (Wagner 2004) — generative
    # recombination of distant entity pairs via Sage into low-confidence
    # conjecture atoms. Source of analogical insight.
    "dream_replay": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from dream_replay import run; import json; print(json.dumps(run(), ensure_ascii=False))",
    ],
    # 2026-05-12: read-side validator for dream_replay conjectures. Promotes
    # conjectures with corroborating evidence, expires barren ones after 21d.
    "conjecture_validate": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from conjecture_validator import run; import json; print(json.dumps(run(), ensure_ascii=False))",
    ],
    # 2026-05-12: D7 per-atom recall quality. Aggregates action_audit
    # (retrieved_atom_ids x outcome) into per-atom accuracy. Surfaces
    # consistently-wrong atoms for review.
    "atom_recall_quality": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from atom_recall_quality import run; import json; print(json.dumps(run(), ensure_ascii=False))",
    ],
    # 2026-05-12: D9 counterfactual replay. Picks 1 failed decision/day,
    # dispatches Sage via codex (subscription) to imagine alternatives.
    # Bounded to 1/day for cost control; can scale once outcomes prove out.
    "counterfactual_replay": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from counterfactual import run_daily; import json; print(json.dumps(run_daily(max_dispatches=1), ensure_ascii=False))",
    ],
    # 2026-04-16 Tier 3 #5: weekly Friston free-energy schema revision —
    # clusters repeated prediction errors, emits raw/inbox proposals for
    # Sage-level schema rewrite instead of atom-level punishment.
    "schema_revision": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from schema_revision import run; import json; print(json.dumps(run(), ensure_ascii=False))",
    ],
    # 2026-04-16 Tier 3 #9: weekly RAPTOR hierarchical summary tree
    # (Sarthi 2024). Builds multi-level abstraction over active canonical.
    "raptor_build": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from raptor import build_tree; import json; print(json.dumps(build_tree(), ensure_ascii=False))",
    ],
    "canonical_index": [
        "/bin/bash",
        "-c",
        'SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret) && curl -sf -X POST -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/index/rebuild',
    ],
    "graph_consolidation": [_py, f"{_bd}/brain_core/graph_consolidation.py"],
    "stale_cleanup": [_py, f"{_bd}/brain_core/maintenance.py", "stale_cleanup"],
    "memory_observability": [_py, f"{_bd}/pipeline/memory_observability.py"],
    "lint_memory": [_py, f"{_bd}/pipeline/lint_memory.py"],
    "canonical_lint": [_py, f"{_bd}/synthesis/canonical_lint.py"],
    "entity_pages": [_py, f"{_bd}/synthesis/entity_pages.py", "--limit", "1"],
    "answer_canonicalize": [_py, f"{_bd}/synthesis/answer_canonicalize.py"],
    "canonical_compaction": [_py, f"{_bd}/synthesis/canonical_compaction.py"],
    "graph_rebuild_mentions": [
        "/bin/bash",
        "-c",
        f"BRAIN_ATOMS_ENABLED=true {_py} {_bd}/cli/rebuild_atom_entity.py",
    ],
    "graph_backfill_co_mention": [_py, f"{_bd}/cli/backfill_co_mention.py"],
    "canonical_merge_draft": [_py, f"{_bd}/synthesis/canonical_merge_draft.py", "--limit", "3"],
    "canonical_quality_filter_report": [_py, f"{_bd}/synthesis/canonical_quality_filter.py", "--dry-run"],
    "near_dedup": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import dedup_semantic_near_duplicates; import json; print(json.dumps(dedup_semantic_near_duplicates()))",
    ],
    "auto_resolve_contradictions": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import auto_resolve_stale_contradictions; import json; print(json.dumps(auto_resolve_stale_contradictions()))",
    ],
    "supersession_chain_cleanup": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import cleanup_supersession_chains; import json; print(json.dumps(cleanup_supersession_chains()))",
    ],
    "feedback_aggregate": [_py, f"{_bd}/brain_core/feedback_aggregator.py"],
    "recall_outcome_label": [_py, f"{_bd}/brain_core/recall_outcome_labeler.py", "--hours", "24"],
    # 2026-05-13: sample 80 / 24h (was 30) so judge_rate moves from ~0.4% of
    # daily recalls toward 1-2%. Run time bounded by recall_judge.MAX_RUN_SECONDS.
    "recall_judge": [_py, f"{_bd}/brain_core/recall_judge.py", "--sample", "80", "--hours", "24"],
    "cross_agent_lessons": [_py, f"{_bd}/brain_core/cross_agent_lessons.py", "--hours", "48"],
    "prompt_survival_report": [_py, f"{_bd}/brain_core/prompt_attribution.py", "--days", "7"],
    "entity_resolution": [_py, f"{_bd}/pipeline/entity_resolution.py", "--apply"],
    "neo4j_backup": [_py, f"{_bd}/cli/backup_neo4j.py"],
    # Backup (also runs via independent launchd plist as a failure-domain safety net)
    "backup": [_py, f"{_bd}/cli/backup_chroma.py"],
    "qdrant_backup": [_py, f"{_bd}/cli/backup_qdrant.py"],
    "backup_restore_drill": [_py, f"{_bd}/cli/backup_restore_drill.py"],
    "backup_verify": [_py, f"{_bd}/cli/backup_verify.py"],
    "openclaw_telegram_target_audit": [
        _py,
        f"{_bd}/cli/audit_openclaw_telegram_targets.py",
        "--json",
    ],
    "openclaw_gateway_start": ["/bin/bash", f"{_bd}/cli/ensure_openclaw_gateway.sh"],
    # reembed_migrator is manual-only (requires positional <collection> arg).
    # Invoke directly: python brain_core/pipeline/reembed_migrator.py <collection_name>
    "proactive_insights": [_py, f"{_bd}/brain_core/pipeline/proactive_linker.py"],
    "skill_extract": [_py, f"{_bd}/brain_core/pipeline/skill_extractor.py"],
    "skill_sync": [_py, f"{_bd}/cli/skill_sync.py"],
    "memory_nudge": [_py, f"{_bd}/brain_core/pipeline/memory_nudge.py"],
    "memory_consolidation": [_py, f"{_bd}/brain_core/pipeline/memory_consolidation.py"],
    # Phase N4 — CLS sleep consolidation pipeline
    "sleep_consolidate": [_py, f"{_bd}/brain_core/pipeline/sleep_consolidate.py"],
    # Phase N3 — eval holdout auto-graduation (runs before the weekly promote)
    "eval_holdout_graduate": [
        _py,
        f"{_bd}/brain_core/eval_holdout_promote.py",
        "--graduate",
    ],
    "llm_usage_purge": [_py, f"{_bd}/brain_core/pipeline/llm_usage_purge.py"],
    "event_compressor": [_py, f"{_bd}/brain_core/pipeline/event_compressor.py"],
    "slo_monitor": [_py, f"{_bd}/brain_core/slo_monitor.py"],
    "hnsw_adaptive": [_py, f"{_bd}/brain_core/pipeline/hnsw_tuner.py", "--adaptive"],
    "memory_leak_detector": [_py, f"{_bd}/brain_core/pipeline/memory_leak_detector.py"],
    "training_pairs_generate": [_py, f"{_bd}/brain_core/pipeline/training_pair_generator.py"],
    # Round 9 — Tier 2 new pipelines
    "code_index_refresh": [_py, f"{_bd}/ingest/code_repos.py"],
    "gap_detection": [_py, f"{_bd}/brain_core/pipeline/gap_detector.py"],
    "trust_recompute": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import recompute_trust_scores; import json; print(json.dumps(recompute_trust_scores()))",
    ],
    "focus_aggregate": [_py, f"{_bd}/brain_core/pipeline/focus_aggregator.py"],
    # Round 10 Wave 2 — episodic memory binding (CoALA-style)
    "episode_binder": [_py, f"{_bd}/brain_core/pipeline/episode_binder.py"],
    # Round 10 Wave 3 — synaptic pruning of atrophied memories (MemoryBank)
    "memory_pruning": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import prune_atrophied_memories; import json; print(json.dumps(prune_atrophied_memories(dry_run=True)))",
    ],
    "memory_pruning_active": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import prune_atrophied_memories; import json; print(json.dumps(prune_atrophied_memories(dry_run=False, max_age_days=120, compress_with_gist=True)))",
    ],
    "stale_superseded_cleanup": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import cleanup_stale_superseded; import json; print(json.dumps(cleanup_stale_superseded()))",
    ],
    # LoRA fine-tuning — manual trigger only, behind BRAIN_FINETUNE_ENABLED flag.
    # Must run in the brain venv since sentence-transformers/peft/torch are only
    # installed there, not in the system Python.
    # Writes candidate adapter to models/adapters/lora_v_candidate/ so the
    # weekly lora_ab_gate can find it (it defaults to that path). Prior to
    # 2026-04-23 this called brain_finetune with no args, inheriting its
    # default output=lora_v1 — which clobbered the live adapter AND never
    # produced a candidate for the gate, so the weekly A/B loop silently
    # skipped for weeks. Audit surfaced in 2026-04-23 session.
    "embed_finetune": [
        f"{_bd}/.venv/bin/python3",
        f"{_bd}/cli/brain_finetune.py",
        "--output",
        f"{_bd}/models/adapters/lora_v_candidate",
    ],
    # Infra validation + health reports
    "infra_validation": [_py, f"{_bd}/brain_core/maintenance.py", "validate_infra"],
    "memory_health_report": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import memory_health_report; import json; print(json.dumps(memory_health_report()))",
    ],
    "content_quality_slo": [
        _py,
        "-c",
        f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from slo_monitor import check_content_quality; import json; print(json.dumps(check_content_quality()))",
    ],
}


_running_jobs: dict[str, subprocess.Popen] = {}
_running_jobs_lock = threading.Lock()
_CRITICAL_JOBS = {"personal_ingest", "backup", "canonical_pipeline", "reindex"}
_JOB_TIMEOUT_SECONDS = {
    # The brain-loop contract says ticks are short-lived. A wedged tick used to
    # survive for the generic 1h subprocess cap, hold the process lock, and
    # tempt wake/scheduler paths into spawning more work. Keep this well above
    # the 30s in-process SIGALRM guard but far below the generic cap.
    "brain_loop_tick": 45,
    # Proactive checks are useful but non-critical. They call subscription CLI
    # LLMs and can wedge behind provider timeouts; do not let one run pin the
    # llm resource slot or hold ~250MB RSS for the generic 1h cap.
    "proactive_check": 900,
}


def dispatch_job(job_name: str) -> int:
    """Fire-and-forget launch of a registered job. Returns the child PID."""
    cmd = JOB_REGISTRY.get(job_name)
    if not cmd:
        raise ValueError(f"unknown job '{job_name}'")
    log_dir = BRAIN_DIR / "logs" / "jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{job_name}.log"
    stderr_path = log_dir / f"{job_name}.err.log"
    stdout_f = None
    stderr_f = None
    try:
        stdout_f = stdout_path.open("ab")
        stderr_f = stderr_path.open("ab")
        with _running_jobs_lock:
            existing = _running_jobs.get(job_name)
            if existing and existing.poll() is None:
                raise ValueError(f"job '{job_name}' is already running (pid={existing.pid})")
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
            )
            _running_jobs[job_name] = proc
        threading.Thread(target=_wait_for_job, args=(job_name, proc, stderr_path), daemon=True).start()
        return proc.pid
    except Exception as e:
        _metrics_buf.record_job_result(job_name, ok=False, error=str(e))
        raise
    finally:
        if stdout_f:
            stdout_f.close()
        if stderr_f:
            stderr_f.close()


def _wait_for_job(job_name: str, proc: subprocess.Popen, stderr_path: Path) -> None:
    """Background thread: wait for job completion, record exit code, alert on failure."""
    try:
        proc.wait(timeout=_JOB_TIMEOUT_SECONDS.get(job_name, 3600))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    exit_code = proc.returncode
    error_msg: str | None = None
    if exit_code != 0:
        try:
            error_msg = stderr_path.read_text()[-500:] if stderr_path.exists() else f"exit code {exit_code}"
        except Exception:
            error_msg = f"exit code {exit_code}"

    _metrics_buf.record_job_result(job_name, ok=(exit_code == 0), error=error_msg or "")

    if exit_code == 0 and "backup" in job_name:
        _metrics_buf.record_backup_result(True)

    if exit_code == 0 and job_name == "profile_regen":
        with contextlib.suppress(Exception):
            boot_context.flush_cache()

    if exit_code != 0 and job_name in _CRITICAL_JOBS:
        with contextlib.suppress(Exception):
            from cli_llm import dispatch as _cli_dispatch

            _cli_dispatch(
                agent="sage",
                message=(
                    "[BRAIN JOB ACTION] Critical scheduler job failed. "
                    "Investigate or queue safe remediation; do not ask Chris unless a true "
                    "credential/irreversible blocker remains. "
                    f"Job='{job_name}' exit_code={exit_code}. Error: {(error_msg or '')[:200]}"
                ),
                thinking="off",
                timeout=30,
                openclaw_agent="sage",
                backlog_kind="proactive",
                backlog_payload={
                    "source": "job_registry",
                    "job_name": job_name,
                    "exit_code": exit_code,
                    "error": error_msg or "",
                },
            )

    pending = brain_scheduler._pending_completions.pop(job_name, None)
    if pending:
        start_ts, row_id = pending
        brain_scheduler.record_completion(job_name, row_id, start_ts, error_msg)

    with _running_jobs_lock:
        _running_jobs.pop(job_name, None)
