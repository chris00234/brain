#!/opt/homebrew/bin/python3
"""Backfill calendar events from ChromaDB into Neo4j Event nodes.

Creates Event nodes with date properties for temporal graph queries.

Usage:
  backfill_calendar.py              # dry-run
  backfill_calendar.py --apply      # write to Neo4j
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain_core"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collect_events() -> list[dict]:
    """Query calendar collection from ChromaDB."""
    from indexer import chroma_api, _get_collection_id

    # Try personal (post-migration), fall back to calendar (pre-migration)
    col_id = _get_collection_id("personal") or _get_collection_id("calendar")
    if not col_id:
        print("neither personal nor calendar collection found")
        return []

    resp = chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get", {
        "limit": 200,
        "include": ["documents", "metadatas"],
        "where": {"type": "event"},
    })

    docs = resp.get("documents", [])
    metas = resp.get("metadatas", [])

    events = []
    for doc, meta in zip(docs, metas):
        if not doc or len(doc.strip()) < 10:
            continue
        title = meta.get("title", "")
        event_date = meta.get("event_date", meta.get("date", ""))
        service = meta.get("service", "")  # calendar name

        # Extract a date-qualified name to prevent collisions (e.g., multiple "lunch" events)
        raw_name = title.strip()[:60] if title else doc.strip().split("\n")[0][:60]
        raw_name = re.sub(r'[^\w\s가-힣-]', '', raw_name).strip().lower()
        if len(raw_name) < 3:
            continue
        # Qualify with date to prevent MERGE collisions
        name = f"{raw_name} {event_date}" if event_date else raw_name

        events.append({
            "name": name,
            "event_date": event_date,
            "calendar": service,
            "content": doc[:200],
        })
    return events


def backfill(apply: bool = False):
    events = collect_events()
    print(f"Found {len(events)} calendar events")

    if not apply:
        print("\n[DRY RUN] Would create:")
        for e in events[:10]:
            print(f"  Event: {e['name'][:50]}  date={e['event_date']}  cal={e['calendar']}")
        if len(events) > 10:
            print(f"  ... and {len(events) - 10} more")
        print("\nRun with --apply to write to Neo4j")
        return

    from neo4j_client import run_write
    now = _now_iso()

    created = 0
    for e in events:
        run_write(
            "MERGE (ev:Entity {name: $name}) "
            "ON CREATE SET ev.id = 'evt_' + left(randomUUID(), 12), "
            "  ev.entity_type = 'event', ev.first_seen_at = $now, "
            "  ev.last_seen_at = $now, ev.mention_count = 1, "
            "  ev.memory_class = 'ephemeral', ev.event_date = $date, "
            "  ev.calendar = $cal "
            "ON MATCH SET ev.last_seen_at = $now, ev.mention_count = ev.mention_count + 1",
            {"name": e["name"], "now": now, "date": e["event_date"], "cal": e["calendar"]},
        )

        # Link to Chris
        run_write(
            "MATCH (c:Entity {name: 'chris cho'}), (ev:Entity {name: $name}) "
            "MERGE (c)-[r:RELATES_TO {relationship: 'has_event'}]->(ev) "
            "ON CREATE SET r.weight = 0.5, r.co_occurrence_count = 1, r.created_at = $now",
            {"name": e["name"], "now": now},
        )
        created += 1

    print(f"Created {created} Event nodes")


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
