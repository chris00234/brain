"""brain_core/brain_loop.py — the continuous executive cortex.

Biology: prefrontal cortex + default mode network. Fires every 60 s via the
existing AsyncIOScheduler, and on-demand via /tmp/.brain_loop_wake file watcher
(rising edge). Every tick runs a bounded PERCEIVE → REFLECT → DECIDE → ACT →
JOURNAL cycle and writes one line to logs/brain_loop_journal.jsonl.

This is the module that turns brain from "reactive with per-turn injection"
into a real cortical engine that thinks continuously, holds goals across
sessions, and interrupts the world when it decides something matters. Without
this loop, even a perfectly-wired /recall/active only reacts faster — it
never *initiates*.

Key properties:
- No LLM in the hot path. LLM only fires on DISPATCH_AGENT decisions through
  openclaw_dispatch, which is rate-limited and autonomy-gated.
- Hard wall-clock budget per tick (TICK_BUDGET_S = 10 s). If exceeded, current
  tick completes best-effort and next tick fires at the normal interval.
- Single-writer lock: only one tick runs at a time (file lock on the journal).
  A second tick starting while the first is active no-ops.
- Every action goes through brain_core.autonomy.authorize(). If the gate
  returns L0, the action is downgraded to OBSERVE_ONLY. If L1, it's downgraded
  to PROPOSE (written to eval_proposals).
- Per-(kind, subject) rate limit: 3 fires per 1-hour window.
- Respects BRAIN_AUTOPILOT_DISABLED=1 env kill — every tick becomes a no-op.

Trigger: scheduler job `brain_loop_tick` at IntervalTrigger(seconds=60) +
  /tmp/.brain_loop_wake file watcher (touched by claude_boot.sh post-turn,
  scheduler._alert_failure on 3-fail breaker, self_heal dispatcher).
Consumer: journal file (observability), action_audit table (audit trail),
  eval_proposals (L1 proposals), doorbell file (L2 push to Claude),
  openclaw_dispatch (L2 agent dispatch).
Effect: goals advance without Chris asking, stalled tasks get interventions,
  breaker trips Telegram immediately, contradictions reach Sage autonomously,
  miss clusters get proposed as intent routes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

# Top-level imports for sibling modules — all of these are safe (no circular
# deps back into brain_loop) and we want them eager so a failing import is
# caught at service boot, not 60 s into the first tick.
try:
    import intent_miss_scan
except ImportError:
    intent_miss_scan = None  # type: ignore[assignment]

try:
    from proactive import check_decision_contradictions, get_current_insights
except ImportError:
    get_current_insights = None  # type: ignore[assignment]
    check_decision_contradictions = None  # type: ignore[assignment]

try:
    # 2026-04-17: revert to real openclaw_dispatch for agent work. cli_llm
    # is correct for stateless LLM calls (HyDE, extraction, classification),
    # but brain_loop.DISPATCH_AGENT hands a task to a specialist agent
    # (Sage investigates contradictions, Jenna pushes Telegram summaries).
    # CLI path is a stateless LLM call — it loses the agent's AGENTS.md
    # persona, workspace memory, MCP tools, and Jenna→Telegram routing.
    # Agent dispatches must go through the OpenClaw gateway.
    from openclaw_dispatch import dispatch as _openclaw_dispatch
except ImportError:
    _openclaw_dispatch = None  # type: ignore[assignment]

try:
    from autonomy import authorize as _authorize
except ImportError:
    _authorize = None  # type: ignore[assignment]

try:
    from atoms_store import insert_action_audit as _insert_action_audit
except ImportError:
    _insert_action_audit = None  # type: ignore[assignment]

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
JOURNAL_PATH = BRAIN_LOGS_DIR / "brain_loop_journal.jsonl"
# WAKE_FILE removed (MR8 fix 2026-04-14): the /tmp/.brain_loop_wake
# polling path was dead code — no reader ever called check_wake.
# claude_boot.sh still touches the file (harmless no-op) for possible
# future use by a watchdog-based watcher.
DOORBELL_DIR = Path("/tmp")

log = logging.getLogger("brain.brain_loop")

TICK_BUDGET_S = 10.0
RATE_LIMIT_WINDOW_S = 3600
RATE_LIMIT_MAX = 3
STALLED_GOAL_HOURS = 2.0
ACCURACY_DROP_THRESHOLD = 0.6

# Per-observation-kind cooldowns — how long after a (kind, subject) fired
# before the SAME pair can surface again. These override the in-memory
# rate limit which resets per subprocess invocation.
SEEN_COOLDOWN_S: dict[str, int] = {
    "contradiction": 24 * 3600,  # don't re-surface same contradiction for 24h
    "accuracy_drop": 6 * 3600,  # domain-level, 6h between reports
    "breaker_open": 1800,  # 30 min between same-breaker alerts
    "stalled_goal": 2 * 3600,  # 2h between nudges on same goal
    "recall_miss": 3600,  # 1h between same-session miss reports
    "proactive_urgent": 6 * 3600,  # mirror proactive TTL
    "high_salience_event": 1800,
    "claude_active": 60,  # side-channel; just throttle
    "stale_atom": 7 * 24 * 3600,  # stale is slow-moving, 1 week cooldown
    "llm_usage_spike": 3600,  # 1 hour — spike condition evaluates hourly anyway
    "llm_breaker_closed": 60,  # one-shot edge, 60s debounce
    "llm_backlog_overflow": 1800,  # 30 min
    "llm_backlog_stale": 6 * 3600,  # 6h
}
DEFAULT_COOLDOWN_S = 3600
SEEN_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS brain_loop_seen (
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    last_fired_at REAL NOT NULL,
    fire_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at REAL NOT NULL,
    PRIMARY KEY (kind, subject)
);
CREATE INDEX IF NOT EXISTS idx_brain_loop_seen_ts ON brain_loop_seen(last_fired_at);
"""

_seen_schema_ready = False


def _ensure_seen_schema() -> None:
    global _seen_schema_ready
    if _seen_schema_ready:
        return
    try:
        with _connect_autonomy() as conn:
            conn.executescript(SEEN_TABLE_DDL)
            conn.commit()
        _seen_schema_ready = True
    except sqlite3.Error as e:
        log.warning("brain_loop_seen schema init failed: %s", e)


def _seen_recently(kind: str, subject: str) -> bool:
    """Check persistent rate limiter. Returns True if (kind, subject) was fired
    within its cooldown window and should be skipped."""
    _ensure_seen_schema()
    cooldown = SEEN_COOLDOWN_S.get(kind, DEFAULT_COOLDOWN_S)
    try:
        with _connect_autonomy() as conn:
            row = conn.execute(
                "SELECT last_fired_at FROM brain_loop_seen WHERE kind=? AND subject=?",
                (kind, subject),
            ).fetchone()
            if not row:
                return False
            return (time.time() - row["last_fired_at"]) < cooldown
    except sqlite3.Error:
        return False


