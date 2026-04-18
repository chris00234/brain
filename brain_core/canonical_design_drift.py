"""brain_core/canonical_design_drift.py — weekly drift detector.

Compares ~/design-standard/DESIGN.md (source of truth) against
~/server/knowledge/canonical/design/personal_standard.md (canonical mirror)
every Sunday. If the SHAs diverge, dispatch a Telegram alert via Jenna so
Chris knows his design source and canonical copy have drifted.

Why: today's root-cause incident (2026-04-14) was that the Personal Design
Standard wasn't surfacing. Having two copies makes it easy for one to drift
out of sync silently. This cron catches drift within a week.

Trigger: scheduler job `canonical_design_drift` (weekly Sun 05:30).
Consumer: Jenna Telegram on drift.
Effect: Chris is notified, can investigate + reconcile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.canonical_design_drift")

SOURCE_PATH = Path.home() / "design-standard" / "DESIGN.md"
MIRROR_PATH = Path.home() / "server" / "knowledge" / "canonical" / "design" / "personal_standard.md"


def _sha(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def run() -> dict:
    """Check source vs canonical mirror. Return drift metadata."""
    src_sha = _sha(SOURCE_PATH)
    mir_sha = _sha(MIRROR_PATH)

    result = {
        "source_path": str(SOURCE_PATH),
        "mirror_path": str(MIRROR_PATH),
        "source_exists": src_sha is not None,
        "mirror_exists": mir_sha is not None,
        "source_sha": src_sha,
        "mirror_sha": mir_sha,
        "drift": False,
    }

    if src_sha is None and mir_sha is None:
        result["status"] = "both_missing"
        return result
    if src_sha is None:
        result["drift"] = True
        result["status"] = "source_missing"
    elif mir_sha is None:
        result["drift"] = True
        result["status"] = "mirror_missing"
    elif src_sha != mir_sha:
        result["drift"] = True
        result["status"] = "hash_diverged"
    else:
        result["status"] = "in_sync"
        return result

    # Drift detected — notify via Jenna Telegram (best-effort)
    try:
        from cli_llm import dispatch

        body = (
            "⚠ Canonical design standard drift detected\n\n"
            f"status: {result['status']}\n"
            f"source: {SOURCE_PATH} ({(src_sha or 'missing')[:12]})\n"
            f"mirror: {MIRROR_PATH} ({(mir_sha or 'missing')[:12]})\n\n"
            "Action: reconcile the two copies before the next frontend work. "
            "Source is authoritative — the canonical mirror should be regenerated "
            "from it if they differ."
        )
        dispatch(
            agent="jenna",
            message=f"[canonical_design_drift]\n{body}",
            thinking="low",
            timeout=60,
            degraded_placeholder="[drift alert dispatch failed]",
        )
    except Exception as e:
        log.warning("drift alert dispatch failed: %s", e)

    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
