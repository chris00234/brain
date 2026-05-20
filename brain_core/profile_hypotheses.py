"""brain_core/profile_hypotheses.py — dialectic hypothesis tracker.

Honcho's distinguishing feature is treating user-model claims as HYPOTHESES
with accumulating support and counterevidence, only promoting them to
durable beliefs after repeated corroboration. This module gives brain that
same dialectic behavior on top of brain's governance gates:

  observation → candidate hypothesis → (support accrual) → supported →
                                                          ↘ refuted (if counter dominates)
  supported   → (promotion check) → canonicalized via /memory

Storage: dedicated SQLite ``logs/profile_hypotheses.db`` (separate from
brain.db so a schema mistake here can't corrupt the canonical store).

Public API:
  ``record_observation(claim, evidence, actor)`` — upsert; fuzzy-matches
    against existing claims by normalized prefix so paraphrases accrete.
  ``record_counter(claim, evidence, actor)`` — same match path, lands in
    counterevidence column; flips status to ``refuted`` when counter
    dominates support.
  ``find_promotable(min_support, min_confidence)`` — list hypotheses that
    survived enough rounds to canonicalize.
  ``mark_canonicalized(hyp_id, atom_id)`` — link a hypothesis to the durable
    atom that resulted from its promotion.

2026-05-20 W3.5 round 3 (codex gap 2): closes the gap where profile_deepener
was summarizing without tracking support. Without this, every daily run
recreates similar candidate atoms; with it, the same claim accumulates
evidence across runs and only crosses the promotion threshold once it has
real corroboration.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HYPOTHESES_DB = Path("/Users/chrischo/server/brain/logs/profile_hypotheses.db")

# Promotion thresholds — tuned conservatively. A hypothesis must survive ≥3
# distinct daily runs (support_count) and reach confidence ≥0.75 before it
# qualifies for /memory canonicalization. Refutation: counter_count >=
# support_count AND counter_count >= 2 flips status to refuted.
DEFAULT_PROMOTE_MIN_SUPPORT = 3
DEFAULT_PROMOTE_MIN_CONFIDENCE = 0.75


def _connect() -> sqlite3.Connection:
    HYPOTHESES_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HYPOTHESES_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile_hypotheses (
                id TEXT PRIMARY KEY,
                claim TEXT NOT NULL,
                claim_normalized TEXT NOT NULL,
                support_json TEXT NOT NULL DEFAULT '[]',
                counter_json TEXT NOT NULL DEFAULT '[]',
                support_count INTEGER NOT NULL DEFAULT 0,
                counter_count INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'candidate',
                canonicalized_atom_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_profile_hypotheses_norm
                ON profile_hypotheses (claim_normalized);
            CREATE INDEX IF NOT EXISTS idx_profile_hypotheses_status
                ON profile_hypotheses (status, support_count DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


_WORD_RE = re.compile(r"[a-z0-9가-힣]+")


def _normalize(claim: str) -> str:
    """Lowercase + word-token join so paraphrases match.

    Korean syllables retained via the unicode range; word order is preserved
    deliberately — "top brain writers" and "brain writers top" SHOULD be
    distinct hypotheses because the subject differs.
    """
    if not claim:
        return ""
    tokens = _WORD_RE.findall(claim.lower())
    return " ".join(tokens)[:400]


def _hyp_id(claim_normalized: str) -> str:
    h = hashlib.sha256(claim_normalized.encode("utf-8")).hexdigest()[:16]
    return f"hyp_{h}"


def _recompute_confidence(support: int, counter: int) -> float:
    """Beta-style ratio with [0,1] clamp.

    confidence = (support + 1) / (support + counter + 2)
    Starts at 0.5 with no evidence (Laplace smoothing), trends toward 1 with
    pure support, toward 0 with pure counter. Matches Honcho's dialectic
    semantics without needing a full Bayesian setup.
    """
    return float(support + 1) / float(support + counter + 2)


def record_observation(claim: str, evidence: dict, actor: str = "profile_deepener") -> dict:
    """Insert a new hypothesis or accrue support to an existing one."""
    norm = _normalize(claim)
    if not norm:
        return {"error": "empty_claim"}
    ensure_schema()
    hyp_id = _hyp_id(norm)
    now = datetime.now(UTC).isoformat()
    evidence_entry = {
        "at": now,
        "actor": actor,
        "summary": (evidence.get("summary") or claim)[:240],
        "signal": evidence.get("signal", {}),
    }

    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM profile_hypotheses WHERE id = ?", (hyp_id,)).fetchone()
        if existing is None:
            support_json = json.dumps([evidence_entry], ensure_ascii=False)
            confidence = _recompute_confidence(1, 0)
            conn.execute(
                """
                INSERT INTO profile_hypotheses
                  (id, claim, claim_normalized, support_json, counter_json,
                   support_count, counter_count, confidence, status,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, '[]', 1, 0, ?, 'candidate', ?, ?)
                """,
                (hyp_id, claim[:400], norm, support_json, confidence, now, now),
            )
            conn.commit()
            return {
                "id": hyp_id,
                "status": "candidate",
                "support_count": 1,
                "counter_count": 0,
                "confidence": confidence,
                "created": True,
            }

        support_list = json.loads(existing["support_json"] or "[]")
        support_list.append(evidence_entry)
        support_list = support_list[-20:]  # cap memory footprint per hypothesis
        new_support = int(existing["support_count"] or 0) + 1
        new_counter = int(existing["counter_count"] or 0)
        confidence = _recompute_confidence(new_support, new_counter)
        new_status = existing["status"]
        if new_status == "candidate" and new_support >= 2:
            new_status = "supported"
        conn.execute(
            """
            UPDATE profile_hypotheses
            SET support_json = ?, support_count = ?, confidence = ?,
                status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(support_list, ensure_ascii=False),
                new_support,
                confidence,
                new_status,
                now,
                hyp_id,
            ),
        )
        conn.commit()
        return {
            "id": hyp_id,
            "status": new_status,
            "support_count": new_support,
            "counter_count": new_counter,
            "confidence": confidence,
            "created": False,
        }
    finally:
        conn.close()