def _mark_seen(kind: str, subject: str) -> None:
    _ensure_seen_schema()
    now_ts = time.time()
    try:
        with _connect_autonomy() as conn:
            conn.execute(
                "INSERT INTO brain_loop_seen (kind, subject, last_fired_at, fire_count, first_seen_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(kind, subject) DO UPDATE SET "
                "  last_fired_at = excluded.last_fired_at, "
                "  fire_count = fire_count + 1",
                (kind, subject, now_ts, now_ts),
            )
            conn.commit()
    except sqlite3.Error as _exc:
        log.debug("silenced exception in brain_loop.py: %s", _exc)


def _filter_seen(observations: list[Observation]) -> list[Observation]:
    """Drop observations whose (kind, subject) is within cooldown from a
    prior fire. Mark the survivors as seen. Idempotent if sensors re-fire.

    Kept for backwards-compat with unit tests. The tick path uses
    _filter_already_seen + _mark_observations_fired instead, which separates
    "drop if seen" from "mark as fired" so observations dropped at the
    autonomy/rate-limit gate aren't silently lost (F4 fix).
    """
    keep: list[Observation] = []
    for o in observations:
        if _seen_recently(o.kind, o.subject):
            continue
        _mark_seen(o.kind, o.subject)
        keep.append(o)
    return keep


def _filter_already_seen(observations: list[Observation]) -> list[Observation]:
    """Drop observations whose (kind, subject) is within cooldown. Does NOT
    mark survivors as seen — caller must call _mark_observations_fired after
    the gate so retry semantics survive rejections."""
    return [o for o in observations if not _seen_recently(o.kind, o.subject)]


def _mark_observations_fired(
    observations: list[Observation],
    decisions: list[Decision],
    approved: list[Decision],
) -> None:
    """Mark an observation seen iff it either produced no decision (reflect
    consciously chose no-op) OR its decision produced an *effective* action.

    HR7 fix (2026-04-14): previously any decision in `approved` advanced
    the cooldown, including OBSERVE_ONLY and PROPOSE downgrades. This
    meant a contradiction dispatched-and-downgraded-to-propose during
    quiet hours got marked seen for 24h and never re-surfaced for a real
    dispatch after quiet hours ended. Now only real actions (dispatch,
    push, telegram, self_modify) advance the cooldown. OBSERVE_ONLY and
    PROPOSE do not — the obs re-enters reflect next tick and can be
    promoted back to a real action when autonomy allows.

    EXCEPTION: if _reflect produced NO decision, the obs is still marked
    seen — reflect deliberately chose no-op and re-running it would be
    wasteful (no new signal).
    """
    effective_kinds = {
        DecisionKind.DISPATCH_AGENT,
        DecisionKind.PUSH_TO_CLAUDE,
        DecisionKind.TELEGRAM_ALERT,
        DecisionKind.SELF_MODIFY,
    }
    effective_keys = {
        (d.observation.kind, d.observation.subject) for d in approved if d.kind in effective_kinds
    }
    decided_keys = {(d.observation.kind, d.observation.subject) for d in decisions}
    for o in observations:
        key = (o.kind, o.subject)
        if key not in decided_keys or key in effective_keys:
            _mark_seen(*key)


# ── Types ─────────────────────────────────────────────────────────


class DecisionKind(Enum):
    OBSERVE_ONLY = "observe"
    PROPOSE = "propose"
    DISPATCH_AGENT = "dispatch_agent"
    PUSH_TO_CLAUDE = "push_to_claude"
    TELEGRAM_ALERT = "telegram_alert"
    SELF_MODIFY = "self_modify"
    DO_NOTHING = "do_nothing"


@dataclass
class Observation:
    kind: str
    subject: str
    evidence: dict[str, Any] = field(default_factory=dict)
    salience: float = 0.5
    ts: str = ""


@dataclass
class Decision:
    observation: Observation
    kind: DecisionKind
    action_payload: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    confidence: float = 0.5
    requires_autonomy: str = "brain_loop.observe"

    def to_journal_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "subject": self.observation.subject,
            "obs_kind": self.observation.kind,
            "reasoning": self.reasoning[:200],
            "confidence": self.confidence,
            "requires_autonomy": self.requires_autonomy,
        }


# ── Helpers ───────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _connect_autonomy() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_brain() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ── Sensors ───────────────────────────────────────────────────────


def _sense_stalled_goals() -> list[Observation]:
    """Goals with status='active' whose updated_at is older than STALLED_GOAL_HOURS.
    Respects next_check_at — skip goals already scheduled for a future check."""
    obs = []
    now = _utcnow()
    try:
        with _connect_autonomy() as conn:
            rows = conn.execute(
                "SELECT id, title, updated_at, owner_agent, next_check_at, "
                "brain_notes, interventions, metadata "
                "FROM goals WHERE status='active'"
            ).fetchall()
    except sqlite3.Error:
        return obs

    for row in rows:
        upd = _parse_iso(row["updated_at"] or "")
        if not upd:
            continue
        age_hours = (now - upd).total_seconds() / 3600
        if age_hours < STALLED_GOAL_HOURS:
            continue
        next_check = _parse_iso(row["next_check_at"] or "")
        if next_check and next_check > now:
            continue  # already scheduled for future check
        obs.append(
            Observation(
                kind="stalled_goal",
                subject=row["id"],
                evidence={
                    "title": row["title"],
                    "age_hours": round(age_hours, 2),
                    "owner": row["owner_agent"] or "chris",
                    "notes_len": len(row["brain_notes"] or ""),
                },
                salience=min(1.0, age_hours / 24),
                ts=_now_iso(),
            )
        )
    return obs


