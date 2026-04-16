#!/opt/homebrew/bin/python3
"""Backfill canonical note relations[] from canonical_lint's missing_cross_refs.

Round 2 Step 2 of the llm-wiki quality improvements.

Phase 4 (Round 1) added auto cross-ref injection to `pipeline/promote_canonical.py`,
but it only fires on NEW promotions. Existing canonical notes flagged by
`canonical_lint.missing_cross_refs` still lack their `{type: "mentions",
target: entity_page_id}` relations. This script backfills them.

Reads the most recent lint report and applies each flagged note. Idempotent
(skips already-linked). Safe — doesn't touch body text, only frontmatter.

Usage:
  backfill_canonical_xrefs.py [--dry-run] [--lint-report PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import ROOT, parse_note, render_note  # noqa: E402

REPORT_DIR = ROOT / "reports" / "canonical_lint"


def _latest_report() -> Path | None:
    if not REPORT_DIR.exists():
        return None
    reports = sorted(REPORT_DIR.glob("*.json"), reverse=True)
    return reports[0] if reports else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _backfill_note(note_path: Path, entity_page_id: str, dry_run: bool) -> str:
    """Return status: 'added' | 'already_linked' | 'missing' | 'error'."""
    if not note_path.exists():
        return "missing"
    try:
        meta, body = parse_note(note_path)
    except Exception:
        return "error"

    relations = list(meta.get("relations") or [])
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        if rel.get("type") == "mentions" and rel.get("target") == entity_page_id:
            return "already_linked"

    relations.append({"type": "mentions", "target": entity_page_id})
    meta["relations"] = relations
    meta["updated_at"] = _utc_now()

    if dry_run:
        return "added"

    tmp = note_path.with_suffix(".tmp")
    tmp.write_text(render_note(meta, body))
    tmp.replace(note_path)
    return "added"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lint-report", type=Path, help="path to a specific lint report JSON")
    args = parser.parse_args()

    report_path = args.lint_report or _latest_report()
    if not report_path or not report_path.exists():
        print(json.dumps({"status": "no_report", "report_dir": str(REPORT_DIR)}))
        return 1

    try:
        report = json.loads(report_path.read_text())
    except Exception as e:
        print(json.dumps({"status": "bad_report", "error": str(e)}))
        return 1

    items = (report.get("checks", {}).get("missing_cross_refs", {}).get("items", []))
    if not items:
        print(json.dumps({"status": "ok", "report": report_path.name, "candidates": 0}))
        return 0

    counts = {"added": 0, "already_linked": 0, "missing": 0, "error": 0}
    details: list[dict] = []

    for item in items:
        note_rel = item.get("note_path")
        entity_page_id = item.get("entity_page_id")
        if not note_rel or not entity_page_id:
            counts["error"] += 1
            continue
        note_path = ROOT / note_rel
        result = _backfill_note(note_path, entity_page_id, args.dry_run)
        counts[result] += 1
        details.append({
            "note_id": item.get("note_id"),
            "entity": item.get("entity"),
            "target": entity_page_id,
            "result": result,
        })

    print(json.dumps({
        "status": "ok",
        "report": report_path.name,
        "dry_run": args.dry_run,
        "candidates": len(items),
        **counts,
        "details": details,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
