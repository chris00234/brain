#!/usr/bin/env python3
"""Auto-act on chris_override=true outcomes (lightweight closed-loop).

This is NOT a full fix for the brain's recommendation-pipeline-bypasses-atoms
problem. The deeper issue: brain synthesizes recommendations (e.g.,
"skip-infra is acceptable") via LLM without first checking stored atoms, so
Chris's correction atoms (10x "Chris does not consider skipping infra
acceptable") have zero effect on next-turn recommendations.

What this script does (interim closure):
  1. Walk recent outcomes where chris_override=1.
  2. For each, find atoms whose text semantically matches actual_action
     (Chris's correction). Use simple text-match because we don't want to
     spend embeddings here — these atoms were already created by
     brain_correct so they share keywords.
  3. Boost trust_score on matching atoms by +0.02 per override (cap 1.0).
     Multiple corrections on the same belief reinforce it.
  4. Mark outcome.acked=1 so we don't double-count.

Designed to run nightly via the brain scheduler. Idempotent — acked flag
prevents repeat work.

NEXT STEP after this (codex co-design):
  Make the recommendation synthesis path call brain_recall(filter agent=chris)
  before publishing a recommendation, and inject contradicting atoms into the
  LLM prompt. Without that, this trust_score boost has no immediate effect
  because nobody reads trust_score at recommendation time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Local import — same package.
sys.path.insert(0, str(Path(__file__).parent))
from db import retrying_transaction

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
LEDGER_FILE = Path("/Users/chrischo/server/brain/logs/auto_supersede_ledger.json")
TRUST_DELTA = 0.02
TRUST_CAP = 1.0
LOOKBACK_DAYS = 30
# 2026-05-19: Wrong-atom demotion. When a chris_override outcome records
# brain_recommendation=X, search for atoms whose text matches X and shrink
# their trust_score so they fall in future recalls. Closes the gap the file
# docstring flagged: trust_score boost on correction atoms had no path to
# demote the wrong atom that paraphrased its way past the cosine gate.
TRUST_DEMOTE = 0.08
TRUST_FLOOR = 0.10
DEBOOST_WEIGHT_DEMOTE = 0.4  # writes into atom_deboost for recall-path use


def _load_ledger() -> set[str]:
    if not LEDGER_FILE.exists():
        return set()
    try:
        data = json.loads(LEDGER_FILE.read_text())
        return set(data.get("processed_outcome_ids", []))
    except Exception:
        return set()


def _save_ledger(ledger: set[str]) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps({"processed_outcome_ids": sorted(ledger)}, indent=2))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _snippet_for_like(text: str) -> str | None:
    """Pick a 6-word middle slice as a LIKE key. Returns None if unusable."""
    text = (text or "").strip()
    if len(text) < 30:
        return None
    words = text.split()
    if len(words) < 6:
        return None
    start = min(2, len(words) - 6)
    snippet = " ".join(words[start : start + 6])
    if "%" in snippet or "_" in snippet:
        return None
    return snippet


def find_matching_atoms(brain_conn: sqlite3.Connection, correction_text: str) -> list[sqlite3.Row]:
    """Find unsuperseded atoms whose text shares a meaningful keyword overlap
    with the correction. Cheap text-LIKE for now; embedding match would be
    more accurate but requires the Qdrant client and adds latency.
    """
    snippet = _snippet_for_like(correction_text)
    if snippet is None:
        return []
    cursor = brain_conn.execute(
        """
        SELECT id, text, trust_score
          FROM atoms
         WHERE text LIKE ?
           AND superseded_by IS NULL
           AND tier != 'obsolete'
        LIMIT 5
        """,
        (f"%{snippet}%",),
    )
    return cursor.fetchall()


def find_wrong_matching_atoms(brain_conn: sqlite3.Connection, wrong_text: str) -> list[sqlite3.Row]:
    """Find atoms that paraphrase the wrong claim Chris just overrode.

    Same text-LIKE strategy as find_matching_atoms but targets the
    brain_recommendation (the wrong claim) so the caller can demote
    rather than boost. Returns up to 3 to limit blast radius — accidental
    demotions of unrelated atoms are harder to recover from than
    accidental boosts.
    """
    snippet = _snippet_for_like(wrong_text)
    if snippet is None:
        return []
    cursor = brain_conn.execute(
        """
        SELECT id, text, trust_score
          FROM atoms
         WHERE text LIKE ?
           AND superseded_by IS NULL
           AND tier != 'obsolete'
        LIMIT 3
        """,
        (f"%{snippet}%",),
    )
    return cursor.fetchall()


def _ensure_deboost_table(conn: sqlite3.Connection) -> None:
    """Mirror atom_deboost._ensure_table without importing it (avoid cycle)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS atom_deboost (
            atom_id TEXT PRIMARY KEY,
            weight REAL NOT NULL DEFAULT 1.0,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )


def _already_demoted(conn: sqlite3.Connection, atom_id: str) -> bool:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='atom_deboost'").fetchone():
        return False
    row = conn.execute(
        "SELECT weight FROM atom_deboost WHERE atom_id = ?",
        (atom_id,),
    ).fetchone()
    return bool(row and float(row["weight"]) <= DEBOOST_WEIGHT_DEMOTE)


