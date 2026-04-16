"""brain_core/default_levels.py - seed policy for the L0-L3 autonomy gate.

Action-kind namespace + default levels + quiet-hours / execution-windows /
deny list. This module is pure data: no IO, no side-effects. autonomy.py
loads it on boot and surfaces overrides via brain_config.

Levels:
    L0 — never run
    L1 — propose only (queue task with status=pending_approval, requires_ack)
    L2 — notify then act (Telegram alert, configurable lag, then proceed)
    L3 — immediate execution
"""

from __future__ import annotations

DEFAULT_LEVELS: dict[str, str] = {
    # ── Self-heal lane ───────────────────────────────────────
    "heal.log_rotate": "L3",  # always-safe, runs in quiet hours too
    "heal.vacuum_embed_cache": "L3",
    "heal.reindex": "L2",
    "heal.memory_consolidation": "L2",
    "heal.cron_failure_inspect": "L1",  # propose only
    # ── Task queue lanes ─────────────────────────────────────
    "task.dispatch": "L2",
    "task.approve": "L2",
    # ── Reasoning & planning ─────────────────────────────────
    "reasoning.multihop": "L2",
    "goal.decompose": "L1",  # propose-only by default
    # ── Triggers (per-name override permitted via brain_config) ───
    "trigger.fire": "L2",
    # ── SLO remediation ──────────────────────────────────────
    "slo.remediate": "L2",
    # ── LLM dispatch (gated by persistent CB) ────────────────
    "llm.dispatch": "L3",
    # ── Advisory (read-only proposals — always safe) ─────────
    "advise.daily_brief": "L3",
    "advise.memory_lint": "L3",
    # ── v3 brain_loop action kinds (continuous executive cortex) ─
    # Observe-only: loop notices something and writes journal. No external effect.
    "brain_loop.observe": "L3",
    # Propose: write candidate to eval_proposals for weekly review.
    "brain_loop.propose_eval_candidate": "L2",
    # Dispatch agent for check-in on stalled goal (owner-agent goals).
    "brain_loop.dispatch_agent_checkin": "L2",
    # Dispatch agent for contradiction or gap investigation (usually Sage).
    "brain_loop.dispatch_agent_investigation": "L2",
    # Push urgent content into active Claude Code session via doorbell file.
    "brain_loop.push_to_claude": "L2",
    # Send urgent Telegram alert via Jenna dispatch (breaker, SLO, stalled goal).
    "brain_loop.telegram_urgent": "L2",
    # Self-modification: demote or promote autonomy level for an action kind.
    # Stays L1 by default so Chris must approve until the proposer earns trust.
    "brain_loop.self_modify_autonomy": "L1",
    # Self-modification: patch intent_routes.yaml with a new route.
    "brain_loop.self_modify_route": "L1",
    # Self-modification: add or adjust scheduler job.
    "brain_loop.self_modify_scheduler": "L1",
    # llm_backlog drain: event-driven catch-up when llm.dispatch breaker
    # just closed. Safe to auto-execute — it just calls handlers that
    # already exist, no new side effects beyond what normal LLM work does.
    "brain_loop.drain_llm_backlog": "L3",
    # ── Hard L0 (never auto-execute) ─────────────────────────
    "write.canonical": "L0",  # canonical promotion is human-only
}

# Hardcoded for security — Chris must approve a code change to add a new
# always-blocked prefix. Soft denies live in brain_config.
DENY_PREFIXES: tuple[str, ...] = ("cloudflared",)


# Quiet hours: every kind not in `exceptions` gets demoted L3→L2, L2→L1
# inside this window. Wall-clock evaluated in the configured timezone.
QUIET_HOURS: dict[str, object] = {
    "start": "23:00",
    "end": "07:00",
    "tz": "America/Los_Angeles",
    "exceptions": ["heal.log_rotate", "heal.vacuum_embed_cache"],
}


# Unified execution windows — resolves the quiet-hours / work-hours conflict.
# CLAUDE.md rule: NO heavy Ollama/Chroma jobs between 9am-6pm PST. Reindex
# 2x/day, personal ingest 3x/day. Encoded here as `night` window membership.
#
# Possible window labels:
#   "any"   - never blocked by execution-window
#   "night" - only allowed 23:00-07:00 PT
#   "day"   - only allowed 07:00-23:00 PT
EXECUTION_WINDOWS: dict[str, list[str]] = {
    "heal.reindex": ["night"],
    "heal.memory_consolidation": ["night"],
    "heal.vacuum_embed_cache": ["night"],
    "reasoning.multihop": ["any"],
    "task.dispatch": ["any"],
    "advise.daily_brief": ["any"],
    "trigger.fire": ["any"],
    "llm.dispatch": ["any"],
    "slo.remediate": ["any"],
    # brain_loop observation/proposing is safe 24/7.
    "brain_loop.observe": ["any"],
    "brain_loop.propose_eval_candidate": ["any"],
    # Loop can nudge Chris or dispatch any time (doorbell / Telegram).
    "brain_loop.push_to_claude": ["any"],
    "brain_loop.telegram_urgent": ["any"],
    "brain_loop.dispatch_agent_checkin": ["any"],
    "brain_loop.dispatch_agent_investigation": ["any"],
    # Self-mods should only land off-hours so they're stable before Chris wakes up.
    "brain_loop.self_modify_autonomy": ["night"],
    "brain_loop.self_modify_route": ["night"],
    "brain_loop.self_modify_scheduler": ["night"],
    # Catch-up drain: event-driven, must run any time quota returns.
    "brain_loop.drain_llm_backlog": ["any"],
}


# Notify lag (L2 only): seconds between Telegram alert and execution.
# Heal actions get a longer lag because Chris may be asleep.
NOTIFY_LAG_S: dict[str, int] = {
    "heal.reindex": 300,  # 5 min
    "heal.memory_consolidation": 300,
    "heal.log_rotate": 0,  # L3 anyway, but if demoted: act immediately
    "heal.vacuum_embed_cache": 0,
    "task.dispatch": 30,
    "task.approve": 30,
    "reasoning.multihop": 30,
    "trigger.fire": 30,
    "slo.remediate": 30,
}


def notify_lag_for(kind: str) -> int:
    """Return notify lag for an action kind, defaulting to 30 s."""
    return NOTIFY_LAG_S.get(kind, 30)
