#!/Users/chrischo/server/brain/.venv/bin/python
"""pipeline/re_examine_rejected.py — monthly rejected-proposal re-examiner.

2026-04-16 Tier 2 fix for the "rejections are permanent" anti-pattern.

score_proposals.py:141 excludes REJECTED_DIR from every scoring pass, and
nothing else ever looks back at rejected notes. If a proposal was rejected
at a time when only one source cited it (score < 42), but later evidence
arrives corroborating the same claim, the proposal stays buried forever.

This job runs monthly:
  1. Walk every rejected proposal markdown.
  2. For each, search the brain for content similar to its title + leading
     paragraph (in-process search_unified, no LLM).
  3. If ≥MIN_NEW_CORROBORATIONS new high-trust hits appear AND the
     proposal's age exceeds MIN_AGE_DAYS (guards against re-running the
     scorer on a just-rejected note), move it back to the review queue
     (pending/) with a `reexamined_at` timestamp so score_proposals picks
     it up on its next cycle.

The re-examine never auto-promotes. It only restores the proposal to
human review.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, iter_note_paths, parse_markdown_frontmatter, write_markdown_frontmatter

REVIEW_QUEUE = ROOT / "reports" / "review-queue"
REJECTED_DIR = REVIEW_QUEUE / "rejected"
PENDING_DIR = REVIEW_QUEUE / "pending"

MIN_NEW_CORROBORATIONS = 3
MIN_AGE_DAYS = 30
MIN_NEW_TRUST_TIER = 2  # only count corroborations at distilled+ tier


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _search_corroborating(title: str, body_head: str, since: datetime) -> int:
    """Count new high-trust corroborations found since the proposal was rejected."""
    try:
        sys.path.insert(0, str(Path("/Users/chrischo/server/brain/brain_core")))
        import search_unified

        query = (title or "")[:100] + " " + (body_head or "")[:300]
        payload = search_unified.search_all(query, limit=20)
        results = payload.get("results", [])
    except Exception:
        return 0

    count = 0
    for r in results:
        try:
            trust_tier = r.get("trust_tier", 0)
            if isinstance(trust_tier, str):
                trust_tier = int(trust_tier) if trust_tier.isdigit() else 0
            if trust_tier < MIN_NEW_TRUST_TIER:
                continue
            r_created = r.get("created_at") or (r.get("metadata") or {}).get("created_at")
            dt = _parse_dt(r_created)
            if dt and dt > since:
                count += 1
        except Exception:
            continue
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-examine rejected proposals for fresh corroboration")
    parser.add_argument("--rejected-dir", type=Path, default=REJECTED_DIR)
    parser.add_argument("--pending-dir", type=Path, default=PENDING_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.rejected_dir.exists():
        print(json.dumps({"status": "skip", "reason": "no rejected dir"}))
        return 0

    args.pending_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    restored: list[str] = []
    kept: list[str] = []
    for path in iter_note_paths(args.rejected_dir):
        try:
            metadata, body = parse_markdown_frontmatter(path)
        except Exception:
            continue
        rejected_at = _parse_dt(metadata.get("rejected_at") or metadata.get("updated_at"))
        if rejected_at and (now - rejected_at) < timedelta(days=MIN_AGE_DAYS):
            kept.append(metadata.get("id", path.stem))
            continue
        since = rejected_at or (now - timedelta(days=90))
        new_hits = _search_corroborating(
            metadata.get("title", ""),
            body[:400],
            since,
        )
        if new_hits < MIN_NEW_CORROBORATIONS:
            kept.append(metadata.get("id", path.stem))
            continue
        # Promote back to pending with a reexamined_at marker
        metadata["review_state"] = "proposed"
        metadata["reexamined_at"] = now.isoformat(timespec="seconds")
        metadata["new_corroborations_on_reexam"] = new_hits
        if args.dry_run:
            restored.append(metadata.get("id", path.stem))
            continue
        dest = args.pending_dir / path.name
        write_markdown_frontmatter(dest, metadata, body)
        try:
            path.unlink()
        except Exception:
            pass
        restored.append(metadata.get("id", path.stem))

    summary = {
        "status": "ok",
        "restored": len(restored),
        "restored_ids": restored,
        "kept_rejected": len(kept),
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