def run(dry_run: bool = False, demote_only_backfill: bool = False) -> dict:
    """When ``demote_only_backfill=True``, skip the ledger gate and re-walk
    every override outcome — but only execute the wrong-atom demotion path
    (no boost). Lets the new demote logic land on the 305 historical
    chris_override outcomes that the old boost-only code already ledger-
    marked. Boosts are skipped to keep idempotency on trust_score (which
    is capped at 1.0; re-running boost would no-op anyway, but skipping it
    makes the intent explicit and the audit trail cleaner)."""
    stats = {
        "outcomes_in_window": 0,
        "outcomes_skipped_already_processed": 0,
        "outcomes_newly_processed": 0,
        "atoms_boosted": 0,
        "boosts_total": 0.0,
        "atoms_demoted": 0,
        "demotes_total": 0.0,
        "deboost_writes": 0,
        "no_match": 0,
        "dry_run": dry_run,
    }

    if not BRAIN_DB.exists() or not AUTONOMY_DB.exists():
        stats["error"] = "missing db"
        return stats

    ledger = _load_ledger()
    stats["ledger_size_before"] = len(ledger)

    auto = _connect(AUTONOMY_DB)
    brain = _connect(BRAIN_DB)
    try:
        rows = auto.execute(
            """
            SELECT id, task_id, domain, brain_recommendation, actual_action
              FROM outcomes
             WHERE chris_override = 1
               AND created_at > datetime('now', ?)
            ORDER BY created_at DESC
            """,
            (f"-{LOOKBACK_DAYS} days",),
        ).fetchall()

        boost_updates: list[tuple[float, str]] = []
        demote_updates: list[tuple[float, str]] = []
        deboost_writes: list[tuple[str, float, str, str, str]] = []
        from datetime import UTC, datetime

        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

        for row in rows:
            stats["outcomes_in_window"] += 1
            if row["id"] in ledger and not demote_only_backfill:
                stats["outcomes_skipped_already_processed"] += 1
                continue

            atoms = [] if demote_only_backfill else find_matching_atoms(brain, row["actual_action"])
            wrong_atoms = find_wrong_matching_atoms(brain, row["brain_recommendation"])

            if not atoms and not wrong_atoms:
                stats["no_match"] += 1
                if not dry_run:
                    ledger.add(row["id"])
                continue

            # Boost atoms that semantically match Chris's correction
            for atom in atoms:
                old = float(atom["trust_score"] or 0.5)
                new = min(TRUST_CAP, old + TRUST_DELTA)
                if new <= old:
                    continue
                stats["atoms_boosted"] += 1
                stats["boosts_total"] += new - old
                boost_updates.append((new, atom["id"]))

            # Demote atoms that paraphrase the wrong claim. Skip any atom
            # that ALSO matched the correction text (rare but possible when
            # an atom contains both restatement and contradiction) so a
            # single ambiguous atom can't be both boosted and demoted in
            # the same pass.
            correction_ids = {a["id"] for a in atoms}
            for atom in wrong_atoms:
                if atom["id"] in correction_ids:
                    continue
                if _already_demoted(brain, atom["id"]):
                    continue
                old = float(atom["trust_score"] or 0.5)
                new = max(TRUST_FLOOR, old - TRUST_DEMOTE)
                if new >= old:
                    continue
                stats["atoms_demoted"] += 1
                stats["demotes_total"] += old - new
                demote_updates.append((new, atom["id"]))
                # Also write a deboost weight so the recall path picks it up
                # without waiting for the nightly atom_deboost recompute.
                evidence = json.dumps(
                    {
                        "source": "auto_supersede_overrides",
                        "outcome_id": row["id"],
                        "domain": row["domain"],
                    }
                )
                reason = f"chris_override demotion ({row['domain']})"
                deboost_writes.append((atom["id"], DEBOOST_WEIGHT_DEMOTE, evidence, reason, now_iso))

            stats["outcomes_newly_processed"] += 1
            # Skip ledger update in backfill mode so the next regular run
            # still sees these outcomes as already-processed for the boost
            # path. The demote pass we just executed is the only thing
            # backfill changes about the historical record.
            if not dry_run and not demote_only_backfill:
                ledger.add(row["id"])

        if (boost_updates or demote_updates or deboost_writes) and not dry_run:
            with retrying_transaction(brain):
                if boost_updates:
                    brain.executemany(
                        "UPDATE atoms SET trust_score = ?, updated_at = datetime('now') WHERE id = ?",
                        boost_updates,
                    )
                if demote_updates:
                    brain.executemany(
                        "UPDATE atoms SET trust_score = ?, updated_at = datetime('now') WHERE id = ?",
                        demote_updates,
                    )
                if deboost_writes:
                    _ensure_deboost_table(brain)
                    brain.executemany(
                        """
                        INSERT INTO atom_deboost (atom_id, weight, evidence_json, reason, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(atom_id) DO UPDATE SET
                            weight = MIN(atom_deboost.weight, excluded.weight),
                            evidence_json = excluded.evidence_json,
                            reason = excluded.reason,
                            updated_at = excluded.updated_at
                        """,
                        deboost_writes,
                    )
                    stats["deboost_writes"] = len(deboost_writes)

        if not dry_run:
            _save_ledger(ledger)
        stats["ledger_size_after"] = len(ledger)
    finally:
        brain.close()
        auto.close()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--demote-only-backfill",
        action="store_true",
        help="Re-walk all override outcomes and run ONLY the wrong-atom "
        "demote path, ignoring the ledger. Leaves the ledger untouched.",
    )
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, demote_only_backfill=args.demote_only_backfill)
    import json

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