def _sense_recall_misses() -> list[Observation]:
    """Read recent action_audit for /recall/active entries where the NEXT turn
    in the same session contains correction phrases. Uses the same regex as
    intent_miss_scan — keeps the truth in one place."""
    if intent_miss_scan is None:
        return []
    obs: list[Observation] = []
    cutoff = (_utcnow() - timedelta(minutes=30)).isoformat()
    try:
        with _connect_brain() as conn:
            rows = conn.execute(
                "SELECT id, query_text, session_id, created_at "
                "FROM action_audit "
                "WHERE route='/recall/active' AND created_at >= ? "
                "ORDER BY session_id, created_at ASC",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error:
        return obs

    by_session: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_session.setdefault(r["session_id"] or "unknown", []).append(r)

    pattern = intent_miss_scan.CORRECTION_REGEX
    for sid, turns in by_session.items():
        for i in range(1, len(turns)):
            cur_text = turns[i]["query_text"] or ""
            if pattern.search(cur_text):
                prev = turns[i - 1]
                obs.append(
                    Observation(
                        kind="recall_miss",
                        subject=sid,
                        evidence={
                            "prev_prompt": (prev["query_text"] or "")[:300],
                            "correction": cur_text[:300],
                            "prev_ts": prev["created_at"],
                        },
                        salience=0.8,
                        ts=_now_iso(),
                    )
                )
    return obs


def _sense_breaker_open() -> list[Observation]:
    """Read autonomy.db::heal_breakers for rows in state='open'."""
    obs = []
    try:
        with _connect_autonomy() as conn:
            rows = conn.execute(
                "SELECT kind, state, failures, reason, opened_at FROM heal_breakers WHERE state='open'"
            ).fetchall()
    except sqlite3.Error:
        return obs
    for row in rows:
        obs.append(
            Observation(
                kind="breaker_open",
                subject=row["kind"],
                evidence={
                    "state": row["state"],
                    "failures": row["failures"],
                    "reason": row["reason"] or "",
                    "opened_at": row["opened_at"],
                },
                salience=1.0,
                ts=_now_iso(),
            )
        )
    return obs


def _sense_accuracy_drops() -> list[Observation]:
    """Read accuracy_tracker and flag domains with accuracy < threshold and
    sufficient sample size. Only fires if there are new outcomes since last tick."""
    obs = []
    try:
        with _connect_autonomy() as conn:
            rows = conn.execute(
                "SELECT domain, total_recommendations, correct_recommendations, override_count "
                "FROM accuracy_tracker WHERE total_recommendations >= 5"
            ).fetchall()
    except sqlite3.Error:
        return obs
    for row in rows:
        total = row["total_recommendations"] or 0
        correct = row["correct_recommendations"] or 0
        if total < 5:
            continue
        acc = correct / total
        if acc < ACCURACY_DROP_THRESHOLD:
            obs.append(
                Observation(
                    kind="accuracy_drop",
                    subject=row["domain"],
                    evidence={
                        "accuracy": round(acc, 3),
                        "total": total,
                        "correct": correct,
                        "overrides": row["override_count"] or 0,
                    },
                    salience=0.7,
                    ts=_now_iso(),
                )
            )
    return obs


def _sense_contradictions() -> list[Observation]:
    """Check proactive.check_decision_contradictions for new items. Reuses
    existing code rather than duplicating the Chroma query logic."""
    obs = []
    if check_decision_contradictions is None:
        return obs
    try:
        insights = check_decision_contradictions()
    except Exception:
        return obs
    for ins in insights[:5]:
        try:
            obs.append(
                Observation(
                    kind="contradiction",
                    subject=getattr(ins, "id", "unknown"),
                    evidence={
                        "summary": (getattr(ins, "summary", "") or "")[:200],
                        "detail": (getattr(ins, "detail", "") or "")[:400],
                        "severity": getattr(ins, "severity", "info"),
                    },
                    salience=0.6,
                    ts=_now_iso(),
                )
            )
        except Exception as _exc:
            log.debug("silenced exception in brain_loop.py: %s", _exc)
            continue
    return obs


def _sense_claude_active() -> list[Observation]:
    """Detect if a Claude Code session is actively chatting (any /recall/active
    row in the last 2 min). Used to decide whether PUSH_TO_CLAUDE is viable."""
    obs = []
    cutoff = (_utcnow() - timedelta(minutes=2)).isoformat()
    try:
        with _connect_brain() as conn:
            rows = conn.execute(
                "SELECT session_id, MAX(created_at) as last_ts, COUNT(*) as turn_count "
                "FROM action_audit "
                "WHERE route='/recall/active' AND actor='claude' AND created_at >= ? "
                "GROUP BY session_id ORDER BY last_ts DESC LIMIT 3",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error:
        return obs
    for row in rows:
        obs.append(
            Observation(
                kind="claude_active",
                subject=row["session_id"] or "unknown",
                evidence={
                    "last_turn_ts": row["last_ts"],
                    "turn_count_2m": row["turn_count"],
                },
                salience=0.3,
                ts=_now_iso(),
            )
        )
    return obs


def _sense_llm_usage_spike() -> list[Observation]:
    """Watch llm_usage.db for unusual call-rate spikes. A sensor for cost /
    quota hygiene — if some job starts spamming Sage, we surface it as
    `llm_usage_spike` within 60 s so Chris gets a doorbell push.

    Trigger threshold: last-hour call count > 3x the 24h average per-hour rate,
    AND the last-hour count is at least 20 calls (below that it's just noise).
    """
    obs: list[Observation] = []
    llm_db = BRAIN_LOGS_DIR / "llm_usage.db"
    if not llm_db.exists():
        return obs
    try:
        with sqlite3.connect(str(llm_db), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cutoff_24h = (_utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
            cutoff_1h = (_utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")

            daily = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ?",
                (cutoff_24h,),
            ).fetchone()[0]
            hourly = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ?",
                (cutoff_1h,),
            ).fetchone()[0]
    except sqlite3.Error:
        return obs

    baseline_rate = daily / 24.0 if daily > 0 else 0
    # Only fire when current rate is meaningfully above baseline
    if hourly >= 20 and baseline_rate > 0 and hourly > baseline_rate * 3:
        obs.append(
            Observation(
                kind="llm_usage_spike",
                subject=f"last_hour_{hourly}",
                evidence={
                    "hourly_rate": hourly,
                    "baseline_per_hour": round(baseline_rate, 1),
                    "ratio": round(hourly / baseline_rate, 2),
                    "daily_total": daily,
                },
                salience=min(1.0, (hourly / baseline_rate) / 10),
                ts=_now_iso(),
            )
        )
    return obs


def _sense_stale_atoms() -> list[Observation]:
    """Flag semantic-tier atoms that are past their category decay window AND
    have low access_count. Proposes obsoletion via self_modify (L1 by default).

    Decay thresholds mirror memory_lifecycle.py:
      - preference: 90d
      - fact: 180d
      - decision: 365d
    Access threshold: < 2 accesses in last 30d means the atom isn't helping
    retrieval and is a candidate for obsoletion.
    """
    obs: list[Observation] = []
    now = _utcnow()
    try:
        with _connect_brain() as conn:
            # Use created_at as fallback since last_reviewed_at may be NULL
            rows = conn.execute(
                "SELECT id, text, kind, tier, created_at, last_reviewed_at, "
                "       reinforcement_count, confidence "
                "FROM atoms "
                "WHERE tier IN ('semantic','episodic') "
                "  AND (superseded_by IS NULL OR superseded_by = '') "
                "  AND reinforcement_count < 2 "
                "ORDER BY COALESCE(last_reviewed_at, created_at) ASC "
                "LIMIT 20"
            ).fetchall()
    except sqlite3.Error:
        return obs

    for row in rows:
        kind_key = (row["kind"] or "fact").lower()
        decay_days = {"preference": 90, "decision": 365}.get(kind_key, 180)
        anchor = _parse_iso(row["last_reviewed_at"] or row["created_at"] or "")
        if not anchor:
            continue
        age_days = (now - anchor).total_seconds() / 86400
        if age_days < decay_days:
            continue
        obs.append(
            Observation(
                kind="stale_atom",
                subject=row["id"],
                evidence={
                    "kind": kind_key,
                    "age_days": round(age_days, 1),
                    "decay_threshold_days": decay_days,
                    "reinforcement_count": row["reinforcement_count"] or 0,
                    "text_preview": (row["text"] or "")[:120],
                    "tier": row["tier"],
                },
                salience=min(1.0, age_days / (decay_days * 2)),
                ts=_now_iso(),
            )
        )
    return obs


def _sense_proactive_pending() -> list[Observation]:
    """Read existing proactive.get_current_insights for urgent unacted items."""
    obs = []
    if get_current_insights is None:
        return obs
    try:
        urgent = get_current_insights(max_age_hours=6, severity="urgent") or []
    except Exception:
        return obs
    for ins in urgent[:3]:
        obs.append(
            Observation(
                kind="proactive_urgent",
                subject=getattr(ins, "id", "unknown"),
                evidence={
                    "summary": (getattr(ins, "summary", "") or "")[:200],
                    "detail": (getattr(ins, "detail", "") or "")[:400],
                },
                salience=0.95,
                ts=_now_iso(),
            )
        )
    return obs


# Module-level state — tracks the last-seen llm.dispatch breaker state so
# we can fire an event when it transitions open → closed.
_last_llm_breaker_was_open = False


def _sense_llm_breaker_closed() -> list[Observation]:
    """Emit a one-shot observation when llm.dispatch breaker transitions
    from open → closed. Triggers an immediate llm_backlog drain so catch-up
    runs within 60 s of quota returning rather than waiting for the 30-min
    cron tick."""
    global _last_llm_breaker_was_open
    obs = []
    try:
        from breakers import peek_breaker

        snapshot = peek_breaker("llm.dispatch")
        is_open_now = snapshot.is_open
    except Exception:
        return obs

    if _last_llm_breaker_was_open and not is_open_now:
        # Just transitioned open → closed. Emit event + check pending.
        pending = 0
        try:
            from llm_backlog import pending_count

            pending = pending_count()
        except Exception as _exc:
            log.debug("silenced exception in brain_loop.py: %s", _exc)
        if pending > 0:
            obs.append(
                Observation(
                    kind="llm_breaker_closed",
                    subject="llm.dispatch",
                    evidence={"pending_backlog": pending},
                    salience=0.9,
                    ts=_now_iso(),
                )
            )
    _last_llm_breaker_was_open = is_open_now
    return obs


def _sense_llm_backlog_pending() -> list[Observation]:
    """SLO sensor for llm_backlog health.

    2026-04-17 threshold revision: previous rule (pending>100 OR oldest>24h)
    fired false positives during normal post-incident catch-up. Example:
    after a 30-min EMFILE outage queued 300 items, the 30-min cron drain
    processes ~50/cycle — so pending stays >100 for ~3 hours and `oldest`
    stays near the outage timestamp, not indicating any actual problem.

    New rule: only fire `overflow` when BOTH
      - pending > 200 (larger buffer for post-incident catch-up)
      - last 3 drain cycles failed to make progress (drained=0 or failing)
    New rule: only fire `stale` when oldest > 24h AND pending is still
    growing (not draining).

    This avoids paging Chris while the system is self-healing at the
    expected rate."""
    obs = []
    try:
        from llm_backlog import oldest_pending_age_seconds, pending_count

        pending = pending_count()
        oldest = oldest_pending_age_seconds()
    except Exception as exc:
        log.debug("llm_backlog sensor query failed: %s", exc)
        return obs

    # Check if drain is making progress. If recent drain cycles processed
    # items, we're catching up — don't alarm.
    draining_stuck = _is_backlog_drain_stuck()

    if pending > 200 and draining_stuck:
        obs.append(
            Observation(
                kind="llm_backlog_overflow",
                subject="pending_count",
                evidence={
                    "pending": pending,
                    "oldest_age_s": int(oldest),
                    "drain_stuck": True,
                },
                salience=0.8,
                ts=_now_iso(),
            )
        )
    elif oldest > 86400 and draining_stuck:
        obs.append(
            Observation(
                kind="llm_backlog_stale",
                subject="oldest_entry",
                evidence={
                    "pending": pending,
                    "oldest_age_s": int(oldest),
                    "drain_stuck": True,
                },
                salience=0.7,
                ts=_now_iso(),
            )
        )
    return obs


def _is_backlog_drain_stuck() -> bool:
    """Return True if the last 3 drain cycles processed 0 items each.

    Reads from the scheduler's job_history for `llm_backlog_drain`.
    Conservative default: if we can't tell, assume NOT stuck (don't alarm)."""
    try:
        import json as _json

        logs_dir = Path("/Users/chrischo/server/brain/logs/jobs")
        log_path = logs_dir / "llm_backlog_drain.log"
        if not log_path.exists():
            return False
        # Tail-read more than we need (8KB) so that even if the first
        # line is a partial byte-boundary fragment, at least 3 complete
        # lines remain after dropping it.
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="ignore")
        # Drop the first line if the window started mid-file — it may
        # be a byte-boundary fragment that begins with '{' but is truncated.
        raw_lines = tail.splitlines()
        if size > 8192 and raw_lines:
            raw_lines = raw_lines[1:]
        candidate_lines = [line for line in raw_lines if line.strip().startswith("{")]

        # Parse all candidates; a single parse failure no longer short-
        # circuits the whole check. We only declare "stuck" when we have
        # at least 3 successfully parsed recent records AND every one
        # shows zero drain + positive pending.
        parsed = []
        for line in candidate_lines:
            try:
                parsed.append(_json.loads(line))
            except Exception as _exc:
                log.debug("drain log line parse failed (ignored): %s", _exc)
                continue
        if len(parsed) < 3:
            return False
        last_three = parsed[-3:]
        zero_drain_streak = sum(
            1 for r in last_three if r.get("drained", 0) == 0 and r.get("pending_after", 0) > 0
        )
        return zero_drain_streak == 3
    except Exception as exc:
        log.debug("_is_backlog_drain_stuck check failed: %s", exc)
        return False


SENSORS = [
    ("stalled_goals", _sense_stalled_goals),
    ("recall_misses", _sense_recall_misses),
    ("breaker_open", _sense_breaker_open),
    ("accuracy_drops", _sense_accuracy_drops),
    ("contradictions", _sense_contradictions),
    ("claude_active", _sense_claude_active),
    ("proactive_pending", _sense_proactive_pending),
    ("stale_atoms", _sense_stale_atoms),
    ("llm_usage_spike", _sense_llm_usage_spike),
    ("llm_breaker_closed", _sense_llm_breaker_closed),
    ("llm_backlog_health", _sense_llm_backlog_pending),
]


# ── Reflect: map observations to candidate decisions ─────────────


def _find_active_claude_session(observations: list[Observation]) -> str | None:
    for o in observations:
        if o.kind == "claude_active":
            return o.subject
    return None


def _reflect(observations: list[Observation]) -> list[Decision]:
    decisions: list[Decision] = []
    claude_session = _find_active_claude_session(observations)

    for o in observations:
        if o.kind == "stalled_goal":
            owner = o.evidence.get("owner", "chris")
            title = o.evidence.get("title", "")
            age = o.evidence.get("age_hours", 0)
            if owner == "chris" and claude_session:
                decisions.append(
                    Decision(
                        observation=o,
                        kind=DecisionKind.PUSH_TO_CLAUDE,
                        action_payload={
                            "session_id": claude_session,
                            "title": f"Stalled goal: {title}",
                            "content": (
                                f"⚠ Goal '{title}' has been stalled for {age:.1f}h. "
                                f"Consider next steps or mark it paused."
                            ),
                            "priority": "high",
                            "source": "brain_loop.goal_monitor",
                        },
                        reasoning=f"Goal stalled >{STALLED_GOAL_HOURS}h, Chris-owned, Claude session active",
                        confidence=0.8,
                        requires_autonomy="brain_loop.push_to_claude",
                    )
                )
            elif owner != "chris":
                decisions.append(
                    Decision(
                        observation=o,
                        kind=DecisionKind.DISPATCH_AGENT,
                        action_payload={
                            "agent": owner,
                            "message": (
                                f"Your goal '{title}' has been stalled for {age:.1f}h. "
                                f"Please advance it or report blockers in a reply."
                            ),
                        },
                        reasoning=f"Goal stalled >{STALLED_GOAL_HOURS}h, agent={owner}",
                        confidence=0.7,
                        requires_autonomy="brain_loop.dispatch_agent_checkin",
                    )
                )
            else:
                # Chris-owned but no active session — log only
                decisions.append(
                    Decision(
                        observation=o,
                        kind=DecisionKind.OBSERVE_ONLY,
                        reasoning="Chris-owned stalled goal but no active Claude session",
                        confidence=0.5,
                        requires_autonomy="brain_loop.observe",
                    )
                )

        elif o.kind == "recall_miss":
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.PROPOSE,
                    action_payload={
                        "category": "intent_route_candidate",
                        "evidence": o.evidence,
                        "session_id": o.subject,
                    },
                    reasoning="Recall miss detected mid-session",
                    confidence=0.9,
                    requires_autonomy="brain_loop.propose_eval_candidate",
                )
            )

        elif o.kind == "breaker_open":
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.TELEGRAM_ALERT,
                    action_payload={
                        "severity": "urgent",
                        "body": (
                            f"⚠ Breaker OPEN: {o.subject}\n"
                            f"failures={o.evidence.get('failures', '?')}\n"
                            f"reason: {o.evidence.get('reason', 'n/a')[:200]}"
                        ),
                    },
                    reasoning="Breaker open = degraded subsystem, Chris must know",
                    confidence=1.0,
                    requires_autonomy="brain_loop.telegram_urgent",
                )
            )

        elif o.kind == "accuracy_drop":
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.SELF_MODIFY,
                    action_payload={
                        "modification": "autonomy_demote",
                        "domain": o.subject,
                        "to_level": "L1",
                        "reason": f"Accuracy {o.evidence.get('accuracy', 0):.0%} < threshold {ACCURACY_DROP_THRESHOLD:.0%}",
                    },
                    reasoning=f"Accuracy {o.evidence.get('accuracy', 0):.2f} over {o.evidence.get('total', 0)} tasks",
                    confidence=0.8,
                    requires_autonomy="brain_loop.self_modify_autonomy",
                )
            )

        elif o.kind == "contradiction":
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.DISPATCH_AGENT,
                    action_payload={
                        "agent": "sage",
                        "message": (
                            f"New contradiction detected: {o.evidence.get('summary', '')[:200]}\n"
                            f"Please investigate and write a canonical resolution."
                        ),
                    },
                    reasoning="Sage owns contradiction resolution",
                    confidence=0.7,
                    requires_autonomy="brain_loop.dispatch_agent_investigation",
                )
            )

        elif o.kind == "llm_usage_spike":
            # LLM call rate jumped 3x+ — push to Chris immediately so he can
            # investigate which job is spamming. Especially critical if Chris
            # is about to hit a rate-limit window.
            hourly = o.evidence.get("hourly_rate", 0)
            baseline = o.evidence.get("baseline_per_hour", 0)
            ratio = o.evidence.get("ratio", 0)
            if claude_session:
                decisions.append(
                    Decision(
                        observation=o,
                        kind=DecisionKind.PUSH_TO_CLAUDE,
                        action_payload={
                            "session_id": claude_session,
                            "title": "LLM usage spike detected",
                            "content": (
                                f"⚠ Last hour: {hourly} LLM calls. "
                                f"Baseline: {baseline}/hr. Ratio: {ratio}x. "
                                f"Investigate: `brain cost agent` to see which job is spamming."
                            ),
                            "priority": "high",
                            "source": "brain_loop.llm_usage_spike",
                        },
                        reasoning=f"LLM rate {ratio}x baseline — possible runaway job",
                        confidence=0.9,
                        requires_autonomy="brain_loop.push_to_claude",
                    )
                )
            else:
                decisions.append(
                    Decision(
                        observation=o,
                        kind=DecisionKind.TELEGRAM_ALERT,
                        action_payload={
                            "severity": "warning",
                            "body": (
                                f"LLM usage spike: {hourly} calls last hour vs "
                                f"{baseline}/hr baseline ({ratio}x). Check "
                                f"`brain cost agent` for culprit."
                            ),
                        },
                        reasoning="No active Claude session — fallback to Telegram",
                        confidence=0.85,
                        requires_autonomy="brain_loop.telegram_urgent",
                    )
                )

        elif o.kind == "stale_atom":
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.SELF_MODIFY,
                    action_payload={
                        "modification": "atom_obsolete",
                        "atom_id": o.subject,
                        "reason": (
                            f"atom kind={o.evidence.get('kind','?')} age={o.evidence.get('age_days','?')}d > "
                            f"decay={o.evidence.get('decay_threshold_days','?')}d, "
                            f"reinforcement={o.evidence.get('reinforcement_count','?')}"
                        ),
                        "preview": o.evidence.get("text_preview", ""),
                    },
                    reasoning="Stale atom past decay window with low access",
                    confidence=0.7,
                    requires_autonomy="brain_loop.self_modify_route",
                )
            )

        elif o.kind == "proactive_urgent":
            if claude_session:
                decisions.append(
                    Decision(
                        observation=o,
                        kind=DecisionKind.PUSH_TO_CLAUDE,
                        action_payload={
                            "session_id": claude_session,
                            "title": o.evidence.get("summary", "urgent insight")[:80],
                            "content": o.evidence.get("detail", "")[:800],
                            "priority": "critical",
                            "source": "brain_loop.proactive",
                        },
                        reasoning="Urgent proactive insight + active Claude session",
                        confidence=0.9,
                        requires_autonomy="brain_loop.push_to_claude",
                    )
                )

        elif o.kind == "claude_active":
            # Side-channel signal, not a standalone decision
            continue

        elif o.kind == "llm_breaker_closed":
            # Quota just came back — immediately drain the backlog so
            # missed work catches up within 60 s rather than waiting for
            # the 30-min cron. Use SELF_MODIFY kind since we're invoking
            # a brain-owned maintenance action.
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.SELF_MODIFY,
                    action_payload={
                        "modification": "drain_llm_backlog",
                        "pending_backlog": o.evidence.get("pending_backlog", 0),
                    },
                    reasoning="llm.dispatch breaker closed with pending backlog — immediate drain",
                    confidence=0.95,
                    requires_autonomy="brain_loop.drain_llm_backlog",
                )
            )

        elif o.kind in ("llm_backlog_overflow", "llm_backlog_stale"):
            # Queue is piling up — surface to Chris. Urgent if overflow,
            # warn if just stale.
            severity = "urgent" if o.kind == "llm_backlog_overflow" else "warn"
            pending = o.evidence.get("pending", 0)
            age_h = o.evidence.get("oldest_age_s", 0) / 3600
            decisions.append(
                Decision(
                    observation=o,
                    kind=DecisionKind.TELEGRAM_ALERT,
                    action_payload={
                        "severity": severity,
                        "body": (
                            f"⚠ llm_backlog {o.kind.split('_', 2)[-1]}: "
                            f"pending={pending}, oldest={age_h:.1f}h. "
                            f"Drain cron may be stuck or LLM still degraded."
                        ),
                    },
                    reasoning="llm_backlog SLO breach — Chris needs to know",
                    confidence=0.85,
                    requires_autonomy="brain_loop.telegram_urgent",
                )
            )

    return decisions


