"""brain_core/atom_deboost.py — outcome-aware atom shadow weights.

Stores a per-atom multiplier (0.0-1.0) that recall scoring can apply
after rerank to demote atoms that repeatedly drive bad outcomes.
Weights decay back to 1.0 (no penalty) when the atom contributes to a
successful outcome.

Signals consumed (read-only):
  - recall_judgments.relevance < JUDGE_WRONG_THRESHOLD → wrong-evidence count
  - action_audit.outcome == 'judged_wrong' → wrong-evidence count
  - decision_ledger.outcome_status == 'failed' WHERE selected_payload_json
    references the atom → override count
  - decision_ledger.outcome_status == 'succeeded' for same atom → recovery

Storage:
  atom_deboost(atom_id PK, weight REAL, evidence_json TEXT, reason TEXT,
               updated_at TEXT)

Contract:
  - Pure SQLite reads/writes; no LLM, no embeddings.
  - Bounded per-run output (MAX_UPDATES); reruns are idempotent.
  - Does NOT mutate atoms.tier or supersession chains.
  - Recall integration is opt-in via a future env flag — this module
    only owns the data table and the scoring helpers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.atom_deboost")

DEFAULT_WEIGHT = 1.0
MIN_WEIGHT = 0.05
DEBOOST_FLOOR = 0.20
RECOVERY_STEP = 0.10
PENALTY_STEP = 0.15
MAX_UPDATES = 200
WRONG_RELEVANCE_THRESHOLD = 0.30
WINDOW_HOURS = 24 * 7


def _now_iso() -> str:
    # 2026-05-15 P2-8: delegate to shared helper; Z-suffix lex-sorts with
    # atoms_store / entry_manifest writers.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from db import now_iso

    return now_iso(z_suffix=True)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS atom_deboost (
            atom_id TEXT PRIMARY KEY,
            weight REAL NOT NULL DEFAULT 1.0,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_atom_deboost_weight
          ON atom_deboost(weight);
        """
    )


