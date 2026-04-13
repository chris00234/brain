#!/opt/homebrew/bin/python3
"""brain_core/pipeline/memory_consolidation.py — Nightly memory tier consolidation.

Phase 1D: Three-tier memory (episodic → semantic → obsolete).

Rules (calibrated 2026-04-11):
  episodic → semantic:  age ≥ 3 days AND utility_score ≥ 0.3
  episodic → obsolete:  age ≥ 7 days AND utility_score < 0.2
  semantic → obsolete:  age ≥ 180 days AND utility_score < 0.1

Utility: Neo4j MemoryAccess.utility_score, with access_count fallback from ChromaDB.
Runs nightly at 3:45am.
"""
from __future__ import annotations

import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from http_pool import http_json
from search import get_collections

CHROMA_URL = "http://127.0.0.1:8000"
CHROMA_API = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections"
BATCH_SIZE = 50


def _utility_scores(memory_ids: list[str]) -> dict[str, float]:
    """Fetch utility scores from Neo4j for a batch of memory IDs."""
    if not memory_ids:
        return {}
    try:
        from neo4j_client import run_query
        rows = run_query(
            "UNWIND $ids AS mid "
            "OPTIONAL MATCH (m:MemoryAccess {memory_id: mid}) "
            "RETURN mid, coalesce(m.utility_score, 0.5) AS score",
            {"ids": memory_ids}
        )
        return {r["mid"]: float(r["score"]) for r in rows}
    except Exception:
        return {mid: 0.5 for mid in memory_ids}


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.rstrip("Z")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def consolidate() -> dict:
    cols = get_collections()
    sem_col_id = cols.get("semantic_memory")
    if not sem_col_id:
        return {"error": "semantic_memory collection not found"}

    # Paginate the full scan in 1000-doc pages. A single 50k fetch would
    # allocate the full body multiple times (FastAPI response → subprocess
    # memory → list comprehensions) and grow linearly with memory size.
    ids: list[str] = []
    metas: list[dict] = []
    PAGE = 1000
    offset = 0
    while True:
        try:
            resp = http_json(
                "POST",
                f"{CHROMA_API}/{sem_col_id}/get",
                {"limit": PAGE, "offset": offset, "include": ["metadatas"]}
            )
        except Exception as e:
            return {"error": f"fetch failed at offset={offset}: {e}"}
        page_ids = resp.get("ids", [])
        if not page_ids:
            break
        ids.extend(page_ids)
        metas.extend(resp.get("metadatas", []) or [])
        if len(page_ids) < PAGE:
            break
        offset += PAGE

    if not ids:
        return {"status": "empty", "total": 0}

    now = datetime.now(timezone.utc)
    promoted = 0
    demoted_episodic = 0
    demoted_semantic = 0

    # Fetch utility scores in batches
    utility: dict[str, float] = {}
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        utility.update(_utility_scores(batch))

    # Enrich utility with access_count fallback from ChromaDB metadata
    for mid, meta in zip(ids, metas):
        meta = meta or {}
        access_count = int(meta.get("access_count") or 0)
        if mid not in utility or utility[mid] == 0.5:  # 0.5 = Neo4j fallback
            utility[mid] = min(1.0, 0.3 + (access_count * 0.1))

    # Collect updates
    updates_batch: list[tuple[str, dict]] = []

    print(f"[consolidate] scanning {len(ids)} memories")
    for mid, meta in zip(ids, metas):
        meta = meta or {}
        current_class = meta.get("memory_class") or "episodic"
        if current_class == "obsolete":
            continue  # already terminal

        created_at = _parse_iso(meta.get("created_at", ""))
        if not created_at:
            continue
        age_days = (now - created_at).days
        u = utility.get(mid, 0.3)
        access_count = int(meta.get("access_count") or 0)

        new_class = None
        trust_score = None
        if current_class == "episodic":
            if age_days >= 3 and u >= 0.3:
                new_class = "semantic"
                old_trust = float(meta.get("trust_score") or 0.5)
                trust_score = min(1.0, old_trust + 0.1)
                promoted += 1
            elif age_days >= 7 and u < 0.2:
                new_class = "obsolete"
                trust_score = 0.2
                demoted_episodic += 1
        elif current_class == "semantic":
            if age_days >= 180 and u < 0.1:
                new_class = "obsolete"
                trust_score = 0.2
                demoted_semantic += 1

        if new_class:
            update_meta = {"memory_class": new_class}
            if trust_score is not None:
                update_meta["trust_score"] = str(trust_score)
            print(f"  {mid[:30]} age={age_days}d utility={u:.2f} access={access_count} {current_class}->{new_class}")
            updates_batch.append((mid, update_meta))
            if len(updates_batch) >= BATCH_SIZE:
                _apply_updates(sem_col_id, updates_batch)
                updates_batch = []

    # Flush
    if updates_batch:
        _apply_updates(sem_col_id, updates_batch)

    return {
        "status": "ok",
        "total": len(ids),
        "promoted_episodic_to_semantic": promoted,
        "demoted_episodic_to_obsolete": demoted_episodic,
        "demoted_semantic_to_obsolete": demoted_semantic,
        "timestamp": now.isoformat(),
    }


def _apply_updates(col_id: str, updates: list[tuple[str, dict]]):
    """Apply a batch of metadata updates to ChromaDB."""
    if not updates:
        return
    ids = [u[0] for u in updates]
    metas = [u[1] for u in updates]
    try:
        http_json(
            "POST",
            f"{CHROMA_API}/{col_id}/update",
            {"ids": ids, "metadatas": metas},
        )
    except Exception as e:
        print(f"update batch failed: {e}")


if __name__ == "__main__":
    result = consolidate()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "ok" else 1)