# ── Decide: rate limit + autonomy gate ───────────────────────────

_rate_limits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _rate_limit_check(key: str) -> bool:
    """Return True if this action may fire, False if rate-limited."""
    now_ts = time.time()
    with _rate_lock:
        recent = _rate_limits.get(key, [])
        recent = [t for t in recent if now_ts - t < RATE_LIMIT_WINDOW_S]
        if len(recent) >= RATE_LIMIT_MAX:
            _rate_limits[key] = recent
            return False
        recent.append(now_ts)
        _rate_limits[key] = recent
    return True


def _decide(decisions: list[Decision]) -> list[Decision]:
    """Filter by autonomy gate + rate limit. May downgrade kinds."""
    if _authorize is None:
        return []

    approved: list[Decision] = []
    for d in decisions:
        try:
            decision = _authorize(d.requires_autonomy)
            level = decision.level
        except Exception as e:
            log.debug("autonomy check failed for %s: %s", d.requires_autonomy, e)
            level = "L0"

        # Downgrade per level
        if level == "L0":
            d.kind = DecisionKind.OBSERVE_ONLY
            d.reasoning += " [downgraded L0]"
        elif level == "L1":
            # L1 means propose only — override to PROPOSE unless already OBSERVE
            if d.kind not in (DecisionKind.OBSERVE_ONLY, DecisionKind.PROPOSE):
                # HR6 fix (2026-04-14): when downgrading to PROPOSE,
                # rewrite action_payload into an eval_proposal shape so
                # _write_eval_proposal can build a meaningful fingerprint
                # and query row. Previously downgraded dispatches kept
                # their {agent, message} payload and collapsed into one
                # meaningless duplicate row with empty query/expected.
                original_payload = d.action_payload
                d.action_payload = {
                    "evidence": {
                        "observation_kind": d.observation.kind,
                        "observation_subject": d.observation.subject,
                        "observation_evidence": d.observation.evidence,
                        "intended_kind": d.kind.value,
                        "intended_payload": original_payload,
                    },
                    "reasoning": d.reasoning,
                    "confidence": d.confidence,
                }
                d.kind = DecisionKind.PROPOSE
                d.reasoning += " [downgraded L1]"

        # Rate limit per (kind, subject) pair
        key = f"{d.kind.value}:{d.observation.subject}"
        if not _rate_limit_check(key):
            # HR8 fix (2026-04-14): rate-limited observation shouldn't
            # re-fire every tick for an hour. Mark it seen with a short
            # cooldown that matches the rate-limit window so reflect
            # doesn't waste CPU re-mapping the same obs repeatedly.
            _mark_seen_with_short_cooldown(d.observation.kind, d.observation.subject)
            continue

        approved.append(d)
    return approved


