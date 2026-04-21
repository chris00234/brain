#!/Users/chrischo/server/brain/.venv/bin/python
"""cli/backfill_canonical_status.py — sync canonical MD status → Chroma metadata.

2026-04-17: the pre-R-2 era never wrote `status` or `superseded_by` to
Chroma canonical rows. As a result:
  - search_unified's `not include_history and superseded` filter couldn't
    see pre-existing supersessions (only NEW ones via R-2 mirror)
  - RAPTOR's `where: {"status": "active"}` returned zero rows on first
    run (all 6083 canonical rows lacked status entirely)

This one-shot script walks every canonical/*.md file, reads its
frontmatter status + superseded_by, and writes both to the matching
Chroma canonical row's metadata. Idempotent — rerun-safe.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BRAIN_CORE = Path(__file__).resolve().parent.parent / "brain_core"
sys.path.insert(0, str(BRAIN_CORE))

CANONICAL_DIR = Path("/Users/chrischo/server/knowledge/canonical")


def _read_frontmatter(p: Path) -> dict | None:
    try:
        t = p.read_text(errors="replace")
    except Exception:
        return None
    if not t.startswith("---json"):
        return None
    end = t.find("---", 7)
    if end <= 0:
        return None
    try:
        return json.loads(t[7:end])
    except Exception:
        return None


def main() -> int:
    from vector_store import get_vector_store  # type: ignore

    store = get_vector_store()

    path_to_meta: dict[str, dict] = {}
    for p in CANONICAL_DIR.rglob("*.md"):
        meta = _read_frontmatter(p)
        if not meta:
            continue
        status = meta.get("status")
        if not status:
            continue
        path_to_meta[str(p)] = {
            "status": status,
            "superseded_by": meta.get("superseded_by") or "",
            "valid_to": meta.get("valid_to") or "",
            "updated_at": meta.get("updated_at") or "",
        }

    print(f"scanned {len(path_to_meta)} canonical MD files")

    # Pull all canonical rows + build path → [ids] index, then update
    # by id via VectorStore.update_payload.
    print("  fetching canonical id index...")
    points = store.get(
        "canonical",
        limit=20000,
        with_payload=True,
        with_documents=False,
    )
    # The `source` field holds the filesystem path (indexer convention).
    # 2026-04-17 fix: previous version looked at `path` and matched
    # zero rows silently — stick to `source`.
    path_to_ids: dict[str, list[str]] = {}
    for p in points:
        m = p.payload or {}
        src = m.get("source") or m.get("path")
        if not src:
            continue
        path_to_ids.setdefault(src, []).append(p.id)
    print(f"  indexed {sum(len(v) for v in path_to_ids.values())} rows across {len(path_to_ids)} sources")

    updated_rows = 0
    errors = 0
    by_status = {"active": 0, "superseded": 0, "other": 0}
    for md_path, meta_updates in path_to_meta.items():
        ids = path_to_ids.get(md_path)
        if not ids:
            continue
        try:
            for cid in ids:
                store.update_payload("canonical", ids=[cid], patch=meta_updates)
            updated_rows += len(ids)
            s = meta_updates["status"]
            by_status[s] = by_status.get(s, 0) + 1
            if updated_rows % 500 == 0:
                print(f"  {updated_rows}/{sum(len(v) for v in path_to_ids.values())}...")
        except Exception as e:
            errors += 1
            if errors < 5:
                print(f"ERR {md_path}: {e}")

    summary = {
        "status": "ok",
        "md_files_scanned": len(path_to_meta),
        "chroma_rows_updated": updated_rows,
        "errors": errors,
        "by_status": by_status,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
