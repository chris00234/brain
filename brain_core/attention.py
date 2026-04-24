"""brain_core/attention.py — attention priority queue (biological: thalamus).

2026-04-17: Chris Priority #3 (personal agent capability).

Biological brain's thalamus gates which signals reach conscious attention RIGHT
NOW. Unimportant signals are filtered, novel ones are amplified, and repeated
exposure leads to habituation (filter stronger each time).

Brain's existing proactive.py emits N insights per 6h cycle with severity tags
(info/warning/urgent) but no priority ordering and no habituation — the same
insight can fire week after week, drowning signal in noise.

This module adds:
  1. Priority scoring: urgency x novelty x valence-proximity
  2. Habituation: shown_count decays the score (3x shown → half weight)
  3. Top-1 surfacing: `/brain/attention` returns THE thing to look at now,
     not a dump of every pending insight
  4. Acknowledgment: marking "seen" bumps shown_count

Storage: `attention_queue` table in brain.db. Populated by proactive.py (later
wire-in); read by the /brain/attention endpoint.

Scoring formula:
  priority = severity_weight * novelty * valence_factor
  where:
    severity_weight = {info:1, warning:2, urgent:4}
    novelty = 1 / (1 + shown_count/3)              # habituation (3x ≈ half)
    valence_factor = 1 + 0.3*(positive valence on referenced atoms, clamped ±0.3)
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.attention")

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

SEVERITY_WEIGHT = {"info": 1.0, "warning": 2.0, "urgent": 4.0, "critical": 4.0}
HABITUATION_HALF_LIFE = 3  # shows before weight halves

# 2026-04-17 prod-review fix: schema idempotency flag to avoid repeat
# open+CREATE+close on every public call (hot for top_attention in boot_context).
_schema_done = False


def _ensure_schema() -> None:
    global _schema_done
    if _schema_done:
        return
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS attention_queue (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                summary TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                related_atoms_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                shown_count INTEGER NOT NULL DEFAULT 0,
                last_shown_at TEXT,
                dismissed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_attention_active ON attention_queue(dismissed, expires_at);
            """
        )
        conn.commit()
        _schema_done = True
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def enqueue(
    insight_id: str,
    category: str,
    severity: str,
    summary: str,
    detail: str = "",
    related_atoms: list[str] | None = None,
    ttl_hours: int = 48,
) -> dict:
    """Add or update an insight in the attention queue. Idempotent — same id re-adds."""
    _ensure_schema()
    expires_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() + ttl_hours * 3600, UTC).isoformat(
        timespec="seconds"
    )
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.execute(
            """INSERT INTO attention_queue
               (id, category, severity, summary, detail, related_atoms_json,
                created_at, expires_at, shown_count, dismissed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
               ON CONFLICT(id) DO UPDATE SET
                 category = excluded.category,
                 severity = excluded.severity,
                 summary = excluded.summary,
                 detail = excluded.detail,
                 related_atoms_json = excluded.related_atoms_json,
                 expires_at = excluded.expires_at""",
            (
                insight_id,
                category,
                severity,
                summary[:500],
                detail[:2000],
                json.dumps(related_atoms or []),
                _now_iso(),
                expires_at,
            ),
        )
        conn.commit()
        # Signal-driven brain_loop wake: touch the file watcher so brain_loop
        # reacts to new warning+ attention items within ~1s instead of waiting
        # for the next 60s tick. Fire-and-forget; never fails the enqueue.
        # Includes "urgent" — defined in SEVERITY_WEIGHT but was omitted from
        # the wake trigger in the initial wire-up.
        if severity in ("warning", "urgent", "critical"):
            with contextlib.suppress(OSError):
                Path("/tmp/.brain_loop_wake").touch()
        return {"ok": True, "id": insight_id}
    finally:
        conn.close()