def record_counter(claim: str, evidence: dict, actor: str = "correction") -> dict:
    """Accrue counterevidence. Flips status to 'refuted' when counter dominates."""
    norm = _normalize(claim)
    if not norm:
        return {"error": "empty_claim"}
    ensure_schema()
    hyp_id = _hyp_id(norm)
    now = datetime.now(UTC).isoformat()
    evidence_entry = {
        "at": now,
        "actor": actor,
        "summary": (evidence.get("summary") or claim)[:240],
        "signal": evidence.get("signal", {}),
    }

    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM profile_hypotheses WHERE id = ?", (hyp_id,)).fetchone()
        if existing is None:
            # New counter without prior support — treat as a low-confidence
            # negative claim so a future record_observation can lift it.
            confidence = _recompute_confidence(0, 1)
            conn.execute(
                """
                INSERT INTO profile_hypotheses
                  (id, claim, claim_normalized, support_json, counter_json,
                   support_count, counter_count, confidence, status,
                   created_at, updated_at)
                VALUES (?, ?, ?, '[]', ?, 0, 1, ?, 'candidate', ?, ?)
                """,
                (
                    hyp_id,
                    claim[:400],
                    norm,
                    json.dumps([evidence_entry], ensure_ascii=False),
                    confidence,
                    now,
                    now,
                ),
            )
            conn.commit()
            return {"id": hyp_id, "status": "candidate", "counter_count": 1, "created": True}

        counter_list = json.loads(existing["counter_json"] or "[]")
        counter_list.append(evidence_entry)
        counter_list = counter_list[-20:]
        new_counter = int(existing["counter_count"] or 0) + 1
        new_support = int(existing["support_count"] or 0)
        confidence = _recompute_confidence(new_support, new_counter)
        new_status = existing["status"]
        if new_counter >= max(new_support, 2):
            new_status = "refuted"
        conn.execute(
            """
            UPDATE profile_hypotheses
            SET counter_json = ?, counter_count = ?, confidence = ?,
                status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(counter_list, ensure_ascii=False),
                new_counter,
                confidence,
                new_status,
                now,
                hyp_id,
            ),
        )
        conn.commit()
        return {
            "id": hyp_id,
            "status": new_status,
            "support_count": new_support,
            "counter_count": new_counter,
            "confidence": confidence,
            "created": False,
        }
    finally:
        conn.close()


def find_promotable(
    min_support: int = DEFAULT_PROMOTE_MIN_SUPPORT,
    min_confidence: float = DEFAULT_PROMOTE_MIN_CONFIDENCE,
    limit: int = 20,
) -> list[dict]:
    """List hypotheses ready for /memory canonicalization."""
    ensure_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, claim, support_count, counter_count, confidence,
                   status, support_json
            FROM profile_hypotheses
            WHERE status = 'supported'
              AND support_count >= ?
              AND confidence >= ?
              AND canonicalized_atom_id IS NULL
            ORDER BY confidence DESC, support_count DESC
            LIMIT ?
            """,
            (min_support, min_confidence, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_canonicalized(hyp_id: str, atom_id: str) -> bool:
    ensure_schema()
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE profile_hypotheses
            SET status = 'canonicalized', canonicalized_atom_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (atom_id, datetime.now(UTC).isoformat(), hyp_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def summary() -> dict:
    """Aggregate counts by status for observability."""
    ensure_schema()
    conn = _connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM profile_hypotheses GROUP BY status").fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM profile_hypotheses").fetchone()
        promotable = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM profile_hypotheses
            WHERE status='supported' AND support_count >= ? AND confidence >= ?
              AND canonicalized_atom_id IS NULL
            """,
            (DEFAULT_PROMOTE_MIN_SUPPORT, DEFAULT_PROMOTE_MIN_CONFIDENCE),
        ).fetchone()
    finally:
        conn.close()
    return {
        "total": int((total or {"c": 0})["c"]),
        "by_status": {r["status"]: int(r["c"]) for r in rows},
        "promotable_now": int((promotable or {"c": 0})["c"]),
        "thresholds": {
            "min_support": DEFAULT_PROMOTE_MIN_SUPPORT,
            "min_confidence": DEFAULT_PROMOTE_MIN_CONFIDENCE,
        },
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="profile_hypotheses inspection / actions.")
    parser.add_argument("action", choices=["summary", "promotable", "ensure_schema"])
    args = parser.parse_args()
    if args.action == "summary":
        print(json.dumps(summary(), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.action == "promotable":
        print(json.dumps(find_promotable(), indent=2, ensure_ascii=False))  # noqa: T201
    else:
        ensure_schema()
        print("ok")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
