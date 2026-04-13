"""Temporal reasoning over semantic_memory — diff, evolution, and summaries.

Depends on:
  - temporal.parse_range(), filter_by_created_at()
  - indexer.chroma_api(), get_embedding(), _get_collection_id()
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from datetime import datetime, timezone
from temporal import parse_range, filter_by_created_at
from indexer import chroma_api, get_embedding, _get_collection_id

CHROMA_PREFIX = "/api/v2/tenants/default_tenant/databases/default_database/collections"
CHAIN_HOP_LIMIT = 10
GET_LIMIT = 500


def _col_path(col_id: str, action: str) -> str:
    return f"{CHROMA_PREFIX}/{col_id}/{action}"


def _get_filtered(col_id: str, where: dict, limit: int = GET_LIMIT) -> list[dict]:
    """Fetch docs from ChromaDB with a where filter, return flat list of
    {id, content, category, created_at, valid_until}."""
    try:
        resp = chroma_api("POST", _col_path(col_id, "get"), {
            "where": where,
            "limit": limit,
            "include": ["documents", "metadatas"],
        })
    except Exception:
        return []
    ids = resp.get("ids", [])
    docs = resp.get("documents", []) or []
    metas = resp.get("metadatas", []) or []
    results = []
    for i, mid in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        results.append({
            "id": mid,
            "content": docs[i] if i < len(docs) else "",
            "category": (meta or {}).get("category", ""),
            "created_at": (meta or {}).get("created_at", ""),
            "valid_until": (meta or {}).get("valid_until", ""),
        })
    return results


# ── 1. knowledge_diff ───────────────────────────────────
def knowledge_diff(since: str, until: str = "now") -> dict:
    """Return {added, changed, removed, period} for semantic_memory in the given time range.

    Post-2026-04-12: ChromaDB 1.4.1 rejects string operands in $gte/$lt. We fetch
    by non-range filters only and apply the date filter Python-side on the results.
    """
    start_dt, end_dt = parse_range(since, until if until != "now" else None)
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)

    col_id = _get_collection_id("semantic_memory")
    if not col_id:
        return {"added": [], "changed": [], "removed": [], "period": {"since": since, "until": until}}

    # Fetch candidates by non-date filter, then post-filter by created_at range.
    added_candidates = _get_filtered(col_id, {"supersedes": {"$eq": ""}}, limit=GET_LIMIT * 4)
    added = filter_by_created_at(added_candidates, start_dt, end_dt, field="created_at")

    changed_candidates = _get_filtered(col_id, {"supersedes": {"$ne": ""}}, limit=GET_LIMIT * 4)
    changed = filter_by_created_at(changed_candidates, start_dt, end_dt, field="created_at")

    # Removed: valid_until falls within the range. Fetch all with non-empty valid_until,
    # then post-filter. Safer than relying on ChromaDB for the range.
    removed_candidates = _get_filtered(col_id, {"valid_until": {"$ne": ""}}, limit=GET_LIMIT * 4)
    removed = filter_by_created_at(removed_candidates, start_dt, end_dt, field="valid_until")

    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "period": {
            "since": start_dt.isoformat() if start_dt else since,
            "until": end_dt.isoformat() if end_dt else until,
        },
    }


# ── 2. preference_evolution ─────────────────────────────
def preference_evolution(topic: str, limit: int = 20) -> list[dict]:
    """Chronological timeline of preference changes for a topic."""
    col_id = _get_collection_id("semantic_memory")
    if not col_id:
        return []

    emb = get_embedding(topic, prefix="query")
    try:
        resp = chroma_api("POST", _col_path(col_id, "query"), {
            "query_embeddings": [emb],
            "n_results": limit,
            "where": {"category": "preference"},
            "include": ["documents", "metadatas", "distances"],
        })
    except Exception:
        return []

    ids = (resp.get("ids") or [[]])[0]
    docs = (resp.get("documents") or [[]])[0]
    metas = (resp.get("metadatas") or [[]])[0]
    dists = (resp.get("distances") or [[]])[0]

    # Seed map with query results
    seen: dict[str, dict] = {}
    for i, mid in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        seen[mid] = {
            "id": mid,
            "date": (meta or {}).get("created_at", ""),
            "content": docs[i] if i < len(docs) else "",
            "superseded_by": (meta or {}).get("superseded_by", ""),
            "supersedes": (meta or {}).get("supersedes", ""),
            "confidence": round(1.0 - float(dists[i]), 3) if i < len(dists) else 0.0,
        }

    # Walk supersession chains to collect full history
    def _walk(start_id: str, field: str):
        current = start_id
        for _ in range(CHAIN_HOP_LIMIT):
            next_id = seen.get(current, {}).get(field, "")
            if not next_id or next_id in seen:
                break
            # Fetch the linked record
            try:
                r = chroma_api("POST", _col_path(col_id, "get"), {
                    "ids": [next_id],
                    "include": ["documents", "metadatas"],
                })
            except Exception:
                break
            r_ids = r.get("ids", [])
            if not r_ids:
                break
            r_meta = (r.get("metadatas") or [{}])[0] or {}
            r_doc = (r.get("documents") or [""])[0]
            seen[next_id] = {
                "id": next_id,
                "date": r_meta.get("created_at", ""),
                "content": r_doc,
                "superseded_by": r_meta.get("superseded_by", ""),
                "supersedes": r_meta.get("supersedes", ""),
                "confidence": 0.0,
            }
            current = next_id

    for mid in list(seen):
        _walk(mid, "superseded_by")  # forward
        _walk(mid, "supersedes")     # backward

    # Sort by created_at ascending
    timeline = sorted(seen.values(), key=lambda x: x.get("date") or "")
    return [{"date": e["date"], "content": e["content"], "superseded_by": e["superseded_by"],
             "confidence": e["confidence"], "id": e["id"]} for e in timeline]


# ── 3. what_changed_summary ─────────────────────────────
def what_changed_summary(days: int = 7) -> str:
    """Formatted markdown summary of knowledge changes over N days. No LLM — pure string formatting."""
    diff = knowledge_diff(f"{days}d")

    sections = {"Added": diff["added"], "Changed": diff["changed"], "Removed": diff["removed"]}
    period = diff["period"]
    lines = [f"# Knowledge changes ({period['since'][:10]} to {period['until'][:10]})", ""]

    total = sum(len(v) for v in sections.values())
    if total == 0:
        lines.append("No changes detected.")
        return "\n".join(lines)

    for heading, items in sections.items():
        if not items:
            continue
        lines.append(f"## {heading} ({len(items)})")
        lines.append("")
        # Group by category
        by_cat: dict[str, list[dict]] = {}
        for item in items:
            cat = item.get("category") or "uncategorized"
            by_cat.setdefault(cat, []).append(item)
        for cat, cat_items in sorted(by_cat.items()):
            lines.append(f"### {cat}")
            for item in cat_items:
                date = (item.get("created_at") or "")[:10]
                snippet = (item.get("content") or "")[:120]
                lines.append(f"- [{date}] {snippet}")
            lines.append("")

    return "\n".join(lines)