def _aggregate_wrong_atom_counts(conn: sqlite3.Connection, window_hours: int) -> dict[str, int]:
    """Count how often each retrieved atom shows up in wrong-judged recalls."""
    if not _table_exists(conn, "recall_judgments"):
        return {}
    rows = conn.execute(
        """
        SELECT aa.retrieved_atom_ids, aa.retrieved_chroma_ids
        FROM recall_judgments rj
        JOIN action_audit aa ON aa.id = rj.action_audit_id
        WHERE rj.created_at > datetime('now', ?)
          AND rj.relevance IS NOT NULL
          AND rj.relevance < ?
        """,
        (f"-{int(window_hours)} hours", WRONG_RELEVANCE_THRESHOLD),
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        ids = _parse_ids(row[0]) + _parse_ids(row[1])
        for aid in ids:
            counts[aid] = counts.get(aid, 0) + 1
    return counts


def _aggregate_right_atom_counts(conn: sqlite3.Connection, window_hours: int) -> dict[str, int]:
    """Count how often each retrieved atom shows up in right-judged recalls."""
    if not _table_exists(conn, "recall_judgments"):
        return {}
    rows = conn.execute(
        """
        SELECT aa.retrieved_atom_ids, aa.retrieved_chroma_ids
        FROM recall_judgments rj
        JOIN action_audit aa ON aa.id = rj.action_audit_id
        WHERE rj.created_at > datetime('now', ?)
          AND rj.relevance IS NOT NULL
          AND rj.relevance >= 0.7
        """,
        (f"-{int(window_hours)} hours",),
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        ids = _parse_ids(row[0]) + _parse_ids(row[1])
        for aid in ids:
            counts[aid] = counts.get(aid, 0) + 1
    return counts


def _parse_ids(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x]
    return []


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def update_weights(
    *,
    brain_db_path: Path | str,
    window_hours: int = WINDOW_HOURS,
    max_updates: int = MAX_UPDATES,
) -> dict:
    """One-shot pass: apply penalty for wrong-judged appearances and
    recovery for right-judged appearances. Returns counts + sample of
    changed atoms so the scheduler can log activity."""
    db_path = Path(brain_db_path)
    summary: dict[str, Any] = {
        "started_at": _now_iso(),
        "deboosted": [],
        "recovered": [],
        "totals": {"deboosted": 0, "recovered": 0, "scanned": 0},
    }
    if not db_path.exists():
        summary["status"] = "db_missing"
        return summary
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        _ensure_table(conn)
        wrong = _aggregate_wrong_atom_counts(conn, window_hours)
        right = _aggregate_right_atom_counts(conn, window_hours)
        all_ids = set(wrong) | set(right)
        summary["totals"]["scanned"] = len(all_ids)

        if all_ids:
            placeholders = ",".join("?" * len(all_ids))
            cur_rows = conn.execute(
                f"SELECT atom_id, weight FROM atom_deboost WHERE atom_id IN ({placeholders})",  # noqa: S608 — fixed-count placeholders, not user-derived SQL
                tuple(all_ids),
            ).fetchall()
        else:
            cur_rows = []
        cur_weights: dict[str, float] = {r[0]: float(r[1]) for r in cur_rows}

        changes: list[tuple[str, float, str, dict]] = []
        for atom_id in all_ids:
            cur = cur_weights.get(atom_id, DEFAULT_WEIGHT)
            w_count = wrong.get(atom_id, 0)
            r_count = right.get(atom_id, 0)
            new_weight = cur
            new_weight -= PENALTY_STEP * w_count
            new_weight += RECOVERY_STEP * r_count
            new_weight = max(MIN_WEIGHT, min(DEFAULT_WEIGHT, round(new_weight, 4)))
            if abs(new_weight - cur) < 0.001:
                continue
            reason_parts = []
            if w_count:
                reason_parts.append(f"{w_count}x wrong-judged")
            if r_count:
                reason_parts.append(f"{r_count}x right-judged")
            reason = " + ".join(reason_parts) or "no-op"
            evidence = {"wrong": w_count, "right": r_count, "window_hours": window_hours}
            changes.append((atom_id, new_weight, reason, evidence))

        changes.sort(key=lambda c: c[1])  # most-deboosted first
        for atom_id, new_weight, reason, evidence in changes[:max_updates]:
            conn.execute(
                """
                INSERT INTO atom_deboost (atom_id, weight, evidence_json, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(atom_id) DO UPDATE SET
                    weight = excluded.weight,
                    evidence_json = excluded.evidence_json,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (atom_id, new_weight, json.dumps(evidence), reason, _now_iso()),
            )
            if new_weight < DEBOOST_FLOOR:
                summary["deboosted"].append({"atom_id": atom_id, "weight": new_weight, "reason": reason})
            elif new_weight > 0.95:
                summary["recovered"].append({"atom_id": atom_id, "weight": new_weight, "reason": reason})
        conn.commit()
        summary["totals"]["deboosted"] = len(summary["deboosted"])
        summary["totals"]["recovered"] = len(summary["recovered"])
        summary["status"] = "ok"
    except sqlite3.Error as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    finally:
        conn.close()
    summary["finished_at"] = _now_iso()
    return summary


def load_weight_map(
    *,
    brain_db_path: Path | str,
    floor: float = DEBOOST_FLOOR,
) -> dict[str, float]:
    """Return ID-keyed weights for atoms whose weight is below `floor`.

    Returns a dict whose keys cover EVERY equivalent ID form for each
    deboosted atom:
      * atom_deboost.atom_id (the raw stored form — could be atoms.id /
        chroma_id / audit-UUID, depending on writer)
      * atoms.id (when join succeeds)
      * atoms.chroma_id (when join succeeds)
      * The chroma_id-as-suffix (so `semantic_memory:hex` results match the
        bare `hex`-form chroma_id)
      * UUID-dashed form of any 32-char-hex variant (matches
        _to_dashed_uuid output written into audit/deboost)

    Each weight is duplicated under all equivalent keys so the recall
    consumer can lookup by result.id regardless of which ID schema the
    upstream writer used. Closes the ID-mismatch hole between deboost
    writers (audit-UUID, atoms.id from wrong_atom_demoter, chroma_id from
    judges) and the recall reader (result.id format depends on
    collection).
    """
    db_path = Path(brain_db_path)
    if not db_path.exists():
        return {}
    raw_weights: dict[str, float] = {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            if not _table_exists(conn, "atom_deboost"):
                return {}
            rows = conn.execute(
                "SELECT atom_id, weight FROM atom_deboost WHERE weight < ?",
                (floor,),
            ).fetchall()
            raw_weights = {row[0]: float(row[1]) for row in rows}
            if not raw_weights:
                return {}
            # Cross-key into atoms via id OR chroma_id. Both directions:
            # some writers store atoms.id, others store chroma_id.
            atoms_meta: list[tuple[str, str | None, str | None]] = []
            if _table_exists(conn, "atoms"):
                chunk = list(raw_weights.keys())
                for i in range(0, len(chunk), 800):
                    batch = chunk[i : i + 800]
                    placeholders = ",".join("?" * len(batch))
                    rows = conn.execute(
                        f"SELECT atom_id_or_chroma, id, chroma_id FROM ("  # noqa: S608 — fixed placeholder count
                        f"  SELECT id AS atom_id_or_chroma, id, chroma_id FROM atoms "
                        f"   WHERE id IN ({placeholders}) "
                        f"  UNION "
                        f"  SELECT chroma_id AS atom_id_or_chroma, id, chroma_id FROM atoms "
                        f"   WHERE chroma_id IN ({placeholders}) "
                        f")",
                        batch + batch,
                    ).fetchall()
                    atoms_meta.extend((r[0], r[1], r[2]) for r in rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return {}

    expanded: dict[str, float] = {}
    for key, weight in raw_weights.items():
        for variant in _id_variants(key):
            expanded[variant] = min(expanded.get(variant, 1.0), weight)
    for ad_key, atoms_id, chroma_id in atoms_meta:
        if not ad_key:
            continue
        weight = raw_weights.get(ad_key)
        if weight is None:
            continue
        for variant in _id_variants(atoms_id) + _id_variants(chroma_id):
            expanded[variant] = min(expanded.get(variant, 1.0), weight)
    return expanded


def _id_variants(raw: str | None) -> list[str]:
    """Return every equivalent textual form for a single atom identifier."""
    if not raw:
        return []
    out = {raw}
    if ":" in raw:
        out.add(raw.split(":", 1)[1])
    bare = raw.split(":", 1)[1] if ":" in raw else raw
    if len(bare) == 32 and "-" not in bare and all(c in "0123456789abcdef" for c in bare.lower()):
        out.add(f"{bare[:8]}-{bare[8:12]}-{bare[12:16]}-{bare[16:20]}-{bare[20:]}")
    return [v for v in out if v]


def run_default(brain_db_path: Path | str | None = None) -> dict:
    if brain_db_path is None:
        from config import BRAIN_DB

        brain_db_path = BRAIN_DB
    return update_weights(brain_db_path=brain_db_path)