def _priority_score(row: dict, valence_map: dict[str, float]) -> float:
    sev = SEVERITY_WEIGHT.get(str(row.get("severity", "info")).lower(), 1.0)
    shown = int(row.get("shown_count") or 0)
    novelty = 1.0 / (1.0 + shown / HABITUATION_HALF_LIFE)
    # Valence factor: average valence of related atoms, clamped
    related = json.loads(row.get("related_atoms_json") or "[]")
    if related:
        vals = [valence_map.get(a, 0.0) for a in related]
        avg_v = sum(vals) / max(1, len(vals))
    else:
        avg_v = 0.0
    valence_factor = max(0.7, min(1.3, 1.0 + 0.3 * avg_v))
    return sev * novelty * valence_factor


def top_attention(limit: int = 1) -> list[dict]:
    """Return top-N items by priority. Default 1 — the single most-worth-attention item."""
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        rows = conn.execute(
            """SELECT id, category, severity, summary, detail, related_atoms_json,
                      created_at, expires_at, shown_count, last_shown_at
               FROM attention_queue
               WHERE dismissed = 0 AND datetime('now', 'utc') < expires_at"""
        ).fetchall()
        items = []
        all_atom_ids = set()
        for r in rows:
            d = {
                "id": r[0],
                "category": r[1],
                "severity": r[2],
                "summary": r[3],
                "detail": r[4],
                "related_atoms_json": r[5],
                "created_at": r[6],
                "expires_at": r[7],
                "shown_count": r[8],
                "last_shown_at": r[9],
            }
            try:
                all_atom_ids.update(json.loads(d["related_atoms_json"] or "[]"))
            except Exception as _exc:
                log.debug("silenced exception in attention.py: %s", _exc)
            items.append(d)
        valence_map = {}
        if all_atom_ids:
            try:
                from valence import get_valence_batch

                valence_map = get_valence_batch(list(all_atom_ids))
            except Exception:
                valence_map = {}
        scored = []
        for item in items:
            item["priority"] = round(_priority_score(item, valence_map), 4)
            scored.append(item)
        scored.sort(key=lambda x: x["priority"], reverse=True)
        return scored[:limit]
    finally:
        conn.close()


def mark_shown(insight_id: str) -> dict:
    """Called when Chris has seen the insight. Bumps shown_count (habituation)."""
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.execute(
            "UPDATE attention_queue SET shown_count = shown_count + 1, last_shown_at = ? " "WHERE id = ?",
            (_now_iso(), insight_id),
        )
        conn.commit()
        return {"ok": True, "id": insight_id}
    finally:
        conn.close()


def dismiss(insight_id: str) -> dict:
    """Chris explicitly dismisses this insight (not relevant)."""
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.execute("UPDATE attention_queue SET dismissed = 1 WHERE id = ?", (insight_id,))
        conn.commit()
        return {"ok": True, "id": insight_id}
    finally:
        conn.close()


def queue_stats() -> dict:
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        total = conn.execute("SELECT count(*) FROM attention_queue").fetchone()[0]
        active = conn.execute(
            "SELECT count(*) FROM attention_queue WHERE dismissed = 0 AND datetime('now', 'utc') < expires_at"
        ).fetchone()[0]
        dismissed = conn.execute("SELECT count(*) FROM attention_queue WHERE dismissed = 1").fetchone()[0]
        return {"total": total, "active": active, "dismissed": dismissed}
    finally:
        conn.close()


def prune_expired() -> int:
    """Remove insights past their expires_at. Returns count pruned."""
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        cur = conn.execute("DELETE FROM attention_queue WHERE datetime('now', 'utc') >= expires_at")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")

    p_top = sub.add_parser("top")
    p_top.add_argument("--limit", type=int, default=1)

    sub.add_parser("stats")

    p_seed = sub.add_parser("seed")

    args = p.parse_args()
    if args.cmd == "top":
        print(json.dumps(top_attention(args.limit), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(queue_stats(), indent=2))
    elif args.cmd == "seed":
        enqueue("test_insight_1", "pattern", "warning", "Test insight", "detail", ttl_hours=1)
        print(json.dumps(queue_stats(), indent=2))
    else:
        p.print_help()
