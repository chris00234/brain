#!/usr/bin/env python3
# ruff: noqa: E402,I001
"""Backfill distilled-note provenance from canonical note source links.

This is intentionally conservative: only exact canonical `sources` entries
that match a distilled note id are written. No fuzzy title/path matching.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.common import parse_note, utc_now, write_markdown_frontmatter


DEFAULT_KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")
log = logging.getLogger("brain.backfill_distilled_provenance")


@dataclass(frozen=True)
class BackfillChange:
    distilled_path: Path
    distilled_id: str
    canonical_id: str
    canonical_path: Path


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _relation_exists(relations: list[Any], rel_type: str, target: str) -> bool:
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        if relation.get("type") == rel_type and relation.get("target") == target:
            return True
    return False


def _note_files(base: Path) -> list[Path]:
    return sorted(path for path in base.rglob("*.md") if path.is_file())


def _load_notes(base: Path) -> list[tuple[Path, dict[str, Any], str]]:
    notes: list[tuple[Path, dict[str, Any], str]] = []
    if not base.exists():
        return notes
    for path in _note_files(base):
        try:
            metadata, body = parse_note(path)
        except Exception as exc:
            log.debug("skipping malformed note %s: %s", path, exc)
            continue
        notes.append((path, metadata, body))
    return notes


def _distilled_by_id(knowledge_dir: Path) -> dict[str, tuple[Path, dict[str, Any], str]]:
    by_id: dict[str, tuple[Path, dict[str, Any], str]] = {}
    for path, metadata, body in _load_notes(knowledge_dir / "distilled"):
        note_id = metadata.get("id")
        if isinstance(note_id, str) and note_id:
            by_id.setdefault(note_id, (path, metadata, body))
    return by_id


def _canonical_relpath(knowledge_dir: Path, canonical_path: Path) -> str:
    try:
        return canonical_path.relative_to(knowledge_dir).as_posix()
    except ValueError:
        return canonical_path.as_posix()


def _apply_distilled_backfill(
    *,
    knowledge_dir: Path,
    distilled_path: Path,
    distilled_meta: dict[str, Any],
    canonical_path: Path,
    canonical_id: str,
) -> bool:
    changed = False
    canonical_relpath = _canonical_relpath(knowledge_dir, canonical_path)

    supersedes = _unique_strings(_as_list(distilled_meta.get("supersedes")))
    if canonical_id not in supersedes:
        supersedes.append(canonical_id)
        distilled_meta["supersedes"] = supersedes
        changed = True

    aliases = _unique_strings(_as_list(distilled_meta.get("source_aliases")))
    for alias in (canonical_id, canonical_relpath, canonical_path.stem):
        if alias and alias not in aliases:
            aliases.append(alias)
            changed = True
    if aliases:
        distilled_meta["source_aliases"] = aliases

    relations = _as_list(distilled_meta.get("relations"))
    if not _relation_exists(relations, "supersedes", canonical_id):
        relations.append({"type": "supersedes", "target": canonical_id})
        distilled_meta["relations"] = relations
        changed = True

    existing = distilled_meta.get("provenance_backfill")
    if not isinstance(existing, dict):
        existing = {}
    canonical_paths = _unique_strings(_as_list(existing.get("canonical_paths")))
    backfill_changed = False
    if canonical_relpath not in canonical_paths:
        canonical_paths.append(canonical_relpath)
        backfill_changed = True
    backfill = {
        **existing,
        "method": "canonical_sources_distilled_id",
        "canonical_paths": canonical_paths,
    }
    if backfill.get("method") != existing.get("method"):
        backfill_changed = True
    if backfill_changed or changed:
        backfill["updated_at"] = utc_now()
    elif existing.get("updated_at"):
        backfill["updated_at"] = existing["updated_at"]
    if backfill != distilled_meta.get("provenance_backfill"):
        distilled_meta["provenance_backfill"] = backfill
        changed = changed or backfill_changed

    return changed


def backfill(knowledge_dir: Path, *, write: bool = False, limit: int | None = None) -> list[BackfillChange]:
    knowledge_dir = knowledge_dir.expanduser().resolve()
    distilled = _distilled_by_id(knowledge_dir)
    changes: list[BackfillChange] = []

    for canonical_path, canonical_meta, _canonical_body in _load_notes(knowledge_dir / "canonical"):
        canonical_id = canonical_meta.get("id")
        if not isinstance(canonical_id, str) or not canonical_id:
            continue
        for source_id in _unique_strings(_as_list(canonical_meta.get("sources"))):
            if not source_id.startswith("dist_") or source_id not in distilled:
                continue
            distilled_path, distilled_meta, distilled_body = distilled[source_id]
            candidate_meta = dict(distilled_meta)
            changed = _apply_distilled_backfill(
                knowledge_dir=knowledge_dir,
                distilled_path=distilled_path,
                distilled_meta=candidate_meta,
                canonical_path=canonical_path,
                canonical_id=canonical_id,
            )
            if not changed:
                continue
            changes.append(
                BackfillChange(
                    distilled_path=distilled_path,
                    distilled_id=source_id,
                    canonical_id=canonical_id,
                    canonical_path=canonical_path,
                )
            )
            if write:
                write_markdown_frontmatter(distilled_path, candidate_meta, distilled_body)
                distilled[source_id] = (distilled_path, candidate_meta, distilled_body)
            if limit is not None and len(changes) >= limit:
                return changes
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-dir", type=Path, default=DEFAULT_KNOWLEDGE_DIR)
    parser.add_argument("--write", action="store_true", help="write matching distilled note metadata")
    parser.add_argument("--limit", type=int, help="maximum changes to report/write")
    args = parser.parse_args()

    changes = backfill(args.knowledge_dir, write=args.write, limit=args.limit)
    action = "updated" if args.write else "would update"
    print(f"{action} {len(changes)} distilled notes")
    for change in changes[:50]:
        rel_dist = _canonical_relpath(args.knowledge_dir, change.distilled_path)
        rel_canon = _canonical_relpath(args.knowledge_dir, change.canonical_path)
        print(f"- {rel_dist}: supersedes {change.canonical_id} ({rel_canon})")
    if len(changes) > 50:
        print(f"... {len(changes) - 50} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
