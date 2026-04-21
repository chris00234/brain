"""Temporal reasoning over semantic_memory — diff, evolution, and summaries.

Depends on:
  - temporal.parse_range(), filter_by_created_at()
  - vector_store.get_vector_store()
  - indexer.get_embedding()
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from datetime import UTC, datetime

from indexer import get_embedding
from temporal import filter_by_created_at, parse_range
from vector_store import get_vector_store

CHAIN_HOP_LIMIT = 10
GET_LIMIT = 500


def _get_filtered(where: dict, limit: int = GET_LIMIT) -> list[dict]:
    """Fetch semantic_memory docs with a where filter. Returns flat list of
    {id, content, category, created_at, valid_until}."""
    try:
        points = get_vector_store().get(
            "semantic_memory",
            filter=where,
            limit=limit,
            with_payload=True,
            with_documents=True,
        )
    except Exception:
        return []
    results = []
    for p in points:
        meta = p.payload or {}
        results.append(
            {
                "id": p.id,
                "content": p.document or "",
                "category": meta.get("category", ""),
                "created_at": meta.get("created_at", ""),
                "valid_until": meta.get("valid_until", ""),
            }
        )
    return results


# ── 1. knowledge_diff ───────────────────────────────────
def knowledge_diff(since: str, until: str = "now") -> dict:
    """Return {added, changed, removed, period} for semantic_memory in the given time range.

    Post-2026-04-12: ChromaDB 1.4.1 rejects string operands in $gte/$lt. We fetch
    by non-range filters only and apply the date filter Python-side on the results.
    """
    start_dt, end_dt = parse_range(since, until if until != "now" else None)
    if end_dt is None:
        end_dt = datetime.now(UTC)

    # Fetch candidates by non-date filter, then post-filter by created_at range.
    added_candidates = _get_filtered({"supersedes": {"$eq": ""}}, limit=GET_LIMIT * 4)
    added = filter_by_created_at(added_candidates, start_dt, end_dt, field="created_at")

    changed_candidates = _get_filtered({"supersedes": {"$ne": ""}}, limit=GET_LIMIT * 4)
    changed = filter_by_created_at(changed_candidates, start_dt, end_dt, field="created_at")

    # Removed: valid_until falls within the range. Fetch all with non-empty valid_until,
    # then post-filter. Safer than relying on ChromaDB for the range.
    removed_candidates = _get_filtered({"valid_until": {"$ne": ""}}, limit=GET_LIMIT * 4)
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
    store = get_vector_store()
    emb = get_embedding(topic, prefix="query")
    try:
        hits = store.query(
            "semantic_memory",
            vector=emb,
            k=limit,
            filter={"category": "preference"},
            with_payload=True,
        )
    except Exception:
        return []

    # Seed map with query results. score is already similarity (higher=better)
    # thanks to the ChromaStore distance→similarity flip.
    seen: dict[str, dict] = {}
    for h in hits:
        meta = h.payload or {}
        seen[h.id] = {
            "id": h.id,
            "date": meta.get("created_at", ""),
            "content": h.document or "",
            "superseded_by": meta.get("superseded_by", ""),
            "supersedes": meta.get("supersedes", ""),
            "confidence": round(h.score, 3),
        }

    # Walk supersession chains to collect full history
    def _walk(start_id: str, field: str):
        current = start_id
        for _ in range(CHAIN_HOP_LIMIT):
            next_id = seen.get(current, {}).get(field, "")
            if not next_id or next_id in seen:
                break
            try:
                points = store.get(
                    "semantic_memory",
                    ids=[next_id],
                    with_payload=True,
                    with_documents=True,
                )
            except Exception:
                break
            if not points:
                break
            p = points[0]
            r_meta = p.payload or {}
            seen[next_id] = {
                "id": next_id,
                "date": r_meta.get("created_at", ""),
                "content": p.document or "",
                "superseded_by": r_meta.get("superseded_by", ""),
                "supersedes": r_meta.get("supersedes", ""),
                "confidence": 0.0,
            }
            current = next_id

    for mid in list(seen):
        _walk(mid, "superseded_by")  # forward
        _walk(mid, "supersedes")  # backward

    # Sort by created_at ascending
    timeline = sorted(seen.values(), key=lambda x: x.get("date") or "")
    return [
        {
            "date": e["date"],
            "content": e["content"],
            "superseded_by": e["superseded_by"],
            "confidence": e["confidence"],
            "id": e["id"],
        }
        for e in timeline
    ]


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
