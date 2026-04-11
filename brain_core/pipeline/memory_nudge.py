#!/opt/homebrew/bin/python3
"""Weekly memory nudge — Jenna reviews recent memories for promotion/archival.

Classifies memories as durable/obsolete/pattern, then takes real action:
- durable  → mark metadata.promotion_candidate=true (for canonical_pipeline to pick up)
- obsolete → mark memory_class=obsolete (hidden from default search)
- pattern  → store as new semantic_memory with category=preference
"""
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from openclaw_dispatch import dispatch_with_schema
from http_pool import http_json
from search import get_collections

OUT_FILE = Path("/Users/chrischo/server/brain/logs/memory-nudge-latest.json")

CHROMA_URL = "http://127.0.0.1:8000"
CHROMA_API = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections"

PROMPT_TEMPLATE = """Review these recent memories from Chris's brain. For each, classify as:
- durable: should be promoted to canonical knowledge
- obsolete: no longer useful, archive
- pattern: reveals a reusable rule or behavior

Memories:
{memories}
"""

SCHEMA = '{"durable": [<memory_id>, ...], "obsolete": [<memory_id>, ...], "patterns": [{"rule": "...", "from": [<memory_id>, ...]}]}'


def fetch_recent(days: int = 7) -> list[dict]:
    cols = get_collections()
    sem_id = cols.get("semantic_memory")
    if not sem_id:
        return []
    resp = http_json(
        "POST",
        f"{CHROMA_API}/{sem_id}/get",
        {"limit": 200, "include": ["documents", "metadatas"]},
    )
    ids = resp.get("ids", [])
    docs = resp.get("documents", []) or []
    metas = resp.get("metadatas", []) or []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = []
    for i, d, m in zip(ids, docs, metas):
        m = m or {}
        if (m.get("memory_class") or "") == "obsolete":
            continue  # skip already-obsolete
        ts = m.get("created_at", "")
        if ts >= cutoff:
            recent.append({
                "id": i,
                "content": (d or "")[:200],
                "category": m.get("category", "other"),
            })
    return recent[:50]  # cap for prompt size


def mark_obsolete(sem_id: str, memory_ids: list[str]) -> int:
    if not memory_ids:
        return 0
    try:
        http_json(
            "POST",
            f"{CHROMA_API}/{sem_id}/update",
            {
                "ids": memory_ids,
                "metadatas": [{"memory_class": "obsolete"} for _ in memory_ids],
            },
        )
        return len(memory_ids)
    except Exception as e:
        print(f"mark_obsolete failed: {e}")
        return 0


def mark_promotion_candidate(sem_id: str, memory_ids: list[str]) -> int:
    if not memory_ids:
        return 0
    try:
        http_json(
            "POST",
            f"{CHROMA_API}/{sem_id}/update",
            {
                "ids": memory_ids,
                "metadatas": [
                    {"promotion_candidate": "true", "promotion_flagged_at": datetime.now(timezone.utc).isoformat()}
                    for _ in memory_ids
                ],
            },
        )
        return len(memory_ids)
    except Exception as e:
        print(f"mark_promotion_candidate failed: {e}")
        return 0


def main():
    recent = fetch_recent(7)
    if not recent:
        print("No recent memories to review")
        return 0

    cols = get_collections()
    sem_id = cols.get("semantic_memory")
    if not sem_id:
        print("semantic_memory collection not found")
        return 1

    memory_text = "\n".join(
        f"- [{m['id']}] ({m['category']}) {m['content']}" for m in recent
    )
    prompt = PROMPT_TEMPLATE.format(memories=memory_text)

    parsed = dispatch_with_schema(
        agent="jenna",
        message=prompt,
        schema_description=SCHEMA,
        thinking="low",
        timeout=120,
        max_retries=1,
    )
    if parsed is None:
        print("Dispatch failed or JSON parse failed after retries")
        return 1

    # Validate recent IDs exist — avoid marking IDs that don't belong to the batch
    recent_id_set = {m["id"] for m in recent}
    durable_ids = [i for i in parsed.get("durable", []) if isinstance(i, str) and i in recent_id_set]
    obsolete_ids = [i for i in parsed.get("obsolete", []) if isinstance(i, str) and i in recent_id_set]
    patterns = parsed.get("patterns", []) or []

    # Act on classifications
    promoted_count = mark_promotion_candidate(sem_id, durable_ids)
    archived_count = mark_obsolete(sem_id, obsolete_ids)

    report = {
        "timestamp": datetime.now().isoformat(),
        "reviewed_count": len(recent),
        "durable": durable_ids,
        "obsolete": obsolete_ids,
        "patterns": patterns,
        "actions": {
            "promotion_flagged": promoted_count,
            "archived": archived_count,
            "patterns_stored": 0,  # TODO: store patterns as new memories
        },
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(report, indent=2))

    print(f"Reviewed {len(recent)} memories")
    print(f"  durable (promotion flagged): {promoted_count}")
    print(f"  obsolete (archived): {archived_count}")
    print(f"  patterns: {len(patterns)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
