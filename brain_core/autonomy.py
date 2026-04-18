"""brain_core/autonomy.py - L0-L3 autonomy gate.

Single chokepoint for "is this autonomous action allowed right now?"
Called from every site that takes a non-read action: task_queue, action_triggers,
self_heal, reasoning_loop, goal_decompose, slo_monitor, openclaw_dispatch.

Hot-path target: <5 ms p99 on warm cache. Implementation:
    - 15 s LRU on level lookups from brain_config
    - 5 s snapshot cache on breaker state (in breakers.py)
    - precomputed quiet-hours / work-hours window check (datetime arithmetic only)
    - NO synchronous LLM, Neo4j, or Chroma calls
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import brain_config_store
    from breakers import peek_breaker, try_claim_probe
    from config import AUTONOMY_DB
    from default_levels import (
        DEFAULT_LEVELS,
        DENY_PREFIXES,
        EXECUTION_WINDOWS,
        QUIET_HOURS,
        notify_lag_for,
    )
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    DEFAULT_LEVELS = {}
    DENY_PREFIXES = ()
    EXECUTION_WINDOWS = {}
    QUIET_HOURS = {"start": "23:00", "end": "07:00", "tz": "America/Los_Angeles", "exceptions": []}
    brain_config_store = None  # type: ignore[assignment]

    def notify_lag_for(_kind: str) -> int:
        return 30

    def peek_breaker(_kind: str) -> None:
        return None

    def try_claim_probe(_kind: str) -> bool:
        return False


_LEVEL_CACHE_TTL_S = 15.0
_level_cache_stamp = 0.0
_level_cache: dict[str, str] = {}
_soft_deny_cache_stamp = 0.0
_soft_deny_cache: tuple[str, ...] = ()
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    level: str  # L0 | L1 | L2 | L3
    requires_ack: bool  # L1 → True (insert pending_approval task)
    notify_lag_s: int  # L2 → > 0 (Telegram alert + delay)
    reason: str
    breaker_state: str  # closed | open | half_open | n/a
    kind: str

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "level": self.level,
            "requires_ack": self.requires_ack,
            "notify_lag_s": self.notify_lag_s,
            "reason": self.reason,
            "breaker_state": self.breaker_state,
            "kind": self.kind,
        }


def _ensure_brain_config_schema() -> None:
    """Thin shim — brain_config_store owns the DDL now. Kept for back-compat."""
    if brain_config_store is not None:
        brain_config_store.ensure_schema()


def _load_level_overrides() -> dict[str, str]:
    """Pull autonomy.<kind>.level rows from brain_config and merge over defaults.

    Returns an empty dict on any sqlite error so callers fall through to
    DEFAULT_LEVELS instead of crashing the autonomy gate. The gate is on the
    hot path of every action; a single brain_config lock contention event
    used to crash every authorize() call.
    """
    if brain_config_store is None:
        return {}
    try:
        rows = brain_config_store.get_prefix("autonomy.")
    except sqlite3.Error:
        return {}
    overrides: dict[str, str] = {}
    for key, value in rows.items():
        if not key.endswith(".level"):
            continue
        if value not in ("L0", "L1", "L2", "L3"):
            continue
        kind = key[len("autonomy.") : -len(".level")]
        overrides[kind] = value
    return overrides


def _resolve_level_cached() -> dict[str, str]:
    global _level_cache_stamp, _level_cache
    now = time.monotonic()
    with _cache_lock:
        if _level_cache and (now - _level_cache_stamp) < _LEVEL_CACHE_TTL_S:
            return _level_cache
        merged = dict(DEFAULT_LEVELS)
        # Gate hot path must never crash on a transient brain_config error.
        with contextlib.suppress(sqlite3.Error):
            merged.update(_load_level_overrides())
        _level_cache = merged
        _level_cache_stamp = now
        return merged


def invalidate_levels_cache() -> None:
    global _level_cache_stamp, _soft_deny_cache_stamp
    with _cache_lock:
        _level_cache_stamp = 0.0
        _soft_deny_cache_stamp = 0.0


def _load_soft_denylist() -> tuple[str, ...]:
    """Pull denylist.<prefix>=1 rows from brain_config.

    Returns an empty tuple on any sqlite error so the gate falls through
    instead of crashing.
    """
    if brain_config_store is None:
        return ()
    try:
        rows = brain_config_store.get_prefix("denylist.")
    except sqlite3.Error:
        return ()
    prefixes: list[str] = []
    for key, value in rows.items():
        if value == "1":
            prefix = key[len("denylist.") :]
            if prefix:
                prefixes.append(prefix)
    return tuple(prefixes)


def _resolve_soft_denylist_cached() -> tuple[str, ...]:
    global _soft_deny_cache_stamp, _soft_deny_cache
    now = time.monotonic()
    with _cache_lock:
        if _soft_deny_cache and (now - _soft_deny_cache_stamp) < _LEVEL_CACHE_TTL_S:
            return _soft_deny_cache
        try:
            _soft_deny_cache = _load_soft_denylist()
        except sqlite3.Error:
            _soft_deny_cache = ()
        _soft_deny_cache_stamp = now
        return _soft_deny_cache


def set_level(kind: str, level: str, *, updated_by: str = "system") -> None:
    """Persist a level override to brain_config and invalidate the cache."""
    if level not in ("L0", "L1", "L2", "L3"):
        raise ValueError(f"invalid level: {level}")
    if brain_config_store is None:
        return
    brain_config_store.set(f"autonomy.{kind}.level", level, updated_by=updated_by)
    invalidate_levels_cache()


def list_levels() -> dict[str, str]:
    return dict(_resolve_level_cached())


def _autopilot_enabled() -> bool:
    """Top-level kill switch — preserved from autopilot.py JSON state file.

    2026-04-16 fix: fails CLOSED (returns False) when the autopilot module
    cannot be imported or raises. Previously fail-open — a broken import
    silently enabled every autonomous action, which is exactly the wrong
    failure mode for a kill switch. An autonomous brain that defaults ON
    under module corruption is unsafe; the correct default is OFF and
    require Chris to explicitly re-enable after investigating.
    """
    try:
        from autopilot import is_enabled

        return bool(is_enabled())
    except Exception:
        return False


def _in_quiet_hours(now_local: datetime) -> bool:
    start = dtime.fromisoformat(QUIET_HOURS["start"])  # type: ignore[arg-type]
    end = dtime.fromisoformat(QUIET_HOURS["end"])  # type: ignore[arg-type]
    t = now_local.time()
    if start > end:  # wraps midnight
        return t >= start or t < end
    return start <= t < end


def _demote(level: str) -> str:
    return {"L3": "L2", "L2": "L1", "L1": "L1", "L0": "L0"}[level]


def _execution_window_check(kind: str, now_local: datetime) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    windows = EXECUTION_WINDOWS.get(kind, ["any"])
    if "any" in windows:
        return True, ""
    hour = now_local.hour
    in_night = hour >= 23 or hour < 7
    in_work_block = 9 <= hour < 18  # CLAUDE.md: no heavy ops 9-18 PT
    if "night" in windows:
        if not in_night:
            return False, "execution_window_block:night_only"
        if in_work_block:
            return False, "execution_window_block:work_hours"
    if "day" in windows and in_night:
        return False, "execution_window_block:day_only"
    return True, ""


# ── Audit logging (2026-04-17) ─────────────────────────────
# Every authorize() decision is recorded so we can post-mortem "why was
# this denied?" / "why did the gate let this through?" weeks later.
# Schema is lazy — creates on first write so older brain_core/ imports
# still work.
import sqlite3 as _sqlite3_audit

_audit_schema_ready = False


def _ensure_audit_schema() -> None:
    global _audit_schema_ready
    if _audit_schema_ready:
        return
    try:
        conn = _sqlite3_audit.connect(str(AUTONOMY_DB), timeout=5)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS autonomy_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    level TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    breaker_state TEXT NOT NULL,
                    context_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autonomy_decisions_ts "
                "ON autonomy_decisions(ts_utc DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autonomy_decisions_kind "
                "ON autonomy_decisions(kind, ts_utc DESC)"
            )
            conn.commit()
        finally:
            conn.close()
        _audit_schema_ready = True
    except Exception as exc:
        log.debug("autonomy_decisions schema init failed: %s", exc)


def _record_decision(decision: AuthorizationDecision, context: dict | None) -> None:
    """Best-effort insert into autonomy_decisions. Never raises."""
    try:
        _ensure_audit_schema()
        import json as _json

        ts = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
        ctx_json = _json.dumps(context, default=str)[:1000] if context else None
        conn = _sqlite3_audit.connect(str(AUTONOMY_DB), timeout=2)
        try:
            conn.execute(
                "INSERT INTO autonomy_decisions "
                "(ts_utc, kind, level, allowed, reason, breaker_state, context_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    decision.kind,
                    decision.level,
                    1 if decision.allowed else 0,
                    decision.reason,
                    decision.breaker_state,
                    ctx_json,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("autonomy decision audit write failed: %s", exc)


def _authorize_core(
    kind: str,
    *,
    context: dict | None = None,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """The gate. Resolve whether a kind is allowed to act right now.

    Evaluation order (matches plan):
      a. BRAIN_AUTOPILOT_DISABLED env → L0
      b. autopilot.is_enabled() False → L0
      c. DENY_PREFIXES match → L0
      d. breaker open → L0 (half_open allows ONE probe)
      e. brain_config + DEFAULT_LEVELS lookup
      f. quiet hours demotion
      g. EXECUTION_WINDOWS hard block
      h. level → decision
    """
    now = now or datetime.now(ZoneInfo("UTC"))
    breaker_state = "n/a"

    # (a) hard env kill — 2026-04-16 fix: now accepts any truthy value
    # ("1"/"true"/"yes"/"on"), matching the rest of the config-flag idiom
    # in config.py. Previously only "1" disabled; setting the intuitive
    # BRAIN_AUTOPILOT_DISABLED=true had no effect — worst-case silent
    # misconfiguration of the most safety-critical flag in the system.
    if os.environ.get("BRAIN_AUTOPILOT_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return AuthorizationDecision(
            allowed=False,
            level="L0",
            requires_ack=False,
            notify_lag_s=0,
            reason="env_kill",
            breaker_state=breaker_state,
            kind=kind,
        )

    # (b) global autopilot kill switch (existing JSON file)
    if not _autopilot_enabled():
        return AuthorizationDecision(
            allowed=False,
            level="L0",
            requires_ack=False,
            notify_lag_s=0,
            reason="autopilot_off",
            breaker_state=breaker_state,
            kind=kind,
        )

    # (c) hardcoded deny list (security floor — not overridable)
    if any(kind.startswith(p) for p in DENY_PREFIXES):
        return AuthorizationDecision(
            allowed=False,
            level="L0",
            requires_ack=False,
            notify_lag_s=0,
            reason="denylist",
            breaker_state=breaker_state,
            kind=kind,
        )

    # (c.2) soft deny list from brain_config (operator-managed via /brain/denylist/add)
    soft_denies = _resolve_soft_denylist_cached()
    if soft_denies and any(kind.startswith(p) for p in soft_denies):
        return AuthorizationDecision(
            allowed=False,
            level="L0",
            requires_ack=False,
            notify_lag_s=0,
            reason="soft_denylist",
            breaker_state=breaker_state,
            kind=kind,
        )

    # (d) persistent breaker
    snapshot = peek_breaker(kind)
    if snapshot is not None:
        breaker_state = snapshot.state
        if snapshot.is_open:
            return AuthorizationDecision(
                allowed=False,
                level="L0",
                requires_ack=False,
                notify_lag_s=0,
                reason="breaker_open",
                breaker_state=breaker_state,
                kind=kind,
            )
        if snapshot.is_probing:
            # Another caller already claimed the single-flight probe.
            return AuthorizationDecision(
                allowed=False,
                level="L0",
                requires_ack=False,
                notify_lag_s=0,
                reason="breaker_probe_in_flight",
                breaker_state=breaker_state,
                kind=kind,
            )
        if snapshot.is_half_open:
            # Half-open: exactly ONE caller wins the atomic claim.
            # Losers see probe_in_flight on the next peek.
            if not try_claim_probe(kind):
                return AuthorizationDecision(
                    allowed=False,
                    level="L0",
                    requires_ack=False,
                    notify_lag_s=0,
                    reason="breaker_probe_lost",
                    breaker_state="half_open_probing",
                    kind=kind,
                )
            breaker_state = "half_open_probing"

    # (e) level lookup with brain_config override
    levels = _resolve_level_cached()
    # Family lookup: trigger.fire.health_check_failed → falls back to trigger.fire
    level = levels.get(kind)
    if level is None:
        # Try parent family (drop trailing dotted suffix)
        family_parts = kind.split(".")
        for i in range(len(family_parts) - 1, 0, -1):
            family = ".".join(family_parts[:i])
            if family in levels:
                level = levels[family]
                break
        if level is None:
            level = "L1"  # safe default for unknown kinds

    # (f) quiet hours demotion
    now_local = now.astimezone(ZoneInfo(QUIET_HOURS["tz"]))  # type: ignore[arg-type]
    quiet_exceptions = QUIET_HOURS.get("exceptions") or []
    if _in_quiet_hours(now_local) and kind not in quiet_exceptions:
        level = _demote(level)

    # (g) execution window hard block
    win_ok, win_reason = _execution_window_check(kind, now_local)
    if not win_ok:
        return AuthorizationDecision(
            allowed=False,
            level="L0",
            requires_ack=False,
            notify_lag_s=0,
            reason=win_reason,
            breaker_state=breaker_state,
            kind=kind,
        )

    # (h) level → decision shape
    if level == "L0":
        return AuthorizationDecision(
            allowed=False,
            level="L0",
            requires_ack=False,
            notify_lag_s=0,
            reason="level_L0",
            breaker_state=breaker_state,
            kind=kind,
        )
    if level == "L1":
        return AuthorizationDecision(
            allowed=True,
            level="L1",
            requires_ack=True,
            notify_lag_s=0,
            reason="propose_only",
            breaker_state=breaker_state,
            kind=kind,
        )
    if level == "L2":
        return AuthorizationDecision(
            allowed=True,
            level="L2",
            requires_ack=False,
            notify_lag_s=notify_lag_for(kind),
            reason="notify_then_act",
            breaker_state=breaker_state,
            kind=kind,
        )
    return AuthorizationDecision(
        allowed=True,
        level="L3",
        requires_ack=False,
        notify_lag_s=0,
        reason="immediate",
        breaker_state=breaker_state,
        kind=kind,
    )


def authorize(
    kind: str,
    *,
    context: dict | None = None,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Public entrypoint: run the gate and record the decision.

    Keeps the _authorize_core logic pure / side-effect-free for testing,
    and layers persistent audit logging on top. Every gate decision is
    written to autonomy_decisions for post-mortem. Write is best-effort;
    DB issues never block the decision itself."""
    decision = _authorize_core(kind, context=context, now=now)
    _record_decision(decision, context)
    return decision
