"""brain_core/backup_retention.py — bounded retention for backup directories.

`brain.db` and `autonomy.db` have explicit retention windows (4d) but
`logs/backups/docker-volumes/` accumulates daily ghost/uptime-kuma/
couchdb tarballs with no cleanup — that's where 296 MB of the logs_dir
budget was sitting on 2026-05-13.

The retention pass keeps the latest N daily snapshots per logical
backup family (matched by stem prefix) and deletes the rest. Family
parsing assumes filenames look like `<family>-YYYYMMDD.<ext>` or
`<family>-YYYYMMDDTHHMMSS<...>`. Files that don't match any pattern
are left alone so we never delete something we don't understand.
"""

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.backup_retention")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_LOGS_DIR
except ImportError:  # pragma: no cover - direct execution fallback
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")


DEFAULT_KEEP_DAILY = 7  # keep the most recent N daily snapshots per family


_FAMILY_FROM_NAME = re.compile(
    r"^(?P<family>[a-zA-Z0-9_-]+?)-(?P<stamp>[0-9]{8}(?:T[0-9]{6}Z?)?)(?P<rest>.*)$"
)


def run_backup_retention(
    *,
    target_dir: Path | str | None = None,
    keep_per_family: int = DEFAULT_KEEP_DAILY,
    dry_run: bool = False,
) -> dict:
    """Trim each backup family in `target_dir` to its newest N files.

    Returns a summary `{started_at, target, kept, deleted, families}`.
    """
    summary: dict = {
        "started_at": _now_iso(),
        "target": str(target_dir or (BRAIN_LOGS_DIR / "backups" / "docker-volumes")),
        "kept": [],
        "deleted": [],
        "families": {},
        "dry_run": dry_run,
    }
    target = Path(summary["target"])
    if not target.exists() or not target.is_dir():
        summary["status"] = "missing"
        summary["finished_at"] = _now_iso()
        return summary
    keep = max(1, int(keep_per_family or DEFAULT_KEEP_DAILY))

    families: dict[str, list[Path]] = defaultdict(list)
    for entry in target.iterdir():
        if not entry.is_file():
            continue
        match = _FAMILY_FROM_NAME.match(entry.name)
        if not match:
            continue
        families[match.group("family")].append(entry)

    for family, paths in families.items():
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        kept = paths[:keep]
        delete = paths[keep:]
        summary["families"][family] = {
            "total": len(paths),
            "kept": len(kept),
            "deleted": len(delete),
        }
        for path in kept:
            summary["kept"].append(str(path.name))
        for path in delete:
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("backup_retention failed to delete %s: %s", path, exc)
                    continue
            summary["deleted"].append(str(path.name))
    summary["status"] = "ok"
    summary["finished_at"] = _now_iso()
    return summary


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    p.add_argument("--target", default=None)
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP_DAILY)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(
        json.dumps(
            run_backup_retention(
                target_dir=args.target,
                keep_per_family=args.keep,
                dry_run=args.dry_run,
            ),
            indent=2,
            default=str,
        )
    )