def _mark_seen_with_short_cooldown(kind: str, subject: str) -> None:
    """HR8 fix: mark an observation seen with a 5-minute cooldown (shorter
    than the normal 6h-24h) so rate-limited observations don't flood the
    sensor→reflect loop. The rate limit itself is 3/hour per key, so 5
    minutes is enough to let the rate counter decay partially."""
    try:
        _ensure_seen_schema()
        now_ts = time.time()
        # Subtract ~50 min from last_fired_at so the normal cooldown
        # check (elapsed vs SEEN_COOLDOWN_S[kind]) treats this as "fired
        # an hour ago" — giving us ~5min de-facto cooldown before the
        # full cooldown resolves via kind-specific time.
        shifted_ts = now_ts - (55 * 60)
        with _connect_autonomy() as conn:
            conn.execute(
                "INSERT INTO brain_loop_seen "
                "(kind, subject, last_fired_at, fire_count, first_seen_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(kind, subject) DO UPDATE SET "
                "  last_fired_at = excluded.last_fired_at",
                (kind, subject, shifted_ts, now_ts),
            )
            conn.commit()
    except sqlite3.Error as _exc:
        log.debug("silenced exception in brain_loop.py: %s", _exc)


# ── Act: execute approved decisions ──────────────────────────────


def _write_doorbell(session_id: str, title: str, content: str, priority: str, source: str) -> bool:
    path = DOORBELL_DIR / f".brain_doorbell.{session_id}.jsonl"
    try:
        rec = {
            "ts": _now_iso(),
            "title": title,
            "content": content,
            "priority": priority,
            "source": source,
        }
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        return True
    except OSError:
        return False


