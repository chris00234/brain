#!/opt/homebrew/bin/python3
"""Promote reviewed merge drafts to canonical and archive superseded notes.

Reads drafts from `reports/canonical_compaction/drafts/YYYY-MM-DD/cluster_*.md`,
for each one:
  1. Validates draft frontmatter (status=draft, supersedes list populated)
  2. Writes new canonical note at canonical/<domain>/<slug>.md with
     status=active, review_state=confirmed, timestamps refreshed
  3. For each id in supersedes: moves original file from canonical/<domain>/
     to canonical/archived/<domain>/<basename>.md with status=superseded,
     superseded_by=<draft_id>, archived_at=<now>
  4. Moves draft file to drafts/applied/<basename>.md as breadcrumb

Usage:
  canonical_merge_apply.py [--dry-run] [--drafts-dir PATH] [--cluster CLUSTER_IDX]

Default reads latest drafts/YYYY-MM-DD/ subdir.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import ROOT, iter_note_paths, parse_markdown_frontmatter, parse_note, render_note, slugify

DRAFTS_BASE = ROOT / "reports" / "canonical_compaction" / "drafts"
CANONICAL_DIR = ROOT / "canonical"
ARCHIVE_DIR = CANONICAL_DIR / "archived"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _latest_drafts_dir() -> Path | None:
    if not DRAFTS_BASE.exists():
        return None
    dirs = sorted([p for p in DRAFTS_BASE.iterdir() if p.is_dir() and p.name != "applied"], reverse=True)
    return dirs[0] if dirs else None


def _find_note_by_id(note_id: str) -> Path | None:
    """Locate a canonical note path by its frontmatter id."""
    for path in iter_note_paths(CANONICAL_DIR):
        # Skip already-archived
        if "archived" in path.relative_to(CANONICAL_DIR).parts:
            continue
        try:
            meta, _ = parse_note(path)
        except Exception:
            continue
        if meta.get("id") == note_id:
            return path
    return None


def _archive_one(note_id: str, draft_id: str, dry_run: bool) -> dict:
    """Move original to canonical/archived/<domain>/ with superseded status."""
    src = _find_note_by_id(note_id)
    if src is None:
        return {"note_id": note_id, "status": "not_found"}
    try:
        meta, body = parse_note(src)
    except Exception as e:
        return {"note_id": note_id, "status": "parse_error", "error": str(e)[:100]}
    domain = meta.get("domain") or "other"

    dest_dir = ARCHIVE_DIR / domain
    dest = dest_dir / src.name

    if dry_run:
        return {
            "note_id": note_id,
            "status": "would_archive",
            "from": str(src.relative_to(ROOT)),
            "to": str(dest.relative_to(ROOT)),
        }

    meta["status"] = "superseded"
    meta["superseded_by"] = draft_id
    meta["archived_at"] = _utc_now()
    meta["archived_reason"] = "merge_apply:consolidated"
    meta["valid_to"] = _utc_now()

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(render_note(meta, body))
    tmp.replace(dest)
    src.unlink()
    return {
        "note_id": note_id,
        "status": "archived",
        "from": str(src.relative_to(ROOT)),
        "to": str(dest.relative_to(ROOT)),
    }


def _promote_draft(draft_path: Path, dry_run: bool) -> dict:
    try:
        meta, body = parse_note(draft_path)
    except Exception as e:
        return {"draft": draft_path.name, "status": "parse_error", "error": str(e)[:200]}

    if meta.get("status") != "draft":
        return {"draft": draft_path.name, "status": "skipped_not_draft", "current_status": meta.get("status")}

    supersedes = meta.get("supersedes") or []
    if not supersedes:
        return {"draft": draft_path.name, "status": "skipped_no_supersedes"}

    # Derive final canonical id/slug
    draft_id = meta.get("id") or ""
    canonical_id = (
        draft_id.replace("draft_merge_", "merge_", 1)
        if draft_id.startswith("draft_merge_")
        else f"merge_{draft_id}"
    )
    domain = meta.get("domain") or "decisions"
    title = meta.get("title") or draft_id
    slug = slugify(title)
    if not slug:
        slug = canonical_id

    # Update frontmatter for active canonical state
    now = _utc_now()
    meta["id"] = canonical_id
    meta["status"] = "active"
    meta["subtype"] = "consolidated"
    meta["review_state"] = "confirmed"
    meta["updated_at"] = now
    meta["last_reviewed_at"] = now
    meta["confidence"] = 0.85
    meta["change_policy"] = "review_required"
    # Preserve supersedes but also add relations for graph connectivity
    relations = list(meta.get("relations") or [])
    for sid in supersedes:
        rel = {"type": "supersedes", "target": sid}
        if rel not in relations:
            relations.append(rel)
    meta["relations"] = relations

    target = CANONICAL_DIR / domain / f"{slug}.md"

    # Slug collision check — abort before any write to prevent data loss
    if target.exists():
        try:
            existing_meta, _ = parse_markdown_frontmatter(target)
            if existing_meta.get("id") != canonical_id:
                return {
                    "draft": draft_path.name,
                    "status": "skipped_slug_collision",
                    "target": str(target.relative_to(ROOT)),
                    "existing_id": existing_meta.get("id"),
                    "new_id": canonical_id,
                }
        except Exception:
            return {
                "draft": draft_path.name,
                "status": "skipped_unreadable_target",
                "target": str(target.relative_to(ROOT)),
            }

    if dry_run:
        return {
            "draft": draft_path.name,
            "status": "would_promote",
            "canonical_path": str(target.relative_to(ROOT)),
            "canonical_id": canonical_id,
            "supersedes_count": len(supersedes),
        }

    # Write the new active canonical note (atomic tmp+rename)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(render_note(meta, body))
    tmp.replace(target)

    # Archive every superseded original
    archive_results = [_archive_one(sid, canonical_id, dry_run=False) for sid in supersedes]
    archived_ok = sum(1 for r in archive_results if r["status"] == "archived")
    archive_missing = sum(1 for r in archive_results if r["status"] == "not_found")

    # Move draft to applied/ breadcrumb
    applied_dir = DRAFTS_BASE / "applied"
    applied_dir.mkdir(parents=True, exist_ok=True)
    applied_path = applied_dir / draft_path.name
    shutil.move(str(draft_path), str(applied_path))

    # Mirror as canonical atom + extract entities into Neo4j (same path as promote_canonical.py)
    try:
        sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
        from atoms_store import upsert_atom

        text_preview = (title + "\n" + body)[:2000]
        upsert_atom(
            text=text_preview,
            chroma_id=f"canonical:{canonical_id}",
            kind="decision" if domain == "decisions" else "fact",
            confidence=meta["confidence"],
            tier="core",
            canonical=True,
            version_of=canonical_id,
            distilled_by="canonical",
            collection_hint="canonical",
            valid_from=meta.get("valid_from"),
            valid_until=meta.get("valid_to"),
            provenance={"path": str(target), "supersedes_count": len(supersedes)},
        )
    except Exception:
        pass

    # Extract entities into Neo4j — keeps graph UI synced with new consolidated pages
    try:
        from entity_graph import extract_and_store_entities

        note_text = f"{title} {body[:1500]}"
        extract_and_store_entities(note_text, canonical_id)
    except Exception:
        pass

    return {
        "draft": draft_path.name,
        "status": "promoted",
        "canonical_path": str(target.relative_to(ROOT)),
        "canonical_id": canonical_id,
        "supersedes_requested": len(supersedes),
        "supersedes_archived": archived_ok,
        "supersedes_missing": archive_missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--drafts-dir", type=Path, help="override drafts directory")
    parser.add_argument("--cluster", type=str, help="only apply a specific cluster (e.g., '00')")
    args = parser.parse_args()

    drafts_dir = args.drafts_dir or _latest_drafts_dir()
    if drafts_dir is None or not drafts_dir.exists():
        print(json.dumps({"status": "no_drafts_dir"}))
        return 1

    pattern = f"cluster_{args.cluster}_*.md" if args.cluster else "cluster_*.md"
    draft_paths = sorted(drafts_dir.glob(pattern))
    if not draft_paths:
        print(json.dumps({"status": "no_drafts", "drafts_dir": str(drafts_dir.relative_to(ROOT))}))
        return 0

    print(
        f"[canonical_merge_apply] drafts_dir={drafts_dir.name} drafts={len(draft_paths)} dry_run={args.dry_run}",
        file=sys.stderr,
    )

    results = []
    for dp in draft_paths:
        results.append(_promote_draft(dp, args.dry_run))

    summary = {
        "status": "ok" if not args.dry_run else "dry-run",
        "drafts_dir": str(drafts_dir.relative_to(ROOT)),
        "count": len(results),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
