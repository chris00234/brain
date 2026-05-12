"""brain_core/coding_events.py — outcome tracking for Claude Code edits.

Goal: close the feedback loop that coding-domain recall accuracy needs. The
PostToolUse hook captures every Edit/Write intent into raw_events, but without
outcome (accepted? reverted? superseded?) brain can't learn which edits
represented real signal vs. thrash.

This module does chain-based outcome classification:

  Edit A (file X, old=O, new=N)   ← prior
  Edit B (file X, old=O', new=N') ← new arrival

Rules:
  * If B.old starts with A.new (B built on top of A) -> A.outcome = "refined"
  * If B.new is substantially A.old (B reverted A)    -> A.outcome = "reverted"
  * If B touches a different region entirely          -> A.outcome = "superseded"
  * If no B arrives within OUTCOME_WINDOW_MIN         -> A stays "pending"
    (later raised to "accepted" by the git-commit sweeper — not in this pass.)

Storage: sidecar table `coding_event_outcomes` in brain.db. raw_events stays
append-only + content-hashed; outcomes mutate independently.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

log = logging.getLogger("brain.coding_events")

OUTCOME_WINDOW_MIN = 120  # look back 2h when classifying
MIN_OVERLAP_CHARS = 20  # below this, fall back to "superseded"


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS coding_event_outcomes (
  event_id        TEXT PRIMARY KEY,
  outcome         TEXT NOT NULL,        -- refined | reverted | superseded | accepted | rejected | pending
  outcome_source  TEXT NOT NULL,        -- chain | git_commit | correction | session_end
  outcome_ts      TEXT NOT NULL,
  next_event_id   TEXT,
  commit_sha      TEXT,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_coding_outcomes_next ON coding_event_outcomes(next_event_id);
CREATE INDEX IF NOT EXISTS idx_coding_outcomes_outcome ON coding_event_outcomes(outcome);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA_DDL)
        conn.commit()


from db import now_iso as _now_iso  # noqa: E402  — single-source UTC stamp helper


def _parse_fts_row(row: sqlite3.Row) -> dict:
    """Decode a raw_events row back into its fields for classification.

    The FTS content is the synthesized human text we built in capture_generic
    ("Edit on /path session=S cwd=... status=ok old:... new:..."). We need
    to pull file_path + old_preview + new_preview out of it. The json_path
    column points to the full raw/inbox/*.json which has the structured
    payload — prefer that when available.
    """
    import json
    import re

    json_path = dict(row).get("json_path")
    if json_path:
        try:
            with Path(json_path).open() as f:
                rec = json.load(f)
            payload = json.loads(rec.get("content", "{}"))
            return {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "file_path": payload.get("file_path", ""),
                "tool": payload.get("tool", ""),
                "old": payload.get("old_preview", ""),
                "new": payload.get("new_preview", ""),
                "session_id": payload.get("session_id", ""),
            }
        except Exception as exc:
            log.debug("failed to parse coding event json payload: %s", exc)

    # Fallback: regex the synthesized content.
    content = row["content"] or ""
    m_file = re.search(r"(?:Edit|Write|NotebookEdit) on (\S+)", content)
    m_old = re.search(r"\bold:(.+?)(?: new:|$)", content, re.DOTALL)
    m_new = re.search(r"\bnew:(.+?)$", content, re.DOTALL)
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "file_path": m_file.group(1) if m_file else "",
        "tool": "",
        "old": (m_old.group(1).strip() if m_old else ""),
        "new": (m_new.group(1).strip() if m_new else ""),
        "session_id": "",
    }


def _escape_like(s: str) -> str:
    """Escape SQL LIKE wildcards so a file path with '%' or '_' matches
    literally. Unix paths rarely contain these but legal in the spec."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_recent_events_for_file(
    file_path: str, *, within_minutes: int = OUTCOME_WINDOW_MIN, exclude_event_id: str | None = None
) -> list[dict]:
    """Return raw_events rows matching this file_path within the time window."""
    if not file_path:
        return []
    cutoff = (datetime.now(UTC) - timedelta(minutes=within_minutes)).isoformat(timespec="seconds")
    like_pattern = f"%{_escape_like(file_path)}%"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, content, json_path FROM raw_events "
            "WHERE source_type='coding_event' "
            "  AND content LIKE ? ESCAPE '\\' "
            "  AND timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT 20",
            (like_pattern, cutoff),
        ).fetchall()
    events = []
    for r in rows:
        if exclude_event_id and r["id"] == exclude_event_id:
            continue
        ev = _parse_fts_row(r)
        if ev.get("file_path") == file_path:
            events.append(ev)
    return events


def _substantial_prefix_overlap(a: str, b: str, min_chars: int = MIN_OVERLAP_CHARS) -> bool:
    """True if either string starts with a substantial chunk of the other."""
    if not a or not b:
        return False
    a = a.strip()
    b = b.strip()
    if len(a) < min_chars or len(b) < min_chars:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.startswith(shorter[: max(min_chars, len(shorter) // 2)])


def _roughly_equal(a: str, b: str, min_chars: int = MIN_OVERLAP_CHARS) -> bool:
    """True if two strings are substantially the same text (allowing trim)."""
    if not a or not b:
        return False
    a = a.strip()
    b = b.strip()
    if len(a) < min_chars or len(b) < min_chars:
        return a == b
    shorter = min(len(a), len(b))
    longer = max(len(a), len(b))
    if shorter / longer < 0.6:
        return False
    return a[:shorter] == b[:shorter] or a in b or b in a


def classify_outcome(prior: dict, new: dict) -> str:
    """Decide what the PRIOR event's outcome is, given a new event on same file.

    Returns one of: refined | reverted | superseded.
    """
    prior_new = prior.get("new", "")
    new_old = new.get("old", "")
    new_new = new.get("new", "")
    prior_old = prior.get("old", "")

    # Revert: new edit's result looks like what was there before prior edit.
    if _roughly_equal(new_new, prior_old):
        return "reverted"
    # Refinement: new edit starts from prior edit's result (built on top).
    if _substantial_prefix_overlap(prior_new, new_old):
        return "refined"
    if _roughly_equal(prior_new, new_old):
        return "refined"
    # Otherwise a different region of the same file got edited.
    return "superseded"


def upsert_outcome(
    *,
    event_id: str,
    outcome: str,
    outcome_source: str,
    next_event_id: str | None = None,
    commit_sha: str | None = None,
    note: str | None = None,
) -> None:
    ensure_schema()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO coding_event_outcomes "
            "(event_id, outcome, outcome_source, outcome_ts, next_event_id, commit_sha, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, outcome, outcome_source, _now_iso(), next_event_id, commit_sha, note),
        )
        conn.commit()
    # Signal-driven wake: revert/rejected outcomes are worth brain_loop
    # reacting to immediately (pattern detection). Refined/superseded are
    # routine and don't warrant an interrupt. Fire-and-forget.
    if outcome in ("reverted", "rejected"):
        try:
            from pathlib import Path as _P

            _P("/tmp/.brain_loop_wake").touch()
        except OSError:
            pass


def classify_on_new_event(new_event: dict) -> dict | None:
    """Main entry point — called from capture_generic after a new coding_event
    is persisted. Looks at prior events on the same file, classifies the most
    recent one, writes outcome. Returns {prior_id, outcome} for logging or None.
    """
    fp = new_event.get("file_path")
    if not fp:
        return None
    try:
        prior_events = get_recent_events_for_file(fp, exclude_event_id=new_event.get("id"))
    except Exception as exc:
        log.debug("recent events query failed: %s", exc)
        return None
    if not prior_events:
        return None
    prior = prior_events[0]  # most recent
    if not prior.get("id"):
        return None
    # Skip if prior already has a classified outcome — don't overwrite.
    try:
        with _conn() as conn:
            existing = conn.execute(
                "SELECT outcome FROM coding_event_outcomes WHERE event_id = ?",
                (prior["id"],),
            ).fetchone()
        if existing and existing["outcome"] not in (None, "pending"):
            return None
    except Exception as exc:
        # table might not exist yet — ensure_schema will be called by upsert
        log.debug("coding outcome lookup skipped: %s", exc)

    outcome = classify_outcome(prior, new_event)
    upsert_outcome(
        event_id=prior["id"],
        outcome=outcome,
        outcome_source="chain",
        next_event_id=new_event.get("id"),
        note=f"chain: {new_event.get('tool', '?')} on {fp}",
    )
    return {"prior_id": prior["id"], "outcome": outcome, "via": new_event.get("id")}


def get_outcomes_for_file(file_path: str, *, limit: int = 10) -> list[dict]:
    """Read-side helper — returns recent coding_events for a file with their
    outcomes joined. Used by the /brain/coding_events endpoint and by
    predictive/active-recall weighting later."""
    if not file_path:
        return []
    ensure_schema()
    like_pattern = f"%{_escape_like(file_path)}%"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT re.id, re.timestamp, re.content, re.json_path, "
            "       co.outcome, co.outcome_source, co.outcome_ts, co.next_event_id "
            "FROM raw_events re "
            "LEFT JOIN coding_event_outcomes co ON co.event_id = re.id "
            "WHERE re.source_type='coding_event' AND re.content LIKE ? ESCAPE '\\' "
            "ORDER BY re.timestamp DESC LIMIT ?",
            (like_pattern, limit),
        ).fetchall()
    out = []
    for r in rows:
        ev = _parse_fts_row(r)
        if ev.get("file_path") != file_path:
            continue
        out.append(
            {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "file_path": ev["file_path"],
                "tool": ev.get("tool", ""),
                "outcome": r["outcome"] or "pending",
                "outcome_source": r["outcome_source"],
                "outcome_ts": r["outcome_ts"],
                "next_event_id": r["next_event_id"],
            }
        )
    return out


def outcome_stats(*, within_hours: int = 24) -> dict:
    """Aggregate outcome counts for dashboard / SLO use."""
    ensure_schema()
    cutoff = (datetime.now(UTC) - timedelta(hours=within_hours)).isoformat(timespec="seconds")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(co.outcome, 'pending') AS outcome, COUNT(*) AS n "
            "FROM raw_events re LEFT JOIN coding_event_outcomes co ON co.event_id = re.id "
            "WHERE re.source_type='coding_event' AND re.timestamp >= ? "
            "GROUP BY outcome",
            (cutoff,),
        ).fetchall()
    return {r["outcome"]: r["n"] for r in rows}


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--hours", type=int, default=24)

    p_file = sub.add_parser("file")
    p_file.add_argument("path")
    p_file.add_argument("--limit", type=int, default=10)

    sub.add_parser("migrate")

    args = p.parse_args()
    if args.cmd == "stats":
        print(json.dumps(outcome_stats(within_hours=args.hours), indent=2))
    elif args.cmd == "file":
        print(json.dumps(get_outcomes_for_file(args.path, limit=args.limit), indent=2, ensure_ascii=False))
    elif args.cmd == "migrate":
        ensure_schema()
        print("schema ensured")
