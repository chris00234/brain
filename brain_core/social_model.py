"""brain_core/social_model.py — Theory-of-Mind layer (D6).

Biological motivation: humans use medial prefrontal cortex + temporo-parietal
junction to model other people's beliefs, desires, and likely reactions —
"theory of mind." This module gives the brain an explicit, queryable
representation of what each agent (Jenna, Liz, Ellie, Sage, Market) and
named human (Chris, his wife, coworkers) BELIEVES, WANTS, and KNOWS.

Distinct from atoms/facts about people. atoms store "Chris prefers npm",
which is a statement Chris would agree with. social_model stores "Jenna
believes Chris prefers npm" — a model of someone else's belief state. The
two can disagree (mistakes, stale beliefs, in-progress updates), and
modeling that gap is the whole point.

Use cases:
  - Before delegating to Jenna, query her model: does she already know X?
  - Before pinging Chris's wife about Y, check what she's been told.
  - Track when an agent's belief diverges from reality (calibration signal).
  - Generate "who-should-I-tell" recommendations from belief gaps.

Schema (separate autonomy.db table — no atoms schema mutation):
  social_beliefs:
    id              TEXT PK
    subject         TEXT  -- name of the modeled entity (agent or human)
    subject_kind    TEXT  -- 'agent' | 'human'
    belief          TEXT  -- the belief content
    confidence      REAL  -- 0..1, brain's confidence in this attribution
    source          TEXT  -- how brain learned this ('observed_conversation',
                          --   'inferred_from_role', 'told_by_chris', etc.)
    last_observed_at TEXT -- when belief was last confirmed
    superseded_by   TEXT  -- chain when belief is revised
    created_at      TEXT
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

DB_PATH = BRAIN_LOGS_DIR / "autonomy.db"
log = logging.getLogger("brain.social_model")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_beliefs (
    id              TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,
    subject_kind    TEXT NOT NULL DEFAULT 'agent',
    belief          TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.5,
    source          TEXT NOT NULL DEFAULT '',
    last_observed_at TEXT NOT NULL,
    superseded_by   TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_subject ON social_beliefs(subject);
CREATE INDEX IF NOT EXISTS idx_social_active ON social_beliefs(subject)
    WHERE superseded_by IS NULL;
"""

_schema_done = False


def _ensure_schema() -> None:
    global _schema_done
    if _schema_done:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _schema_done = True
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _gen_id() -> str:
    return f"sb_{uuid.uuid4().hex[:12]}"


def record_belief(
    subject: str,
    belief: str,
    *,
    subject_kind: str = "agent",
    confidence: float = 0.5,
    source: str = "",
    supersedes: str | None = None,
) -> dict:
    """Record (or supersede) a belief that `subject` holds.

    If `supersedes` is given, the named row is marked superseded_by this one,
    so historical revisions are preserved.
    """
    if not subject or not belief:
        return {"ok": False, "error": "empty_subject_or_belief"}
    _ensure_schema()
    confidence = max(0.0, min(1.0, float(confidence)))
    now = _now_iso()
    new_id = _gen_id()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("BEGIN IMMEDIATE")
        if supersedes:
            conn.execute(
                "UPDATE social_beliefs SET superseded_by = ? WHERE id = ?",
                (new_id, supersedes),
            )
        conn.execute(
            "INSERT INTO social_beliefs "
            "(id, subject, subject_kind, belief, confidence, source, "
            " last_observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id, subject, subject_kind, belief[:1000], confidence, source[:200], now, now),
        )
        conn.commit()
        return {
            "ok": True,
            "id": new_id,
            "subject": subject,
            "subject_kind": subject_kind,
            "confidence": confidence,
        }
    finally:
        conn.close()