def _write_eval_proposal(payload: dict) -> bool:
    """HR6 fix (2026-04-14): handle both the original recall_miss evidence
    shape AND the new L1-downgrade evidence shape from _decide. The
    downgraded-dispatch payload carries observation_kind + intended_payload
    so we can build a meaningful fingerprint + query row instead of
    collapsing every downgrade into one empty-fields duplicate.
    """
    try:
        import hashlib

        evidence = payload.get("evidence") or {}

        # Identify the payload shape
        obs_kind = evidence.get("observation_kind", "")
        obs_subject = evidence.get("observation_subject", "")
        intended_payload = evidence.get("intended_payload") or {}

        if obs_kind and obs_subject:
            # HR6 downgraded-dispatch shape
            fp_input = (
                f"brain_loop:{obs_kind}:{obs_subject}:"
                f"{intended_payload.get('agent','')}:"
                f"{(intended_payload.get('message','') or '')[:200]}"
            )
            query_text = f"{obs_kind}:{obs_subject}"
        else:
            # Original recall_miss shape
            fp_input = (
                f"brain_loop:{evidence.get('prev_prompt','')[:300]}:" f"{evidence.get('correction','')[:300]}"
            )
            query_text = (evidence.get("prev_prompt") or "")[:1000]

        fp = hashlib.sha256(fp_input.encode()).hexdigest()[:16]
        pid = f"bloop_{fp}"
        with _connect_autonomy() as conn:
            existing = conn.execute("SELECT id FROM eval_proposals WHERE id = ?", (pid,)).fetchone()
            if existing:
                return True
            conn.execute(
                "INSERT INTO eval_proposals "
                "(id, query, expected, expected_sources, source_event, status, "
                " confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    query_text,
                    json.dumps(evidence),
                    "[]",
                    f"brain_loop:{obs_kind or 'recall_miss'}",
                    "candidate",
                    payload.get("confidence", 0.7),
                    _now_iso(),
                ),
            )
            conn.commit()
        return True
    except sqlite3.Error as e:
        log.warning("eval_proposal write failed: %s", e)
        return False


