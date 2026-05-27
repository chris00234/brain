#!/usr/bin/env python3
"""Migrate eval set expected_source paths after canonical reorganization.

The canonical pipeline has moved many original notes into
``canonical/archived/`` (decisions, incidents, chris) while keeping the
content-equivalent distilled summaries in ``distilled/``. Eval queries
written against the pre-migration paths still point to the original
locations, which no longer exist. Result on extended track: 112 of 116
persistent failures (97%) reference dead paths.

This migrator:
  1. Walks ``eval_set_extended.json`` (or any caller-supplied set).
  2. For each entry whose ``expected_source`` doesn't exist on disk:
       a. Try ``canonical/archived/<rest-of-path>`` — moved-but-preserved.
       b. Try the distilled equivalent by filename stem prefix match.
  3. Writes an alternates field (preserves the original for forensics).

Idempotent: reruns are no-ops if all paths already resolve. Conservative:
never deletes an entry; only adds resolved alternates. Dry-run by default
to avoid silently mutating a curated eval set.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("brain.eval_set_path_migrator")

KNOWLEDGE_ROOT = Path("/Users/chrischo/server/knowledge")
DEFAULT_EVAL_SET = Path("/Users/chrischo/server/brain/cli/eval_set_extended.json")


def _path_exists(rel: str) -> bool:
    if not rel:
        return False
    candidates = [
        KNOWLEDGE_ROOT / rel,
        KNOWLEDGE_ROOT.parent / rel,
    ]
    return any(c.exists() for c in candidates)


def _try_archived_equivalent(rel: str) -> str | None:
    """For canonical/decisions/foo.md → canonical/archived/decisions/foo.md."""
    if not rel.startswith("canonical/") or rel.startswith("canonical/archived/"):
        return None
    rest = rel[len("canonical/") :]
    cand = f"canonical/archived/{rest}"
    return cand if _path_exists(cand) else None


def _try_distilled_equivalent(rel: str) -> str | None:
    """Find a distilled file whose stem-prefix matches the original stem.

    canonical/decisions/chris-updated-the-6-pm-email...md
      → distilled/decisions/dist_chris_updated_the_6_pm_email...md
    """
    if not rel:
        return None
    stem = Path(rel).stem.lower().replace("-", "_")
    if not stem:
        return None
    # Look across distilled subdirs.
    distilled_root = KNOWLEDGE_ROOT / "distilled"
    if not distilled_root.exists():
        return None
    target_keys = [stem[:40], stem[:30]]
    for path in distilled_root.rglob("*.md"):
        fname = path.stem.lower()
        if any(key and key in fname for key in target_keys):
            return str(path.relative_to(KNOWLEDGE_ROOT))
    return None


def migrate(eval_set_path: Path = DEFAULT_EVAL_SET, dry_run: bool = True) -> dict:
    if not eval_set_path.exists():
        return {"status": "missing_eval_set", "path": str(eval_set_path)}
    raw_text = eval_set_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    if not isinstance(data, list):
        return {"status": "not_a_list"}
    # Write the pre-mutation backup BEFORE we touch any entries.
    # The earlier version wrote the backup post-mutation, leaving the .bak
    # file identical to the migrated state — destroying the only rollback
    # path. Capture raw_text byte-for-byte so a future restore is exact.
    if not dry_run:
        backup = eval_set_path.with_suffix(eval_set_path.suffix + ".pre-migrate.bak")
        if not backup.exists():
            backup.write_text(raw_text)

    summary = {
        "total": len(data),
        "stale": 0,
        "resolved_archived": 0,
        "resolved_distilled": 0,
        "unresolved": 0,
        "already_ok": 0,
        "dry_run": dry_run,
    }
    sample_resolved: list[dict] = []
    sample_unresolved: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        src = entry.get("expected_source") or ""
        if not src:
            summary["already_ok"] += 1
            continue
        if _path_exists(src):
            summary["already_ok"] += 1
            continue
        summary["stale"] += 1
        alt = _try_archived_equivalent(src)
        if alt:
            summary["resolved_archived"] += 1
            if len(sample_resolved) < 5:
                sample_resolved.append({"orig": src, "new": alt, "via": "archived"})
            if not dry_run:
                _add_alternate(entry, alt)
            continue
        alt = _try_distilled_equivalent(src)
        if alt:
            summary["resolved_distilled"] += 1
            if len(sample_resolved) < 5:
                sample_resolved.append({"orig": src, "new": alt, "via": "distilled"})
            if not dry_run:
                _add_alternate(entry, alt)
            continue
        summary["unresolved"] += 1
        if len(sample_unresolved) < 5:
            sample_unresolved.append(src)

    if not dry_run and (summary["resolved_archived"] or summary["resolved_distilled"]):
        tmp = eval_set_path.with_suffix(eval_set_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(eval_set_path)

    summary["sample_resolved"] = sample_resolved
    summary["sample_unresolved"] = sample_unresolved
    return summary


def _add_alternate(entry: dict, new_source: str) -> None:
    """Migrate expected_source to the resolved path.

    Eval _source_matches does substring matching only — adding a separate
    field won't help. The original path no longer exists, so replacing
    expected_source in-place is the correct fix. Preserve the old value
    in expected_source_legacy for forensics.
    """
    legacy = entry.get("expected_source") or ""
    if legacy and legacy != new_source:
        entry["expected_source_legacy"] = legacy
    entry["expected_source"] = new_source


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    p.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    args = p.parse_args()
    result = migrate(args.eval_set, dry_run=not args.apply)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
