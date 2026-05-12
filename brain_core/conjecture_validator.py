"""brain_core/conjecture_validator.py — close the read-side gap on dream_replay.

Biological motivation: REM-phase recombination (Wagner 2004) is only useful if
waking cognition tests the recombined hypotheses against fresh observation.
`dream_replay.py` already generates conjectures nightly, but until now no
process tested whether subsequent atoms corroborated them. 101 conjectures had
accumulated at confidence=0.3 with no path to promotion or expiry.

This validator closes the loop:
  1. For each conjecture, extract (entity_a, entity_b) from provenance.
  2. Find atoms written AFTER the conjecture's last_validation_at that mention
     both entities (case-insensitive substring on atoms.text). Self-excluded.
  3. Record each as an atom_evidence row (event_type='conjecture_support').
  4. Bump confidence by +0.05 per new supporter (capped at 0.95).
  5. Once confidence crosses 0.5, promote tier 'episodic' -> 'semantic' so the
     conjecture becomes regularly retrievable (no longer dream-tier downweight).
  6. After 21 days with zero supporters, expire tier -> 'obsolete'.

Idempotent: last_validation_at is stored in provenance_json so re-runs skip
atoms already counted.

Non-destructive: nothing is hard-deleted. Expired conjectures move to
tier='obsolete' (still auditable, just absent from default retrieval).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

log = logging.getLogger("brain.conjecture_validator")

AUDIT_LOG = BRAIN_LOGS_DIR / "conjecture_validator.jsonl"

CONFIDENCE_STEP = 0.05
CONFIDENCE_CEILING = 0.95
PROMOTION_THRESHOLD = 0.5
EXPIRY_DAYS = 21
SUPPORT_SEARCH_LIMIT = 50
MIN_ENTITY_LEN = 4


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _audit(event: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            event["at"] = _now_iso()
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("audit write failed: %s", exc)


def _fetch_conjectures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, text, confidence, tier, valid_from, provenance_json, updated_at "
        "FROM atoms "
        "WHERE kind = 'conjecture' AND tier = 'episodic'"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            prov = json.loads(r[5] or "{}")
        except Exception:
            prov = {}
        a = (prov.get("entity_a") or "").strip()
        b = (prov.get("entity_b") or "").strip()
        if len(a) < MIN_ENTITY_LEN or len(b) < MIN_ENTITY_LEN:
            continue
        if prov.get("origin") != "dream_replay":
            continue
        out.append(
            {
                "id": r[0],
                "text": r[1],
                "confidence": float(r[2] or 0.0),
                "tier": r[3],
                "valid_from": r[4],
                "provenance": prov,
                "updated_at": r[6],
                "entity_a": a,
                "entity_b": b,
                "last_validation_at": prov.get("last_validation_at") or r[4],
            }
        )
    return out


def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters so entity names with % or _ don't over-match.

    D1-D10 review fix: an entity name like '100%' would otherwise match
    every atom. Escape \\, %, _ and reference the escape in the LIKE clause.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_supporters(
    conn: sqlite3.Connection,
    conjecture_id: str,
    entity_a: str,
    entity_b: str,
    since_iso: str,
) -> list[str]:
    """Return atom ids that mention BOTH entities and were created after since_iso.

    Excludes:
      - The conjecture itself
      - Other conjecture atoms (we want real evidence, not cross-dream chains)
      - Obsolete-tier atoms (already retired)
    """
    pat_a = f"%{_escape_like(entity_a)}%"
    pat_b = f"%{_escape_like(entity_b)}%"
    rows = conn.execute(
        "SELECT id FROM atoms "
        "WHERE id != ? "
        "  AND kind != 'conjecture' "
        "  AND tier != 'obsolete' "
        "  AND valid_from > ? "
        "  AND text LIKE ? ESCAPE '\\' COLLATE NOCASE "
        "  AND text LIKE ? ESCAPE '\\' COLLATE NOCASE "
        "ORDER BY valid_from ASC "
        "LIMIT ?",
        (conjecture_id, since_iso, pat_a, pat_b, SUPPORT_SEARCH_LIMIT),
    ).fetchall()
    return [r[0] for r in rows]


def _already_recorded(conn: sqlite3.Connection, conjecture_id: str, supporter_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM atom_evidence "
        "WHERE atom_id = ? AND event_type = 'conjecture_support' AND evidence_ref = ? "
        "LIMIT 1",
        (conjecture_id, supporter_id),
    ).fetchone()
    return row is not None


def _record_support(conn: sqlite3.Connection, conjecture_id: str, supporter_ids: list[str]) -> int:
    added = 0
    now = _now_iso()
    for sid in supporter_ids:
        if _already_recorded(conn, conjecture_id, sid):
            continue
        conn.execute(
            "INSERT INTO atom_evidence (atom_id, event_type, weight, evidence_ref, cluster_size, created_at) "
            "VALUES (?, 'conjecture_support', 0.5, ?, 1, ?)",
            (conjecture_id, sid, now),
        )
        added += 1
    return added


def _support_total(conn: sqlite3.Connection, conjecture_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM atom_evidence " "WHERE atom_id = ? AND event_type = 'conjecture_support'",
        (conjecture_id,),
    ).fetchone()
    return int(row[0] or 0)


def _update_conjecture(
    conn: sqlite3.Connection,
    conjecture_id: str,
    *,
    new_confidence: float | None = None,
    new_tier: str | None = None,
    provenance_patch: dict | None = None,
) -> None:
    row = conn.execute(
        "SELECT confidence, tier, provenance_json FROM atoms WHERE id = ?",
        (conjecture_id,),
    ).fetchone()
    if not row:
        return
    confidence = float(row[0] or 0.0)
    tier = row[1]
    try:
        prov = json.loads(row[2] or "{}")
    except Exception:
        prov = {}
    if new_confidence is not None:
        confidence = round(max(0.0, min(CONFIDENCE_CEILING, new_confidence)), 4)
    if new_tier is not None:
        tier = new_tier
    if provenance_patch:
        prov.update(provenance_patch)
    conn.execute(
        "UPDATE atoms SET confidence = ?, tier = ?, provenance_json = ?, updated_at = ? " "WHERE id = ?",
        (confidence, tier, json.dumps(prov, ensure_ascii=False), _now_iso(), conjecture_id),
    )


def _days_old(iso_ts: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    return (datetime.now(UTC) - ts).total_seconds() / 86400.0


def run() -> dict:
    if not BRAIN_DB.exists():
        return {"status": "skip", "reason": "no_brain_db"}

    conn = sqlite3.connect(str(BRAIN_DB), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conjectures = _fetch_conjectures(conn)
        if not conjectures:
            return {
                "status": "ok",
                "scanned": 0,
                "new_supports": 0,
                "promoted_count": 0,
                "expired_count": 0,
                "progressed_count": 0,
                "promoted": [],
                "expired": [],
            }

        scanned = 0
        new_supports = 0
        promoted: list[dict] = []
        expired: list[dict] = []
        progressed: list[dict] = []
        now_iso = _now_iso()

        for c in conjectures:
            scanned += 1
            supporters = _find_supporters(
                conn, c["id"], c["entity_a"], c["entity_b"], c["last_validation_at"]
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                added = _record_support(conn, c["id"], supporters)
                new_supports += added
                total_support = _support_total(conn, c["id"])
                age_days = _days_old(c["valid_from"])

                if added > 0:
                    new_conf = c["confidence"] + (CONFIDENCE_STEP * added)
                    new_tier = c["tier"]
                    if new_conf >= PROMOTION_THRESHOLD and c["tier"] == "episodic":
                        new_tier = "semantic"
                        promoted.append(
                            {
                                "id": c["id"],
                                "from_confidence": c["confidence"],
                                "to_confidence": min(CONFIDENCE_CEILING, new_conf),
                                "supporters_added": added,
                                "supporters_total": total_support,
                            }
                        )
                    else:
                        progressed.append(
                            {
                                "id": c["id"],
                                "supporters_added": added,
                                "supporters_total": total_support,
                                "new_confidence": min(CONFIDENCE_CEILING, new_conf),
                            }
                        )
                    _update_conjecture(
                        conn,
                        c["id"],
                        new_confidence=new_conf,
                        new_tier=new_tier,
                        provenance_patch={
                            "last_validation_at": now_iso,
                            "last_supporters": supporters[-10:],
                        },
                    )
                elif total_support == 0 and age_days >= EXPIRY_DAYS:
                    _update_conjecture(
                        conn,
                        c["id"],
                        new_tier="obsolete",
                        provenance_patch={
                            "expired_at": now_iso,
                            "expire_reason": f"no_evidence_in_{EXPIRY_DAYS}d",
                            "last_validation_at": now_iso,
                        },
                    )
                    expired.append({"id": c["id"], "age_days": round(age_days, 1)})
                else:
                    _update_conjecture(
                        conn,
                        c["id"],
                        provenance_patch={"last_validation_at": now_iso},
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        summary = {
            "status": "ok",
            "scanned": scanned,
            "new_supports": new_supports,
            "promoted_count": len(promoted),
            "expired_count": len(expired),
            "progressed_count": len(progressed),
            "promoted": promoted,
            "expired": expired,
        }
        _audit(
            {
                "event": "run",
                "summary": {
                    k: summary[k]
                    for k in (
                        "scanned",
                        "new_supports",
                        "promoted_count",
                        "expired_count",
                        "progressed_count",
                    )
                },
            }
        )
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))  # noqa: T201