def _dispatch_agent(agent: str, message: str) -> bool:
    if _openclaw_dispatch is None:
        return False
    try:
        result = _openclaw_dispatch(
            agent=agent,
            message=message,
            thinking="low",
            timeout=60,
            degraded_placeholder=f"[brain_loop → {agent} dispatch failed]",
        )
        return bool(getattr(result, "ok", False))
    except Exception as e:
        log.warning("openclaw_dispatch failed for %s: %s", agent, e)
        return False


def _telegram_alert(body: str) -> bool:
    """Delegate URGENT alerts to the unified telegram_alert module."""
    try:
        from telegram_alert import send_chris_telegram

        return send_chris_telegram(
            f"[brain_loop URGENT]\n{body}",
            source="brain_loop",
            severity="urgent",
        )
    except Exception as exc:
        log.warning("brain_loop telegram alert failed: %s", exc)
        return False


def _apply_self_modification(payload: dict) -> bool:
    """Apply a brain_loop self-modification. Most kinds write a proposal
    for Chris review; drain_llm_backlog is a special case that triggers an
    immediate catch-up drain when the llm.dispatch breaker just closed."""
    modification = payload.get("modification", "unknown")

    # Special case: event-driven llm_backlog drain. Directly invoke the
    # drain function instead of queueing a proposal — the whole point is
    # to catch up FAST when quota returns.
    if modification == "drain_llm_backlog":
        try:
            from llm_backlog import drain

            result = drain(limit=100, abort_on_breaker=True)
            log.info(
                "brain_loop drain_llm_backlog: drained=%d failed=%d abandoned=%d",
                result.get("drained", 0),
                result.get("failed", 0),
                result.get("abandoned", 0),
            )
            return True
        except Exception as e:
            log.warning("brain_loop drain_llm_backlog failed: %s", e)
            return False

    # Default: write a proposal for Chris review.
    try:
        with _connect_autonomy() as conn:
            pid = f"selfmod_{int(time.time())}"
            conn.execute(
                "INSERT INTO eval_proposals "
                "(id, query, expected, expected_sources, source_event, status, "
                " confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    f"self_modify:{modification}:{payload.get('domain', payload.get('subject', ''))}",
                    json.dumps(payload),
                    "[]",
                    "brain_loop_self_modify",
                    "candidate",
                    payload.get("confidence", 0.7),
                    _now_iso(),
                ),
            )
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def _act(decisions: list[Decision]) -> list[dict]:
    results: list[dict] = []
    for d in decisions:
        result: dict[str, Any] = {"status": "skipped"}
        try:
            if d.kind == DecisionKind.OBSERVE_ONLY:
                result = {"status": "observed"}
            elif d.kind == DecisionKind.PROPOSE:
                ok = _write_eval_proposal(d.action_payload)
                result = {"status": "proposed" if ok else "propose_failed"}
            elif d.kind == DecisionKind.DISPATCH_AGENT:
                ok = _dispatch_agent(
                    d.action_payload.get("agent", "jenna"),
                    d.action_payload.get("message", ""),
                )
                result = {"status": "dispatched" if ok else "dispatch_failed"}
            elif d.kind == DecisionKind.PUSH_TO_CLAUDE:
                ok = _write_doorbell(
                    d.action_payload.get("session_id", ""),
                    d.action_payload.get("title", ""),
                    d.action_payload.get("content", ""),
                    d.action_payload.get("priority", "medium"),
                    d.action_payload.get("source", "brain_loop"),
                )
                result = {"status": "doorbell_written" if ok else "doorbell_failed"}
            elif d.kind == DecisionKind.TELEGRAM_ALERT:
                ok = _telegram_alert(d.action_payload.get("body", ""))
                result = {"status": "telegram_sent" if ok else "telegram_failed"}
            elif d.kind == DecisionKind.SELF_MODIFY:
                ok = _apply_self_modification(d.action_payload)
                result = {"status": "self_mod_queued" if ok else "self_mod_failed"}

            # Audit write
            if _insert_action_audit is not None:
                try:
                    _insert_action_audit(
                        route=f"brain_loop/{d.kind.value}",
                        query_text=f"{d.observation.kind}:{d.observation.subject}"[:2000],
                        tool="brain_loop",
                        actor="brain_loop",
                        session_id=d.action_payload.get("session_id"),
                    )
                except Exception as _exc:
                    log.debug("silenced exception in brain_loop.py: %s", _exc)
        except Exception as e:
            result = {"status": "error", "error": str(e)[:200]}

        results.append({"decision": d.to_journal_dict(), "result": result})
    return results


# ── Journal ──────────────────────────────────────────────────────

_journal_lock = threading.Lock()


def _internal_monologue(
    observations: list[Observation],
    decisions: list[Decision],
    approved: list[Decision],
) -> str:
    if not observations:
        return "quiet tick, nothing noticed"
    parts = []
    for o in observations[:4]:
        parts.append(f"{o.kind}={o.subject[:40]}")
    noticed = ", ".join(parts)
    return f"noticed: {noticed} → decided {len(decisions)} → acted {len(approved)}"


def _journal(
    tick_n: int,
    observations: list[Observation],
    decisions: list[Decision],
    approved: list[Decision],
    results: list[dict],
    t0: float,
) -> None:
    entry = {
        "tick": tick_n,
        "ts": _now_iso(),
        "latency_ms": int((time.time() - t0) * 1000),
        "observations": [
            {"kind": o.kind, "subject": o.subject, "salience": o.salience} for o in observations
        ],
        "decisions_total": len(decisions),
        "approved": len(approved),
        "results": [{"kind": r["decision"]["kind"], "status": r["result"].get("status")} for r in results],
        "notes": _internal_monologue(observations, decisions, approved),
    }
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _journal_lock:
        try:
            with JOURNAL_PATH.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("journal write failed: %s", e)


# ── Main loop entry point ────────────────────────────────────────


