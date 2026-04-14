"""brain_core/breakers.py — persistent circuit breakers for autonomy gate.

Replaces the in-memory CB in openclaw_dispatch.py and unifies failure tracking
across all autonomous action kinds (heal.*, task.*, llm.*, reasoning.*, ...).

State lives in `autonomy.db/heal_breakers` so a brain restart doesn't reset
the breaker — critical for long cooldowns (4h) on repeated failures.

Backoff tiers grow each trip:
    [300s, 900s, 3600s, 14400s] = 5m → 15m → 1h → 4h

State machine:
    closed         — green, action allowed
    open           — red, action blocked until reset_after elapses
    half_open      — recovery probe, exactly ONE action allowed; success → closed,
                     failure → re-open with next backoff tier

Hot path: peek_breaker() uses an in-memory snapshot cache (5s TTL keyed by
kind) so the autonomy gate can stay <5ms p99 on the read path.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


CB_THRESHOLD = 3
BACKOFF_TIERS_S = (300, 900, 3600, 14400)  # 5m -> 15m -> 1h -> 4h
SNAPSHOT_TTL_S = 5.0


_init_lock = threading.Lock()
_initialized = False
_snapshot_cache: dict[str, tuple[float, BreakerSnapshot]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class BreakerSnapshot:
    kind: str
    state: str  # closed | open | half_open | half_open_probing
    failures: int
    trip_count: int
    opened_at: float | None
    last_failure_at: float | None
    last_action_at: float | None
    reset_after_s: int
    reason: str

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_half_open(self) -> bool:
        return self.state == "half_open"

    @property
    def is_probing(self) -> bool:
        """Another caller is currently running the single-flight probe."""
        return self.state == "half_open_probing"

    @property
    def blocks_new_callers(self) -> bool:
        """True if a new caller should be denied (open OR probe-in-flight)."""
        return self.state in ("open", "half_open_probing")

    @property
    def is_closed(self) -> bool:
        return self.state == "closed"

    @property
    def remaining_cooldown_s(self) -> float:
        if not self.is_open or self.opened_at is None:
            return 0.0
        elapsed = time.time() - self.opened_at
        return max(0.0, self.reset_after_s - elapsed)


def _ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS heal_breakers (
                  kind            TEXT PRIMARY KEY,
                  state           TEXT NOT NULL DEFAULT 'closed',
                  failures        INTEGER NOT NULL DEFAULT 0,
                  trip_count      INTEGER NOT NULL DEFAULT 0,
                  opened_at       REAL,
                  last_failure_at REAL,
                  last_action_at  REAL,
                  reset_after_s   INTEGER NOT NULL DEFAULT 300,
                  reason          TEXT DEFAULT ''
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def _connect() -> sqlite3.Connection:
    _ensure_schema()
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_snapshot(kind: str, row: sqlite3.Row | None) -> BreakerSnapshot:
    if row is None:
        return BreakerSnapshot(
            kind=kind,
            state="closed",
            failures=0,
            trip_count=0,
            opened_at=None,
            last_failure_at=None,
            last_action_at=None,
            reset_after_s=BACKOFF_TIERS_S[0],
            reason="",
        )
    return BreakerSnapshot(
        kind=row["kind"],
        state=row["state"],
        failures=row["failures"],
        trip_count=row["trip_count"],
        opened_at=row["opened_at"],
        last_failure_at=row["last_failure_at"],
        last_action_at=row["last_action_at"],
        reset_after_s=row["reset_after_s"],
        reason=row["reason"] or "",
    )


def peek_breaker(kind: str) -> BreakerSnapshot:
    """Read-mostly snapshot of a breaker. Auto-promotes open → half_open when
    the cooldown expires. Hot-path target <1 ms.
    """
    now = time.time()
    with _cache_lock:
        cached = _snapshot_cache.get(kind)
        if cached and (now - cached[0]) < SNAPSHOT_TTL_S:
            return cached[1]

    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM heal_breakers WHERE kind = ?", (kind,)).fetchone()
        snapshot = _row_to_snapshot(kind, row)

        # Auto half-open promotion
        if snapshot.is_open and snapshot.opened_at is not None:
            elapsed = now - snapshot.opened_at
            if elapsed >= snapshot.reset_after_s:
                conn.execute(
                    "UPDATE heal_breakers SET state = 'half_open' "
                    "WHERE kind = ? AND state = 'open' AND opened_at = ?",
                    (kind, snapshot.opened_at),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM heal_breakers WHERE kind = ?", (kind,)).fetchone()
                snapshot = _row_to_snapshot(kind, row)
    finally:
        conn.close()

    with _cache_lock:
        _snapshot_cache[kind] = (now, snapshot)
    return snapshot


def record_result(kind: str, *, ok: bool, error: str = "") -> BreakerSnapshot:
    """Record an action outcome and update the breaker state machine.

    Success → reset failures, close. Failure → bump counter; if at threshold,
    open with the next backoff tier. `half_open` + failure always re-opens
    with the next backoff tier (not the stale prior tier), fixing the
    half_open ↔ open zero-cooldown loop.

    Uses BEGIN IMMEDIATE to serialize concurrent writers on the same kind —
    the read-modify-write pattern is racy without an explicit write lock.

    Sqlite errors are swallowed and the prior cached snapshot is returned
    so the autonomy hot path never sees a raw exception from this layer.
    """
    try:
        conn = _connect()
    except sqlite3.Error:
        return _row_to_snapshot(kind, None)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            conn.close()
            return _row_to_snapshot(kind, None)
        row = conn.execute("SELECT * FROM heal_breakers WHERE kind = ?", (kind,)).fetchone()
        snapshot = _row_to_snapshot(kind, row)
        now = time.time()

        # State transitions
        if ok:
            # Success: full reset regardless of prior state (closed / half_open / open)
            new_state = "closed"
            new_failures = 0
            new_trip_count = 0
            new_opened_at = None
            new_reset_after = BACKOFF_TIERS_S[0]
            new_reason = ""
        elif snapshot.is_half_open:
            # Half-open probe failure: always escalate to next backoff tier.
            # This is the fix for the half_open ↔ open zero-cooldown loop —
            # previously the stale opened_at/reset_after_s were reused.
            new_state = "open"
            new_failures = snapshot.failures + 1
            new_trip_count = snapshot.trip_count + 1
            new_opened_at = now
            tier_idx = min(new_trip_count - 1, len(BACKOFF_TIERS_S) - 1)
            new_reset_after = BACKOFF_TIERS_S[tier_idx]
            new_reason = (error[:200] if error else snapshot.reason) or "half_open_probe_failed"
        else:
            new_failures = snapshot.failures + 1
            if new_failures >= CB_THRESHOLD:
                # Threshold crossed: open with next tier
                new_state = "open"
                new_trip_count = snapshot.trip_count + 1
                new_opened_at = now
                tier_idx = min(new_trip_count - 1, len(BACKOFF_TIERS_S) - 1)
                new_reset_after = BACKOFF_TIERS_S[tier_idx]
            else:
                # Still closed, just bump failure count
                new_state = snapshot.state
                new_trip_count = snapshot.trip_count
                new_opened_at = snapshot.opened_at
                new_reset_after = snapshot.reset_after_s
            new_reason = error[:200] if error else snapshot.reason

        try:
            conn.execute(
                "INSERT INTO heal_breakers (kind, state, failures, trip_count, opened_at, "
                " last_failure_at, last_action_at, reset_after_s, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET "
                " state=excluded.state, failures=excluded.failures, "
                " trip_count=excluded.trip_count, opened_at=excluded.opened_at, "
                " last_failure_at=excluded.last_failure_at, "
                " last_action_at=excluded.last_action_at, "
                " reset_after_s=excluded.reset_after_s, reason=excluded.reason",
                (
                    kind,
                    new_state,
                    new_failures,
                    new_trip_count,
                    new_opened_at,
                    now if not ok else snapshot.last_failure_at,
                    now,
                    new_reset_after,
                    new_reason,
                ),
            )
            conn.commit()

            row = conn.execute("SELECT * FROM heal_breakers WHERE kind = ?", (kind,)).fetchone()
            result = _row_to_snapshot(kind, row)
        except sqlite3.Error:
            result = snapshot
    finally:
        conn.close()

    with _cache_lock:
        _snapshot_cache[kind] = (time.time(), result)
    return result


def try_claim_probe(kind: str) -> bool:
    """Atomic single-flight for half_open probes.

    Returns True if this caller wins the exclusive right to run the next
    action against a half_open breaker; False if another caller already
    claimed it or the breaker isn't half_open. Wins are marked by
    transitioning state from 'half_open' → 'half_open_probing' with a
    compare-and-swap UPDATE. `record_result()` will then clear it back
    to 'closed' (success) or 'open' (failure).

    This fixes the bypass where every concurrent caller saw half_open
    via the 5s cache and all passed the gate.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE heal_breakers SET state='half_open_probing', "
            "last_action_at=? "
            "WHERE kind=? AND state='half_open'",
            (time.time(), kind),
        )
        conn.commit()
        claimed = cur.rowcount > 0
    finally:
        conn.close()
    if claimed:
        invalidate_cache(kind)
    return claimed


def reset(kind: str) -> BreakerSnapshot:
    """Manually clear a breaker (admin / Brain UI / Chris). Returns new snapshot."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE heal_breakers SET state='closed', failures=0, trip_count=0, "
            "opened_at=NULL, reset_after_s=?, reason='manual_reset' WHERE kind=?",
            (BACKOFF_TIERS_S[0], kind),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM heal_breakers WHERE kind = ?", (kind,)).fetchone()
        snapshot = _row_to_snapshot(kind, row)
    finally:
        conn.close()
    with _cache_lock:
        _snapshot_cache[kind] = (time.time(), snapshot)
    return snapshot


def list_all() -> list[BreakerSnapshot]:
    """Return all known breakers (closed + open + half_open)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM heal_breakers ORDER BY kind").fetchall()
    finally:
        conn.close()
    return [_row_to_snapshot(r["kind"], r) for r in rows]


def invalidate_cache(kind: str | None = None) -> None:
    """Drop the in-memory snapshot cache for a kind (or all kinds)."""
    with _cache_lock:
        if kind is None:
            _snapshot_cache.clear()
        else:
            _snapshot_cache.pop(kind, None)
