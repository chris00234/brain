"""brain_core/autonomy_proposer.py - Phase 7 autonomy auto-tune.

Reads `accuracy_tracker` from autonomy.db. For each action_kind with sufficient
sample size and high correct_ratio, propose an autonomy level upgrade
(L2 -> L3) by appending an event to audit_log for human review.

NEVER auto-applies. Always surfaces as an audit review item.

Mirror logic: if Chris overrides recent outcomes with reject, propose a
downgrade (L3 -> L2 or L2 -> L1) and tick the breaker for that kind.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.autonomy_proposer")

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


MIN_OUTCOMES_FOR_PROMOTE = 20
PROMOTE_RATIO = 0.95
DEMOTE_RATIO = 0.60


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _propose_audit(kind: str, current_level: str, target_level: str, reason: str) -> None:
    try:
        from audit_log import log_event

        log_event(
            event_type="autonomy_proposal",
            entity_a=kind,
            entity_b=f"{current_level}->{target_level}",
            resolution="proposed",
            reason=reason,
            review_required=True,
        )
    except Exception as exc:
        log.warning("audit log write failed: %s", exc)


def run() -> dict:
    """Walk accuracy_tracker, propose level changes, write audit_log entries.

    Returns a dict summary for the scheduler.
    """
    try:
        from autonomy import list_levels
    except Exception:
        list_levels = lambda: {}  # noqa: E731

    levels = list_levels()
    promotes: list[dict] = []
    demotes: list[dict] = []
    skipped: list[str] = []

    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT domain, total_recommendations, correct_recommendations, "
                "override_count FROM accuracy_tracker"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"error": str(e)[:200]}

    for row in rows:
        kind = row["domain"]
        total = int(row["total_recommendations"] or 0)
        correct = int(row["correct_recommendations"] or 0)
        overrides = int(row["override_count"] or 0)
        if total < MIN_OUTCOMES_FOR_PROMOTE:
            continue
        ratio = correct / total
        current_level = levels.get(kind, "L1")

        if ratio >= PROMOTE_RATIO and current_level == "L2":
            _propose_audit(
                kind,
                current_level,
                "L3",
                f"correct_ratio={ratio:.3f} ({correct}/{total}) >= {PROMOTE_RATIO}",
            )
            promotes.append({"kind": kind, "ratio": ratio, "total": total})
        elif ratio <= DEMOTE_RATIO and current_level in ("L2", "L3"):
            target = "L2" if current_level == "L3" else "L1"
            _propose_audit(
                kind,
                current_level,
                target,
                f"correct_ratio={ratio:.3f} ({correct}/{total}) <= {DEMOTE_RATIO} (overrides={overrides})",
            )
            demotes.append({"kind": kind, "ratio": ratio, "total": total, "target": target})
            # Tick the breaker so subsequent actions get short-circuited
            try:
                from breakers import record_result

                record_result(kind, ok=False, error="autonomy_proposer:low_accuracy")
            except Exception as exc:
                log.warning("breaker tick failed for %s: %s", kind, exc)
        else:
            skipped.append(kind)

    return {
        "promoted_proposals": len(promotes),
        "demoted_proposals": len(demotes),
        "skipped": len(skipped),
        "promotes": promotes,
        "demotes": demotes,
    }


if __name__ == "__main__":
    import json
    import sys as _sys

    _sys.stdout.write(json.dumps(run(), indent=2) + "\n")
