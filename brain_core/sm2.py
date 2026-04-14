"""brain_core/sm2.py — SuperMemo-2 spaced repetition scheduling for atoms.

Implements the classic SM-2 algorithm (Wozniak 1990) over the atoms table.
Quality grades 0..5 → updated easiness factor + interval days + next review.

Quality mapping from existing reinforce_memory(success: bool):
    success=True  → quality 4
    success=False → quality 1

Promotion rules layered ON TOP of SM-2 (incident 2026-04-13 design):
    episodic → semantic : reinforcement_count ≥ 2 AND interval_days ≥ 6
    semantic → core     : reinforcement_count ≥ 5 AND interval_days ≥ 30 AND canonical = 0
    → obsolete          : next_review_at < now - 180d AND reinforcement_count = 0

Module is import-safe even when atoms_store isn't enabled — every public
function returns None / no-op.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from atoms_store import _conn, get_atom_by_chroma_id
    from config import BRAIN_ATOMS_ENABLED, BRAIN_DB
except ImportError:
    BRAIN_ATOMS_ENABLED = False
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    _conn = None
    get_atom_by_chroma_id = None


_MIN_EF = 1.3


def _now() -> datetime:
    return datetime.now(UTC)


def schedule(
    *,
    easiness_factor: float,
    reinforcement_count: int,
    interval_days: float,
    quality: int,
    now: datetime | None = None,
) -> dict:
    """Pure SM-2 step. Returns the next-state dict.

    Caller is responsible for persisting.
    """
    now = now or _now()
    quality = max(0, min(5, int(quality)))
    ef = max(_MIN_EF, easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))

    if quality < 3:
        new_count = 0
        new_interval = 1.0
    else:
        if reinforcement_count == 0:
            new_interval = 1.0
        elif reinforcement_count == 1:
            new_interval = 6.0
        else:
            new_interval = round(interval_days * ef, 2)
        new_count = reinforcement_count + 1

    return {
        "easiness_factor": round(ef, 3),
        "interval_days": new_interval,
        "reinforcement_count": new_count,
        "last_reviewed_at": now.isoformat(timespec="seconds"),
        "next_review_at": (now + timedelta(days=new_interval)).isoformat(timespec="seconds"),
    }


def _promote_tier(atom: dict) -> str | None:
    """Decide tier promotion based on SM-2 state. Returns new tier or None."""
    tier = atom.get("tier") or "episodic"
    rc = atom.get("reinforcement_count") or 0
    interval = atom.get("interval_days") or 0
    canonical = bool(atom.get("canonical"))
    if tier == "episodic" and rc >= 2 and interval >= 6:
        return "semantic"
    if tier == "semantic" and rc >= 5 and interval >= 30 and not canonical:
        return "core"
    return None


def apply_quality(chroma_id: str, quality: int, *, db_path: Path | None = None) -> dict | None:
    """Run SM-2 + tier promotion against an atom and persist. Best-effort.

    Uses BEGIN IMMEDIATE to serialize concurrent reviews on the same atom.
    Without it, two simultaneous quality grades would both read the same
    state and the later commit would silently lose the earlier update.

    Returns the updated atom dict or None if disabled / not found.
    """
    if not BRAIN_ATOMS_ENABLED or _conn is None:
        return None
    try:
        with _conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, easiness_factor, interval_days, reinforcement_count, "
                "tier, canonical FROM atoms WHERE chroma_id = ?",
                (chroma_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            state = schedule(
                easiness_factor=row["easiness_factor"],
                reinforcement_count=row["reinforcement_count"],
                interval_days=row["interval_days"],
                quality=quality,
            )
            new_tier = _promote_tier(
                {
                    "tier": row["tier"],
                    "reinforcement_count": state["reinforcement_count"],
                    "interval_days": state["interval_days"],
                    "canonical": row["canonical"],
                }
            )
            now_iso = _now().isoformat(timespec="seconds")
            if new_tier:
                conn.execute(
                    "UPDATE atoms SET easiness_factor=?, interval_days=?, "
                    "reinforcement_count=?, last_reviewed_at=?, next_review_at=?, "
                    "tier=?, updated_at=? WHERE id=?",
                    (
                        state["easiness_factor"],
                        state["interval_days"],
                        state["reinforcement_count"],
                        state["last_reviewed_at"],
                        state["next_review_at"],
                        new_tier,
                        now_iso,
                        row["id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE atoms SET easiness_factor=?, interval_days=?, "
                    "reinforcement_count=?, last_reviewed_at=?, next_review_at=?, "
                    "updated_at=? WHERE id=?",
                    (
                        state["easiness_factor"],
                        state["interval_days"],
                        state["reinforcement_count"],
                        state["last_reviewed_at"],
                        state["next_review_at"],
                        now_iso,
                        row["id"],
                    ),
                )
            conn.commit()
            return {
                "atom_id": row["id"],
                "tier": new_tier or row["tier"],
                "promoted": bool(new_tier),
                **state,
            }
    except sqlite3.Error:
        return None


def review_due(*, limit: int = 20, tier: str | None = None, db_path: Path | None = None) -> list[dict]:
    """Return atoms whose next_review_at is in the past (or null) and need a review."""
    if not BRAIN_ATOMS_ENABLED or _conn is None:
        return []
    now_iso = _now().isoformat(timespec="seconds")
    sql = (
        "SELECT id, chroma_id, text, kind, tier, reinforcement_count, "
        "interval_days, easiness_factor, next_review_at "
        "FROM atoms WHERE tier != 'obsolete' AND next_review_at IS NOT NULL "
        "AND next_review_at <= ? "
    )
    params: list[object] = [now_iso]
    if tier:
        sql += "AND tier = ? "
        params.append(tier)
    sql += "ORDER BY next_review_at ASC LIMIT ?"
    params.append(limit)
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def consolidate_obsolete(*, days: int = 180, db_path: Path | None = None) -> dict:
    """Mark atoms obsolete if next_review_at < now - days AND reinforcement_count = 0."""
    if not BRAIN_ATOMS_ENABLED or _conn is None:
        return {"obsoleted": 0}
    cutoff = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "UPDATE atoms SET tier='obsolete', updated_at=? "
                "WHERE tier != 'obsolete' AND tier != 'core' "
                "AND reinforcement_count = 0 "
                "AND (next_review_at IS NULL OR next_review_at < ?)",
                (_now().isoformat(timespec="seconds"), cutoff),
            )
            conn.commit()
            return {"obsoleted": cur.rowcount}
    except sqlite3.Error:
        return {"obsoleted": 0, "error": "sqlite_error"}


def nightly_pass(*, db_path: Path | None = None) -> dict:
    """Nightly job: walk atoms with NULL next_review_at and seed them, then
    consolidate obsolete entries. Idempotent.
    """
    if not BRAIN_ATOMS_ENABLED or _conn is None:
        return {"seeded": 0, "obsoleted": 0}
    seeded = 0
    try:
        with _conn(db_path) as conn:
            now_iso = _now().isoformat(timespec="seconds")
            tomorrow = (_now() + timedelta(days=1)).isoformat(timespec="seconds")
            cur = conn.execute(
                "UPDATE atoms SET next_review_at = ?, last_reviewed_at = COALESCE(last_reviewed_at, ?), "
                "updated_at = ? "
                "WHERE next_review_at IS NULL AND tier != 'obsolete'",
                (tomorrow, now_iso, now_iso),
            )
            seeded = cur.rowcount
            conn.commit()
    except sqlite3.Error:
        pass
    obsoleted = consolidate_obsolete(db_path=db_path).get("obsoleted", 0)
    return {"seeded": seeded, "obsoleted": obsoleted}
