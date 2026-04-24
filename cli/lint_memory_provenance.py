#!/usr/bin/env python3
# ruff: noqa: E402
"""Read-only lint for canonical/distilled provenance and supersession metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.common import ValidationError, parse_note, utc_now, write_markdown_frontmatter

DEFAULT_KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")
DEFAULT_REPORT = ROOT / "logs" / "memory-provenance-lint.json"


@dataclass(frozen=True)
class LintIssue:
    severity: str
    code: str
    path: str
    note_id: str
    message: str


@dataclass
class NoteRecord:
    path: Path
    metadata: dict[str, Any]
    body: str
    relpath: str


@dataclass(frozen=True)
class RepairChange:
    code: str
    path: str
    old_value: str
    new_value: str
    message: str


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _strings(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _relation_targets(metadata: dict[str, Any], rel_type: str) -> set[str]:
    targets: set[str] = set()
    for relation in _as_list(metadata.get("relations")):
        if not isinstance(relation, dict):
            continue
        if relation.get("type") == rel_type and isinstance(relation.get("target"), str):
            targets.add(relation["target"].strip())
    return {target for target in targets if target}


def _relpath(knowledge_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(knowledge_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _is_support_markdown(relpath: str) -> bool:
    """Canonical support docs are searchable documents, not canonical notes."""
    return (
        relpath == "canonical/index.md"
        or relpath.startswith("canonical/design/")
        or relpath.startswith("canonical/live_state/")
    )


def _load_records(knowledge_dir: Path) -> tuple[list[NoteRecord], list[LintIssue], list[str]]:
    records: list[NoteRecord] = []
    issues: list[LintIssue] = []
    skipped_support_docs: list[str] = []
    for base in (knowledge_dir / "canonical", knowledge_dir / "distilled"):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            relpath = _relpath(knowledge_dir, path)
            try:
                metadata, body = parse_note(path)
            except (OSError, ValidationError, json.JSONDecodeError) as exc:
                if _is_support_markdown(relpath):
                    skipped_support_docs.append(relpath)
                    continue
                issues.append(
                    LintIssue(
                        severity="error",
                        code="parse_error",
                        path=relpath,
                        note_id="",
                        message=str(exc)[:240],
                    )
                )
                continue
            records.append(NoteRecord(path=path, metadata=metadata, body=body, relpath=relpath))
    return records, issues, skipped_support_docs


def lint(knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR) -> dict:
    knowledge_dir = knowledge_dir.expanduser().resolve()
    records, issues, skipped_support_docs = _load_records(knowledge_dir)
    by_id: dict[str, list[Path]] = {}
    by_stem: dict[str, Path] = {}
    for record in records:
        path = record.path
        metadata = record.metadata
        note_id = metadata.get("id")
        if isinstance(note_id, str) and note_id:
            by_id.setdefault(note_id, []).append(path)
        by_stem.setdefault(path.stem, path)

    for note_id, paths in sorted(by_id.items()):
        if len(paths) <= 1:
            continue
        for path in paths:
            issues.append(
                LintIssue(
                    severity="error",
                    code="duplicate_id",
                    path=_relpath(knowledge_dir, path),
                    note_id=note_id,
                    message=f"id appears in {len(paths)} notes",
                )
            )

    valid_targets = set(by_id) | set(by_stem)
    distilled_ids = {
        record.metadata["id"]
        for record in records
        if isinstance(record.metadata.get("id"), str)
        and (
            record.metadata.get("type") == "distilled"
            or str(record.metadata.get("id", "")).startswith("dist_")
        )
    }

    for record in records:
        metadata = record.metadata
        note_id = metadata.get("id") if isinstance(metadata.get("id"), str) else ""
        rel = record.relpath
        status = str(metadata.get("status") or "").lower()
        supersedes = _strings(metadata.get("supersedes"))
        superseded_by = _strings(metadata.get("superseded_by"))
        supersedes_rel_targets = _relation_targets(metadata, "supersedes")

        if not note_id:
            issues.append(LintIssue("error", "missing_id", rel, "", "note frontmatter has no id"))

        if note_id and note_id in supersedes:
            issues.append(LintIssue("error", "self_supersedes", rel, note_id, "note supersedes itself"))

        for target in superseded_by:
            if target not in valid_targets:
                issues.append(
                    LintIssue(
                        "error",
                        "missing_superseded_by_target",
                        rel,
                        note_id,
                        f"superseded_by target not found: {target}",
                    )
                )

        for target in supersedes:
            if target not in valid_targets:
                issues.append(
                    LintIssue(
                        "warning",
                        "missing_supersedes_target",
                        rel,
                        note_id,
                        f"supersedes target not found: {target}",
                    )
                )
            if target not in supersedes_rel_targets:
                issues.append(
                    LintIssue(
                        "warning",
                        "missing_supersedes_relation",
                        rel,
                        note_id,
                        f"supersedes target lacks matching relations[] edge: {target}",
                    )
                )

        if status == "active" and superseded_by:
            issues.append(
                LintIssue(
                    "warning",
                    "active_note_is_superseded",
                    rel,
                    note_id,
                    f"active note has superseded_by={superseded_by[0]}",
                )
            )

        if rel.startswith("canonical/archived/") and status not in {"superseded", "obsolete", "archived"}:
            issues.append(
                LintIssue(
                    "warning",
                    "archived_note_not_marked_superseded",
                    rel,
                    note_id,
                    f"archived canonical note has status={status or '<missing>'}",
                )
            )

        for source in _strings(metadata.get("sources")):
            if source.startswith("dist_") and source not in distilled_ids:
                issues.append(
                    LintIssue(
                        "warning",
                        "missing_distilled_source",
                        rel,
                        note_id,
                        f"sources[] references missing distilled id: {source}",
                    )
                )

    counts = {
        "errors": sum(1 for issue in issues if issue.severity == "error"),
        "warnings": sum(1 for issue in issues if issue.severity == "warning"),
    }
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "knowledge_dir": str(knowledge_dir),
        "notes_checked": len(records),
        "support_docs_skipped": len(skipped_support_docs),
        **counts,
        "issues": [asdict(issue) for issue in issues],
    }


def _is_distilled_record(record: NoteRecord) -> bool:
    return record.relpath.startswith("distilled/") or record.metadata.get("type") == "distilled"


def _duplicate_winner_key(record: NoteRecord) -> tuple[int, int, str]:
    status = str(record.metadata.get("status") or "").lower()
    is_active = 0 if status == "active" else 1
    is_canonical = 0 if record.relpath.startswith("canonical/") else 1
    return (is_canonical, is_active, record.relpath)


def _stable_duplicate_id(old_id: str, record: NoteRecord, existing_ids: set[str]) -> str:
    parent = record.path.parent.name.replace("-", "_")
    digest = hashlib.sha256(record.relpath.encode()).hexdigest()[:8]
    base = f"{old_id}__{parent}_{digest}"
    candidate = base
    idx = 2
    while candidate in existing_ids:
        candidate = f"{base}_{idx}"
        idx += 1
    existing_ids.add(candidate)
    return candidate


def _repair_one_record(
    *,
    record: NoteRecord,
    valid_targets: set[str],
    now: str,
) -> list[RepairChange]:
    changes: list[RepairChange] = []
    metadata = record.metadata
    note_id = metadata.get("id") if isinstance(metadata.get("id"), str) else ""

    supersedes = _unique_strings(_as_list(metadata.get("supersedes")))
    if note_id and note_id in supersedes:
        repaired = [item for item in supersedes if item != note_id]
        metadata["supersedes"] = repaired
        changes.append(
            RepairChange(
                code="remove_self_supersedes",
                path=record.relpath,
                old_value=note_id,
                new_value="",
                message="removed note id from its own supersedes list",
            )
        )
        supersedes = repaired

    relations = _as_list(metadata.get("relations"))
    existing_supersedes = _relation_targets(metadata, "supersedes")
    for target in supersedes:
        if target not in valid_targets or target in existing_supersedes:
            continue
        rel = {"type": "supersedes", "target": target}
        relations.append(rel)
        existing_supersedes.add(target)
        metadata["relations"] = relations
        changes.append(
            RepairChange(
                code="add_supersedes_relation",
                path=record.relpath,
                old_value="",
                new_value=target,
                message="added relations[] edge matching supersedes[]",
            )
        )

    if changes:
        existing = metadata.get("provenance_repair")
        repair_meta = existing if isinstance(existing, dict) else {}
        codes = _unique_strings([*repair_meta.get("codes", []), *(change.code for change in changes)])
        metadata["provenance_repair"] = {
            **repair_meta,
            "method": "lint_memory_provenance.safe_repair",
            "codes": codes,
            "updated_at": now,
        }
    return changes


def repair_safe(knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR, *, write: bool = False) -> dict:
    """Apply only metadata-preserving repairs.

    Covered:
    - remove self-references from supersedes[]
    - add missing relations[] supersedes edges when the target exists
    - disambiguate duplicate distilled ids by path-qualified ids while keeping
      the prior id in source_aliases/previous_ids

    Not covered: deleting notes, changing canonical duplicate ids, or guessing
    missing supersession targets.
    """
    knowledge_dir = knowledge_dir.expanduser().resolve()
    records, load_issues, skipped_support_docs = _load_records(knowledge_dir)
    by_id: dict[str, list[NoteRecord]] = {}
    by_stem: dict[str, NoteRecord] = {}
    for record in records:
        note_id = record.metadata.get("id")
        if isinstance(note_id, str) and note_id:
            by_id.setdefault(note_id, []).append(record)
        by_stem.setdefault(record.path.stem, record)

    valid_targets = set(by_id) | set(by_stem)
    existing_ids = set(by_id)
    now = utc_now()
    changes: list[RepairChange] = []
    changed_records: dict[Path, NoteRecord] = {}

    for record in records:
        record_changes = _repair_one_record(record=record, valid_targets=valid_targets, now=now)
        if record_changes:
            changes.extend(record_changes)
            changed_records[record.path] = record

    for old_id, duplicates in sorted(by_id.items()):
        if len(duplicates) <= 1:
            continue
        ordered = sorted(duplicates, key=_duplicate_winner_key)
        winner, losers = ordered[0], ordered[1:]
        for record in losers:
            if not _is_distilled_record(record):
                continue
            new_id = _stable_duplicate_id(old_id, record, existing_ids)
            record.metadata["id"] = new_id
            aliases = _unique_strings(
                [*_as_list(record.metadata.get("source_aliases")), old_id, record.path.stem]
            )
            record.metadata["source_aliases"] = aliases
            previous = _unique_strings([*_as_list(record.metadata.get("previous_ids")), old_id])
            record.metadata["previous_ids"] = previous
            existing = record.metadata.get("provenance_repair")
            repair_meta = existing if isinstance(existing, dict) else {}
            record.metadata["provenance_repair"] = {
                **repair_meta,
                "method": "lint_memory_provenance.safe_repair",
                "duplicate_id_winner": winner.relpath,
                "updated_at": now,
            }
            changes.append(
                RepairChange(
                    code="rename_duplicate_distilled_id",
                    path=record.relpath,
                    old_value=old_id,
                    new_value=new_id,
                    message="renamed duplicate distilled id and preserved old id as source_alias",
                )
            )
            changed_records[record.path] = record

    if write:
        for record in changed_records.values():
            write_markdown_frontmatter(record.path, record.metadata, record.body)

    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "knowledge_dir": str(knowledge_dir),
        "write": write,
        "parse_errors": len(load_issues),
        "support_docs_skipped": len(skipped_support_docs),
        "changed_files": len(changed_records),
        "change_count": len(changes),
        "changes": [asdict(change) for change in changes],
    }


def _exit_code(report: dict, fail_on: str) -> int:
    if fail_on == "warning" and (report["errors"] or report["warnings"]):
        return 1
    if fail_on == "error" and report["errors"]:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-dir", type=Path, default=DEFAULT_KNOWLEDGE_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-issues", type=int, default=30)
    parser.add_argument("--fail-on", choices=["never", "error", "warning"], default="never")
    parser.add_argument(
        "--repair-safe",
        action="store_true",
        help="preview safe metadata-only repairs; combine with --write-repair to apply",
    )
    parser.add_argument("--write-repair", action="store_true", help="apply --repair-safe changes")
    args = parser.parse_args()

    repair_report = None
    if args.repair_safe:
        repair_report = repair_safe(args.knowledge_dir, write=args.write_repair)

    report = lint(args.knowledge_dir)
    if args.write_report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    if args.json:
        payload = {"repair": repair_report, "lint": report} if repair_report else report
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if repair_report:
            mode = "applied" if repair_report["write"] else "would apply"
            print(
                f"{mode} {repair_report['change_count']} repair(s) "
                f"across {repair_report['changed_files']} file(s)"
            )
            for change in repair_report["changes"][: args.max_issues]:
                print(
                    f"- [{change['code']}] {change['path']}: "
                    f"{change['old_value']} -> {change['new_value']}"
                )
            remaining_repairs = len(repair_report["changes"]) - args.max_issues
            if remaining_repairs > 0:
                print(f"... {remaining_repairs} more repair(s)")
        print(
            f"checked {report['notes_checked']} notes: "
            f"{report['errors']} error(s), {report['warnings']} warning(s)"
        )
        for issue in report["issues"][: args.max_issues]:
            print(
                f"- [{issue['severity']}] {issue['code']} "
                f"{issue['path']} ({issue['note_id']}): {issue['message']}"
            )
        remaining = len(report["issues"]) - args.max_issues
        if remaining > 0:
            print(f"... {remaining} more issue(s)")
    return _exit_code(report, args.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
