"""brain_core/provenance.py — trace relation chains across canonical notes.

Reads canonical + distilled markdown files, parses JSON frontmatter, follows
relations (depends_on, supports, informs, supersedes, affects, governed_by)
up to a max depth. Returns a tree structure.

No graph DB needed — pure in-memory traversal of ~100 markdown files.

Usage:
    from provenance import trace
    tree = trace("infra_rag_chromadb", max_depth=3)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.provenance")

try:
    from config import CANONICAL_DIR, DISTILLED_DIR
except ImportError:
    CANONICAL_DIR = Path("/Users/chrischo/server/knowledge/canonical")
    DISTILLED_DIR = Path("/Users/chrischo/server/knowledge/distilled")


def _parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse JSON frontmatter from a canonical/distilled note."""
    try:
        text = path.read_text()
        lines = text.splitlines()
        if len(lines) < 3:
            return None
        # Find frontmatter boundaries
        first = lines[0].strip()
        if not (first.startswith("---") or first.startswith("{")):
            return None
        # If starts with ---, find closing ---
        if first.startswith("---"):
            end_index = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end_index = i
                    break
            if end_index is None:
                return None
            raw = "\n".join(lines[1:end_index])
        else:
            # Direct JSON (no --- wrapper)
            raw = text.split("---")[0] if "---" in text else text
        meta = json.loads(raw)
        meta["_path"] = str(path.relative_to(path.parents[2]))  # relative to knowledge/
        body_start = (end_index + 1) if first.startswith("---") else 0
        meta["_body_preview"] = "\n".join(lines[body_start:])[:200].strip()
        return meta
    except Exception:
        return None


import threading as _threading
import time as _time

_index_cache: dict[str, dict] | None = None
_index_cache_ts: float = 0.0
_INDEX_TTL: float = 300.0  # 5 minutes
_index_lock = _threading.Lock()


def _build_index() -> dict[str, dict]:
    """Build id → metadata index from all canonical + distilled notes. Cached for 5 min."""
    global _index_cache, _index_cache_ts
    now = _time.time()
    with _index_lock:
        if _index_cache is not None and (now - _index_cache_ts) < _INDEX_TTL:
            return _index_cache

        index: dict[str, dict] = {}
        for base_dir in (CANONICAL_DIR, DISTILLED_DIR):
            if not base_dir.exists():
                continue
            for md_file in base_dir.rglob("*.md"):
                meta = _parse_frontmatter(md_file)
                if meta and "id" in meta:
                    if meta["id"] not in index or "canonical" in str(md_file):
                        index[meta["id"]] = meta

        # Build reverse adjacency map at cache time (O(n) once, not O(n²) per traversal)
        reverse_map: dict[str, list[tuple[str, str]]] = {}  # target_id -> [(source_id, rel_type)]
        for nid, meta in index.items():
            for rel in meta.get("relations", []):
                if isinstance(rel, dict) and "target" in rel:
                    target = rel["target"]
                    reverse_map.setdefault(target, []).append((nid, rel.get("type", "related")))

        # Store reverse map alongside index
        for meta in index.values():
            meta["_reverse_relations"] = reverse_map.get(meta.get("id", ""), [])

        _index_cache = index
        _index_cache_ts = _time.time()
        return index


def trace(note_id: str, max_depth: int = 3) -> dict:
    """Trace relation chains from a note, returning a tree."""
    index = _build_index()

    if note_id not in index:
        return {"error": f"note '{note_id}' not found", "available_ids": sorted(index.keys())[:20]}

    visited: set[str] = set()

    def _traverse(nid: str, depth: int) -> dict:
        if depth > max_depth:
            return {"id": nid, "truncated": True, "reason": "max_depth"}
        if nid in visited:
            return {"id": nid, "truncated": True, "reason": "already_visited"}
        visited.add(nid)

        meta = index.get(nid)
        if meta is None:
            return {"id": nid, "missing": True}

        node = {
            "id": nid,
            "title": meta.get("title", ""),
            "domain": meta.get("domain", ""),
            "type": meta.get("type", ""),
            "status": meta.get("status", ""),
            "confidence": meta.get("confidence", 0),
            "path": meta.get("_path", ""),
            "body_preview": meta.get("_body_preview", ""),
        }

        children = []

        # Forward relations (outgoing from this note)
        for rel in meta.get("relations", []):
            if isinstance(rel, dict) and "target" in rel:
                child = _traverse(rel["target"], depth + 1)
                child["relation_type"] = rel.get("type", "related")
                child["direction"] = "forward"
                children.append(child)

        # Supersedes chain
        for sup_id in (meta.get("supersedes") or []):
            if isinstance(sup_id, str):
                child = _traverse(sup_id, depth + 1)
                child["relation_type"] = "supersedes"
                child["direction"] = "forward"
                children.append(child)

        superseded_by = meta.get("superseded_by")
        if superseded_by and isinstance(superseded_by, str):
            child = _traverse(superseded_by, depth + 1)
            child["relation_type"] = "superseded_by"
            child["direction"] = "forward"
            children.append(child)

        # Reverse relations (precomputed at cache time — O(1) lookup, not O(n) scan)
        for source_id, rel_type in meta.get("_reverse_relations", []):
            if source_id == nid or source_id in visited:
                continue
            child = _traverse(source_id, depth + 1)
            child["relation_type"] = f"inverse_{rel_type}"
            child["direction"] = "reverse"
            children.append(child)

        if children:
            node["related"] = children

        return node

    root = _traverse(note_id, 0)
    root["total_notes_scanned"] = len(index)
    return root