class BrainLoop:
    """Singleton executive. One instance lives inside brain_server's event loop."""

    def __init__(self) -> None:
        self.tick_n = 0
        self._tick_lock = threading.Lock()
        self._running = False

    def tick(self) -> dict:
        """Run one iteration. Returns a summary dict for scheduler logging.

        Guaranteed to complete in <= TICK_BUDGET_S wall-clock even on partial
        failures. Best-effort: any sub-step that raises is logged and skipped.
        """
        # Env kill switch — respect BRAIN_AUTOPILOT_DISABLED globally.
        # 2026-04-16 fix: accept any standard truthy value, not just "1"
        # (see autonomy.authorize for the paired fix). Prevents silent
        # kill-switch misconfig from `BRAIN_AUTOPILOT_DISABLED=true`.
        if os.environ.get("BRAIN_AUTOPILOT_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
            return {"tick": self.tick_n, "status": "disabled_env"}

        # Reentrancy guard — skip if previous tick is still running
        if not self._tick_lock.acquire(blocking=False):
            return {"tick": self.tick_n, "status": "overlap_skipped"}

        t0 = time.time()
        try:
            self.tick_n += 1
            self._running = True

            # 1. PERCEIVE — run each sensor best-effort, respect budget
            raw_observations: list[Observation] = []
            for name, fn in SENSORS:
                if time.time() - t0 > TICK_BUDGET_S * 0.6:
                    log.warning("brain_loop tick %d budget exceeded in sensor %s", self.tick_n, name)
                    break
                try:
                    obs = fn() or []
                    raw_observations.extend(obs)
                except Exception as e:
                    log.warning("sensor %s failed: %s", name, e)

            # 1b. Filter out observations still within the persistent
            # (kind, subject) cooldown. This survives subprocess boundaries
            # via autonomy.db::brain_loop_seen. F4 fix (2026-04-14): we used
            # to MARK seen here too, but that meant any observation dropped
            # at the autonomy/rate-limit gate was silently lost without
            # retry. Marking now happens AFTER _decide so rejected decisions
            # re-evaluate on the next tick.
            observations = _filter_already_seen(raw_observations)

            # 2. REFLECT
            decisions = _reflect(observations)

            # 3. DECIDE
            approved = _decide(decisions)

            # 3b. Mark seen: observations that either produced no decision
            # (reflect deliberately skipped) or whose decision made it through
            # the gate. Anything rejected stays unmarked for retry.
            _mark_observations_fired(observations, decisions, approved)

            # 4. ACT
            results: list[dict] = []
            if time.time() - t0 < TICK_BUDGET_S * 0.95:
                results = _act(approved)
            else:
                log.warning("brain_loop tick %d budget exceeded pre-act, skipping actions", self.tick_n)

            # 4b. Fire speak.urgent_scan inside the tick so the two systems
            # share rate-limiting and budget. urgent_scan writes to the
            # per-session doorbell when severity >= 7.5; it's idempotent via
            # its own 6h dedup so running it every 60s is safe.
            if time.time() - t0 < TICK_BUDGET_S * 0.9:
                try:
                    from speak import urgent_scan as _urgent_scan

                    _urgent_scan()
                except Exception as exc:
                    log.debug("brain_loop urgent_scan failed: %s", exc)

            # 5. JOURNAL
            _journal(self.tick_n, observations, decisions, approved, results, t0)

            return {
                "tick": self.tick_n,
                "status": "ok",
                "observations": len(observations),
                "decisions": len(decisions),
                "approved": len(approved),
                "acted": len(results),
                "latency_ms": int((time.time() - t0) * 1000),
            }
        except Exception as e:
            log.exception("brain_loop.tick failed: %s", e)
            return {"tick": self.tick_n, "status": "error", "error": str(e)[:200]}
        finally:
            self._running = False
            self._tick_lock.release()

    # 2026-04-23 (NSprint #3): the wake-file watcher is back.
    # Sub-60s response is now valuable because attention.enqueue touches
    # /tmp/.brain_loop_wake on every new warning+ contradiction and
    # coding_events.upsert_outcome touches it on revert/reject. Without a
    # watcher those signals would sit up to 60s before the tick processes
    # them. The watcher calls tick() when mtime advances; tick's reentrancy
    # guard + rate limits + TICK_BUDGET_S bound the blast radius.
    pass


_WAKE_FILE = Path("/tmp/.brain_loop_wake")
_WAKE_POLL_INTERVAL_S = 2.0  # fallback only, used when kqueue unavailable
_WAKE_MIN_INTERVAL_S = 3.0  # debounce — no more than 1 wake-tick every 3s
_wake_last_tick_ts: float = 0.0


def _wake_debounced_tick(loop: BrainLoop) -> None:
    """Wake-triggered tick with a 3s debounce. Spawns the tick as a
    subprocess so it matches the isolation pattern of the scheduler's
    brain_loop_tick job (prevents server event loop contention). Debounce
    prevents a burst of enqueue+outcome touches from hammering brain_loop."""
    global _wake_last_tick_ts
    now = time.time()
    if now - _wake_last_tick_ts < _WAKE_MIN_INTERVAL_S:
        return
    _wake_last_tick_ts = now
    try:
        import subprocess

        subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, "
                    "'/Users/chrischo/server/brain/brain_core'); "
                    "from brain_loop import run; run()"
                ),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("wake-triggered tick spawn failed: %s", exc)


def _wake_watcher_loop() -> None:
    """Daemon thread: fire tick() when wake file is touched.

    On Darwin + Linux (kqueue / inotify-compatible select), reacts in
    ~10-50ms. On platforms without kqueue (Linux pure-Python), falls back
    to 2s mtime polling.
    """
    loop = get_brain_loop()
    import select

    if hasattr(select, "kqueue"):
        _wake_watcher_kqueue(loop)
    else:
        _wake_watcher_poll(loop)


def _wake_watcher_kqueue(loop: BrainLoop) -> None:
    """macOS path — kqueue watches the file for write/delete events. Near
    real-time reactivity with zero CPU when idle."""
    import select

    kq = select.kqueue()
    while True:
        try:
            if not _WAKE_FILE.exists():
                # Create the file so we have something to watch. Touch is idempotent.
                try:
                    _WAKE_FILE.touch()
                except OSError:
                    time.sleep(_WAKE_POLL_INTERVAL_S)
                    continue
            fd = os.open(str(_WAKE_FILE), os.O_RDONLY)
            try:
                kev = select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_ATTRIB | select.KQ_NOTE_DELETE,
                )
                # Blocking wait with 60s timeout so we loop around and
                # re-register if the file is rotated.
                events = kq.control([kev], 1, 60.0)
                if events:
                    _wake_debounced_tick(loop)
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)
        except OSError as exc:
            log.debug("kqueue watcher hiccup: %s", exc)
            time.sleep(_WAKE_POLL_INTERVAL_S)


def _wake_watcher_poll(loop: BrainLoop) -> None:
    """Fallback polling path for platforms without kqueue."""
    try:
        last_mtime = _WAKE_FILE.stat().st_mtime if _WAKE_FILE.exists() else 0.0
    except OSError:
        last_mtime = 0.0
    while True:
        time.sleep(_WAKE_POLL_INTERVAL_S)
        try:
            if not _WAKE_FILE.exists():
                continue
            m = _WAKE_FILE.stat().st_mtime
            if m <= last_mtime:
                continue
            last_mtime = m
            _wake_debounced_tick(loop)
        except OSError:
            continue


_wake_thread_started = False


def _ensure_wake_watcher() -> None:
    global _wake_thread_started
    if _wake_thread_started:
        return
    if os.environ.get("BRAIN_WAKE_WATCHER_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    t = threading.Thread(target=_wake_watcher_loop, daemon=True, name="brain_wake_watcher")
    t.start()
    _wake_thread_started = True


# Module-level singleton — scheduler reuses one instance across ticks.
_brain_loop: BrainLoop | None = None
_brain_loop_lock = threading.Lock()


def get_brain_loop() -> BrainLoop:
    """Thread-safe singleton. Relevant now that the wake-watcher daemon
    calls this from a background thread; the FastAPI startup path also
    touches it, so we need a lock around the check-and-assign."""
    global _brain_loop
    with _brain_loop_lock:
        if _brain_loop is None:
            _brain_loop = BrainLoop()
            _ensure_wake_watcher()
        return _brain_loop


def run() -> dict:
    """Scheduler entry point. Always returns a dict with tick metadata."""
    loop = get_brain_loop()
    return loop.tick()


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False))
