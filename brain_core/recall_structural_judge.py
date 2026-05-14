"""brain_core/recall_structural_judge.py — LLM-free structural scoring of
recent /recall outcomes.

The LLM recall_judge only covers ~1% of recall traffic (50 samples/day
on ~10k recalls/day) and bumping samples burns subscription quota.
This module is the deterministic complement: every eligible unlabeled
/recall row gets a structural score from cheap signals — query/doc token
overlap, top atom confidence, and basic freshness — and the result is stored
in a sidecar table. The LLM judge stays the high-precision arbiter; the
sidecar widens coverage without writing heuristic labels into
action_audit.outcome or blocking later LLM/manual judgment.

Costs: pure SQLite reads + Python set-intersection per row.
No new embedder calls. No outbound network. No LLM dispatch.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB
except ImportError:  # pragma: no cover - direct execution fallback
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

log = logging.getLogger("brain.recall_structural_judge")


STRUCTURAL_GOOD_THRESHOLD = 0.45
STRUCTURAL_WRONG_THRESHOLD = 0.10
SAMPLE_LIMIT = 1000
DEFAULT_HOURS = 6  # hourly cadence covers the last 6h with overlap
JUDGED_ACTORS = ("claude", "codex", "mcp", "jenna", "sage", "liz", "ellie", "market", "brain")
JUDGED_ACTOR_PLACEHOLDERS = "?, ?, ?, ?, ?, ?, ?, ?, ?"


_TOKEN_RE = re.compile(r"[A-Za-z가-힣0-9]+")
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "is",
        "it",
        "this",
        "that",
        "with",
        "as",
        "are",
        "was",
        "were",
        "be",
        "by",
        "from",
        "at",
        "i",
        "you",
        "we",
        "but",
        "not",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
    }
)


def run(
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = SAMPLE_LIMIT,
    dry_run: bool = False,
    brain_db_path: Path | str | None = None,
) -> dict:
    """Score unlabeled /recall outcomes inside the last `hours` window."""
    db_path = Path(brain_db_path or BRAIN_DB)
    counters = {
        "scanned": 0,
        "labeled_good": 0,
        "labeled_wrong": 0,
        "labeled_neutral": 0,
        "skipped_empty": 0,
        "skipped_parse": 0,
    }
    if not db_path.exists():
        counters["status"] = "db_missing"
        return counters
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not dry_run:
                _ensure_structural_table(conn)
            sidecar_filter = (
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM recall_structural_judgments rsj "
                "      WHERE rsj.action_audit_id = action_audit.id"
                "  ) "
                if _table_exists(conn, "recall_structural_judgments")
                else ""
            )
            cutoff = _cutoff_iso(hours)
            rows = conn.execute(
                "SELECT id, query_text, retrieved_atom_ids, retrieved_chroma_ids, created_at "
                "FROM action_audit "
                "WHERE route IN ('/recall', '/recall/v2', '/recall/active') "
                "  AND outcome IS NULL "
                "  AND query_text IS NOT NULL "
                "  AND length(query_text) >= 5 "
                "  AND actor IN (" + JUDGED_ACTOR_PLACEHOLDERS + ") "
                "  AND query_text NOT LIKE 'sed %' "
                "  AND query_text NOT LIKE 'grep %' "
                "  AND query_text NOT LIKE 'cat %' "
                "  AND query_text NOT LIKE 'awk %' "
                "  AND query_text NOT LIKE 'find %' "
                "  AND query_text NOT LIKE 'ls %' "
                "  AND query_text NOT LIKE 'echo %' "
                "  AND query_text NOT LIKE 'curl %' "
                "  AND query_text NOT LIKE 'rg %' "
                "  AND query_text NOT LIKE 'python %' "
                "  AND query_text NOT LIKE '/Users/%' "
                "  AND query_text NOT LIKE '%.py %' "
                "  AND query_text NOT LIKE '%.md %' "
                "  AND query_text NOT LIKE '{%' "
                "  AND query_text NOT LIKE '[%' "
                "  AND created_at > ? " + sidecar_filter + "ORDER BY created_at DESC LIMIT ?",
                (*JUDGED_ACTORS, cutoff, max(1, min(int(limit or SAMPLE_LIMIT), 5000))),
            ).fetchall()
            for row in rows:
                counters["scanned"] += 1
                # Recent /recall/v2 rows store Qdrant point IDs in
                # `retrieved_chroma_ids`, not atom IDs. Atoms carry a
                # `chroma_id` column so we can resolve either format.
                atom_ids = _parse_atom_ids(row["retrieved_atom_ids"])
                chroma_ids = _parse_atom_ids(row["retrieved_chroma_ids"])
                if not atom_ids and not chroma_ids:
                    counters["skipped_empty"] += 1
                    continue
                docs = _fetch_atom_docs(conn, atom_ids[:3], chroma_ids[:3])
                if not docs:
                    counters["skipped_empty"] += 1
                    continue
                score = _structural_score(row["query_text"], docs)
                outcome = _band(score)
                if outcome == "structural_good":
                    counters["labeled_good"] += 1
                elif outcome == "structural_wrong":
                    counters["labeled_wrong"] += 1
                else:
                    counters["labeled_neutral"] += 1
                if not dry_run:
                    _insert_structural_judgment(conn, row, outcome, score)
            if not dry_run:
                conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        counters["error"] = str(exc)[:120]
    counters["status"] = "ok"
    log.info("recall_structural_judge: %s", counters)
    return counters


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _ensure_structural_table(conn: sqlite3.Connection) -> None:
    """Create the sidecar judgment table owned by recall_structural_judge."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recall_structural_judgments (
            action_audit_id INTEGER PRIMARY KEY,
            outcome TEXT NOT NULL,
            structural_score REAL NOT NULL,
            reason_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            judged_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recall_structural_judgments_outcome
          ON recall_structural_judgments(outcome, created_at);
        """
    )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _insert_structural_judgment(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    outcome: str,
    score: float,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO recall_structural_judgments "
        "(action_audit_id, outcome, structural_score, reason_json, created_at, judged_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            outcome,
            float(score),
            json.dumps({"structural_score": round(score, 3)}),
            row["created_at"] or _now_iso(),
            _now_iso(),
        ),
    )


def _structural_score(query: str, docs: list[dict]) -> float:
    """Return a value in [0,1] estimating how well the docs answer the query.

    Components:
      - token_overlap: Jaccard between query tokens and top-3 doc tokens.
      - confidence: mean confidence of the top-3 atoms (caps influence).
      - freshness: `1 - age_days/180` clipped to [0,1].
    Weights: 0.60 overlap + 0.25 confidence + 0.15 freshness. Single doc
    with a high confidence and decent overlap is enough to clear the
    `structural_good` band; pure overlap on stale docs is not.
    """
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    overlap_parts: list[float] = []
    conf_parts: list[float] = []
    fresh_parts: list[float] = []
    for doc in docs[:3]:
        text = doc.get("text") or ""
        d_tokens = _tokenize(text)
        if d_tokens:
            inter = len(q_tokens & d_tokens)
            union = len(q_tokens | d_tokens)
            overlap_parts.append(inter / union if union else 0.0)
        conf = _safe_float(doc.get("confidence"))
        if conf is not None:
            conf_parts.append(min(max(conf, 0.0), 1.0))
        updated = doc.get("updated_at")
        if updated:
            age = _age_days(updated)
            if age is not None:
                fresh_parts.append(max(0.0, 1.0 - age / 180))
    overlap = max(overlap_parts) if overlap_parts else 0.0
    confidence = sum(conf_parts) / len(conf_parts) if conf_parts else 0.5
    freshness = sum(fresh_parts) / len(fresh_parts) if fresh_parts else 0.5
    return round(0.60 * overlap + 0.25 * confidence + 0.15 * freshness, 4)


def _band(score: float) -> str:
    if score >= STRUCTURAL_GOOD_THRESHOLD:
        return "structural_good"
    if score <= STRUCTURAL_WRONG_THRESHOLD:
        return "structural_wrong"
    return "structural_neutral"


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {tok.lower() for tok in _TOKEN_RE.findall(text)[:200] if tok.lower() not in _STOPWORDS}


def _parse_atom_ids(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x]
    return []


def _fetch_atom_docs(
    conn: sqlite3.Connection,
    atom_ids: list[str],
    chroma_ids: list[str],
) -> list[dict]:
    rows: list[sqlite3.Row] = []
    if atom_ids:
        placeholders = ",".join("?" * len(atom_ids))
        with contextlib.suppress(sqlite3.Error):
            rows.extend(
                conn.execute(
                    f"SELECT id, text, confidence, updated_at FROM atoms "  # noqa: S608 — fixed placeholders
                    f"WHERE id IN ({placeholders}) LIMIT 10",
                    atom_ids,
                ).fetchall()
            )
    if chroma_ids:
        placeholders = ",".join("?" * len(chroma_ids))
        with contextlib.suppress(sqlite3.Error):
            rows.extend(
                conn.execute(
                    f"SELECT id, text, confidence, updated_at FROM atoms "  # noqa: S608 — fixed placeholders
                    f"WHERE chroma_id IN ({placeholders}) LIMIT 10",
                    chroma_ids,
                ).fetchall()
            )
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        rid = str(row["id"])
        if rid in seen:
            continue
        seen.add(rid)
        out.append(
            {
                "id": rid,
                "text": row["text"] or "",
                "confidence": row["confidence"],
                "updated_at": row["updated_at"],
            }
        )
    return out


def _safe_float(value: object) -> float | None:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return None


def _age_days(ts: object) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - dt).total_seconds() / 86400)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cutoff_iso(hours: int) -> str:
    """Return a cutoff in the same ISO shape as action_audit.created_at.

    SQLite's datetime('now') uses a space separator. Most action_audit rows are
    ISO-8601 with ``T``/``+00:00`` or ``Z``; lexicographic comparisons against
    SQLite's space-form datetime incorrectly include older same-day rows.
    """

    return (
        (datetime.now(UTC) - timedelta(hours=max(1, int(hours or DEFAULT_HOURS))))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    p.add_argument("--limit", type=int, default=SAMPLE_LIMIT)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(json.dumps(run(hours=args.hours, limit=args.limit, dry_run=args.dry_run), indent=2))
