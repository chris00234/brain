#!/usr/bin/env python3
"""Archive old OpenClaw agent session checkpoints.

Context: On 2026-04-17 the jenna agent session accumulated 499MB — a
103MB live jsonl + 4× ~90-100MB checkpoint files from prior compactions.
Checkpoints are historical snapshots OpenClaw wrote during auto-compaction;
it doesn't actively read them, so they accumulate indefinitely.

This job:
  1. Finds `<sessionId>.checkpoint.<uuid>.jsonl` files older than
     CHECKPOINT_KEEP_DAYS in every agent's sessions dir.
  2. Moves them to `<sessions>/archive/<YYYY-MM>/`.
  3. Gzip-compresses on move to reclaim disk.
  4. Reports live session sizes — alerts if any single live jsonl
     exceeds LIVE_SESSION_ALERT_MB (operator rotation required).

Conservative by design:
  - Never touches the live session jsonl (conversational continuity
    matters; OpenClaw owns rotation of the active session).
  - Never edits sessions.json — archived checkpoints aren't referenced
    in the index, so removing them is safe.
  - Dry-run mode via --dry-run for verification before the first cron
    run.

Scheduled: Sunday 04:30 PDT via scheduler.py (weekly).
"""

from __future__ import annotations

import argparse
import gzip
import logging
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

AGENTS_ROOT = Path.home() / ".openclaw" / "agents"
CHECKPOINT_KEEP_DAYS = 14
LIVE_SESSION_ALERT_MB = 100  # alert when any live .jsonl exceeds this
DRY_RUN_DEFAULT = False


def _archive_path(agent_dir: Path, checkpoint: Path, now: datetime) -> Path:
    """<agent>/sessions/archive/<YYYY-MM>/<checkpoint>.gz"""
    month = now.strftime("%Y-%m")
    archive_dir = agent_dir / "archive" / month
    archive_dir.mkdir(parents=True, exist_ok=True)
    return archive_dir / (checkpoint.name + ".gz")


def _gzip_move(src: Path, dst: Path) -> int:
    """Compress `src` → `dst.gz` atomically, then unlink `src`. Returns
    bytes reclaimed (pre-gzip size - post-gzip size)."""
    pre = src.stat().st_size
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with src.open("rb") as f_in, gzip.open(tmp, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
    tmp.replace(dst)
    src.unlink()
    return pre - dst.stat().st_size


def rotate(dry_run: bool = DRY_RUN_DEFAULT) -> dict:
    if not AGENTS_ROOT.exists():
        return {"status": "skipped", "reason": "no agents dir"}

    now = datetime.now(UTC)
    cutoff_ts = (now - timedelta(days=CHECKPOINT_KEEP_DAYS)).timestamp()

    archived = 0
    bytes_reclaimed = 0
    per_agent: dict[str, dict] = {}
    large_live: list[tuple[str, float]] = []

    for agent_dir in sorted(AGENTS_ROOT.iterdir()):
        if not agent_dir.is_dir():
            continue
        sessions_dir = agent_dir / "sessions"
        if not sessions_dir.is_dir():
            continue

        agent_archived = 0
        agent_bytes = 0

        for cp in sessions_dir.glob("*.checkpoint.*.jsonl"):
            try:
                if cp.stat().st_mtime > cutoff_ts:
                    continue  # still fresh, keep
                dst = _archive_path(sessions_dir, cp, now)
                if dry_run:
                    log.info("[dry-run] would archive %s → %s", cp, dst)
                    agent_archived += 1
                    continue
                reclaimed = _gzip_move(cp, dst)
                agent_archived += 1
                agent_bytes += reclaimed
            except Exception as exc:
                log.warning("archive failed for %s: %s", cp, exc)

        # Live session size report
        for live in sessions_dir.glob("*.jsonl"):
            if ".checkpoint." in live.name:
                continue
            size_mb = live.stat().st_size / (1024 * 1024)
            if size_mb > LIVE_SESSION_ALERT_MB:
                large_live.append((f"{agent_dir.name}/{live.name}", round(size_mb, 1)))

        if agent_archived:
            per_agent[agent_dir.name] = {
                "checkpoints_archived": agent_archived,
                "bytes_reclaimed": agent_bytes,
            }
            archived += agent_archived
            bytes_reclaimed += agent_bytes

    result = {
        "status": "ok",
        "dry_run": dry_run,
        "checkpoints_archived": archived,
        "bytes_reclaimed_mb": round(bytes_reclaimed / (1024 * 1024), 1),
        "per_agent": per_agent,
        "large_live_sessions": large_live,
    }

    # Alert on oversized live sessions (operator must rotate manually)
    if large_live and not dry_run:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
            from telegram_alert import send_chris_telegram

            body = "\n".join(f"  {name}: {mb}MB" for name, mb in large_live)
            send_chris_telegram(
                f"[session_rotate] Live sessions over {LIVE_SESSION_ALERT_MB}MB:\n{body}\n"
                f"Rotate manually: update sessions.json to a new sessionKey "
                f"(e.g. agent:jenna:main:v2) to reset context.",
                source="session_rotate",
                severity="warn",
            )
        except Exception as exc:
            log.warning("session_rotate alert failed: %s", exc)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report actions without archiving")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    result = rotate(dry_run=args.dry_run)

    import json

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