def get_subject_model(subject: str, limit: int = 50) -> dict:
    """Return active beliefs for one subject (superseded entries excluded)."""
    _ensure_schema()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, subject_kind, belief, confidence, source, last_observed_at, created_at "
            "FROM social_beliefs "
            "WHERE subject = ? AND superseded_by IS NULL "
            "ORDER BY confidence DESC, last_observed_at DESC "
            "LIMIT ?",
            (subject, limit),
        ).fetchall()
        return {
            "subject": subject,
            "count": len(rows),
            "beliefs": [
                {
                    "id": r["id"],
                    "subject_kind": r["subject_kind"],
                    "belief": r["belief"],
                    "confidence": round(float(r["confidence"]), 4),
                    "source": r["source"],
                    "last_observed_at": r["last_observed_at"],
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


def list_subjects() -> list[dict]:
    """Return all distinct subjects with belief counts."""
    _ensure_schema()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT subject, subject_kind, COUNT(*) as belief_count, "
            "       MAX(last_observed_at) as last_seen "
            "FROM social_beliefs "
            "WHERE superseded_by IS NULL "
            "GROUP BY subject "
            "ORDER BY belief_count DESC"
        ).fetchall()
        return [
            {
                "subject": r[0],
                "subject_kind": r[1],
                "belief_count": int(r[2]),
                "last_seen": r[3],
            }
            for r in rows
        ]
    finally:
        conn.close()


# Known-good seed: the OpenClaw agents and Chris himself. Sources from
# CLAUDE.md + ~/.openclaw/. Confidence high (0.9) because these are
# canonical role definitions, not inferred behavior.
SEEDS: list[dict] = [
    {
        "subject": "jenna",
        "kind": "agent",
        "belief": "Role: chief of staff. Routes work, runs daily/weekly digest, owns Telegram alert path.",
    },
    {
        "subject": "jenna",
        "kind": "agent",
        "belief": "Has Chris's full session history through openclaw_sessions_ingest. Uses ChatGPT Pro subscription via codex CLI.",
    },
    {
        "subject": "liz",
        "kind": "agent",
        "belief": "Role: engineering. Owns build/test/debug for brain + brain-ui + OpenClaw stack. Independent code reviews.",
    },
    {
        "subject": "ellie",
        "kind": "agent",
        "belief": "Role: infrastructure. Owns Docker/OrbStack, launchd, nginx, Cloudflare Tunnel, homelab health.",
    },
    {
        "subject": "sage",
        "kind": "agent",
        "belief": "Role: research. Runs dream_replay, profile_regen, entity_pages, deep synthesis queries.",
    },
    {
        "subject": "market",
        "kind": "agent",
        "belief": "Role: growth. Tracks side-project metrics, content cadence, audience signals.",
    },
    {
        "subject": "chris",
        "kind": "human",
        "belief": "Owns the system. Software engineer at GIT America Inc, Irvine CA. Korean name 조대현 / Daehyun Cho.",
    },
    {
        "subject": "chris",
        "kind": "human",
        "belief": "Communication preference: direct, concise, no filler, no emoji, no sycophancy. Pushes back on bad approaches expected.",
    },
    {
        "subject": "chris",
        "kind": "human",
        "belief": "Long-term goal as of 2026-05-12: brain becomes a true human-brain replacement, world-class sophistication.",
    },
]


def seed_known_agents() -> dict:
    """Idempotently insert the canonical seed beliefs.

    Skips rows that already exist (matched by subject + belief text).
    Wrapped in BEGIN IMMEDIATE so a concurrent record_belief cannot
    insert a duplicate between our SELECT and INSERT (D1-D10 review fix).
    """
    _ensure_schema()
    conn = sqlite3.connect(str(DB_PATH))
    inserted = 0
    skipped = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for seed in SEEDS:
            existing = conn.execute(
                "SELECT 1 FROM social_beliefs "
                "WHERE subject = ? AND belief = ? AND superseded_by IS NULL "
                "LIMIT 1",
                (seed["subject"], seed["belief"]),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            now = _now_iso()
            conn.execute(
                "INSERT INTO social_beliefs "
                "(id, subject, subject_kind, belief, confidence, source, "
                " last_observed_at, created_at) "
                "VALUES (?, ?, ?, ?, 0.9, 'seed:claude_md_canonical', ?, ?)",
                (_gen_id(), seed["subject"], seed["kind"], seed["belief"], now, now),
            )
            inserted += 1
        conn.commit()
        return {"inserted": inserted, "skipped": skipped, "total_seeds": len(SEEDS)}
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("seed")
    p_get = sub.add_parser("get")
    p_get.add_argument("subject")
    sub.add_parser("list")
    args = p.parse_args()
    if args.cmd == "seed":
        print(json.dumps(seed_known_agents(), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.cmd == "get":
        print(json.dumps(get_subject_model(args.subject), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.cmd == "list":
        print(json.dumps(list_subjects(), indent=2, ensure_ascii=False))  # noqa: T201
    else:
        p.print_help()
