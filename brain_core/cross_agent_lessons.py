"""brain_core/cross_agent_lessons.py — extract lesson atoms from agent transcripts.

Brain ingests OpenClaw agent transcripts into raw_events + distills them into
atoms, but the distillation extracts FACTS, not (intent → action → outcome)
lessons. So when Sage tries an approach that fails, the failure becomes a
fact ("Sage attempted X on date Y") instead of a procedural memory ("avoid
X for queries like Y"). The skill_materializer never sees the lesson.

This module mines recent agent atoms for failure/correction signals and
emits 'lesson' atoms with structured intent/action/outcome metadata. The
existing materialize_all_procedures path then consumes them as skill seeds.

Triggers (regex on atom text):
  - English: failed, wrong, broke, "should have", "instead of", lesson, mistake
  - Korean: 실패, 잘못, 틀렸, 깨졌, "대신", 교훈, 실수

Output: writes new atoms with category='lesson' via /memory POST. The
existing dedup + paraphrase gate handles repeat lessons.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BRAIN_DB

log = logging.getLogger("brain.cross_agent_lessons")

LESSON_PATTERNS = [
    re.compile(r"\b(?:failed|broke|wrong|mistake|should have|instead of)\b", re.I),
    re.compile(r"\b(?:lesson|learned|takeaway|avoid|don'?t)\b", re.I),
    re.compile(r"실패|잘못|틀렸|깨졌|교훈|실수|대신"),
]
AGENT_NAMES = {"jenna", "liz", "ellie", "sage", "market", "claude", "brain"}


def _has_lesson_signal(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in LESSON_PATTERNS)


def _extract_agents(text: str) -> list[str]:
    """Find named agents mentioned in the atom body."""
    if not text:
        return []
    found = set()
    lower = text.lower()
    for name in AGENT_NAMES:
        if re.search(rf"\b{name}\b", lower):
            found.add(name)
    return sorted(found)


def run(hours: int = 24, dry_run: bool = False) -> dict:
    """Scan recent atoms for lesson signals; surface high-signal ones to skill pipeline.

    Strategy: mark candidate atoms with metadata.lesson_candidate=1 + extracted
    agents list so skill_materializer / canonical promotion can pick them up.
    Doesn't create new atoms (avoids the dedup+contradiction overhead) — just
    flags existing atoms with a structured tag.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    counters = {"scanned": 0, "flagged": 0, "no_signal": 0}
    try:
        # Schema migration: add lesson_candidate + lesson_agents columns to atoms
        # if not present. ALTER failures must surface — silently swallowing
        # them would leave the next SELECT crashing on "no such column" with
        # no clear cause.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(atoms)").fetchall()}
        if "lesson_candidate" not in existing_cols:
            conn.execute("ALTER TABLE atoms ADD COLUMN lesson_candidate INTEGER DEFAULT 0")
        if "lesson_agents" not in existing_cols:
            conn.execute("ALTER TABLE atoms ADD COLUMN lesson_agents TEXT DEFAULT ''")

        rows = conn.execute(
            "SELECT chroma_id, text, kind FROM atoms "
            "WHERE created_at > ? "
            "  AND text IS NOT NULL "
            "  AND (lesson_candidate = 0 OR lesson_candidate IS NULL) "
            "LIMIT 5000",
            (cutoff,),
        ).fetchall()

        for row in rows:
            counters["scanned"] += 1
            text = row["text"]
            if not _has_lesson_signal(text):
                counters["no_signal"] += 1
                continue
            agents = _extract_agents(text)
            if not agents:
                # Lesson signal but no agent mentioned — skip; this isn't
                # cross-agent, it's a generic fact.
                counters["no_signal"] += 1
                continue
            counters["flagged"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE atoms SET lesson_candidate = 1, lesson_agents = ? " "WHERE chroma_id = ?",
                    (json.dumps(agents), row["chroma_id"]),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    log.info("cross_agent_lessons: %s", counters)
    return counters


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(hours=args.hours, dry_run=args.dry_run), indent=2))
