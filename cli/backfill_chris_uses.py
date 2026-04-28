#!/usr/bin/env python3
"""Phase G3 backfill — seed `(chris)-[:RELATES_TO {relationship:'uses'}]->(t)`
edges in Neo4j for the tools/services Chris is known to actively run.

Why this exists:
  /recall/v2?exclude_already_used=true filters results that mention any
  entity Chris already uses, but it depends on Neo4j carrying those edges.
  The organic path is `entity_graph.extract_and_store_entities` (background
  Sage extraction on every atom >40 chars), which populates over time. This
  script gives the filter a useful starting set right after Phase 1 ships.

Idempotent: every MERGE matches on the entity name, so re-running tops up
mention_count + last_seen_at without creating duplicates. Safe to run again
when the curated list grows.

Usage:
  /Users/chrischo/server/brain/.venv/bin/python cli/backfill_chris_uses.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# (canonical lowercased name, entity_type, memory_class)
# memory_class follows _neo4j_store_entities: service/tool/agent → permanent
# (never decay), framework/library → seasonal (slow decay).
# Curated to entities that show up in Sage tool-recommendation queries —
# universal infra (nginx, docker, cloudflared) is intentionally excluded
# because Sage isn't going to recommend alternatives for those.
TOOLS: list[tuple[str, str, str]] = [
    # Self-hosted services in active rotation
    ("beszel", "service", "permanent"),
    ("uptime kuma", "service", "permanent"),
    ("glance", "service", "permanent"),
    ("searxng", "service", "permanent"),
    ("vaultwarden", "service", "permanent"),
    ("ghost", "service", "permanent"),
    ("couchdb", "service", "permanent"),
    ("minio", "service", "permanent"),
    ("filebrowser", "service", "permanent"),
    ("loki", "service", "permanent"),
    ("promtail", "service", "permanent"),
    # Brain stack backends
    ("qdrant", "service", "permanent"),
    ("neo4j", "service", "permanent"),
    ("ollama", "service", "permanent"),
    ("crawl4ai", "service", "permanent"),
    # Web/dev frameworks Chris standardizes on
    ("fastapi", "framework", "seasonal"),
    ("next.js", "framework", "seasonal"),
    ("react", "library", "seasonal"),
    ("typescript", "language", "seasonal"),
    ("tailwind", "framework", "seasonal"),
    ("shadcn/ui", "library", "seasonal"),
    # MCP / dev agents
    ("context7", "tool", "permanent"),
    ("claude code", "agent", "permanent"),
    ("openclaw", "agent", "permanent"),
    ("codex", "agent", "permanent"),
]

SOURCE_TAG = "backfill_chris_uses_2026_04_28"


def main() -> int:
    try:
        from neo4j_client import is_healthy, run_query, run_write
    except Exception as exc:
        print(f"FATAL: neo4j_client import failed: {exc}", file=sys.stderr)
        return 2

    if not is_healthy():
        print("FATAL: Neo4j not reachable at bolt://127.0.0.1:7687", file=sys.stderr)
        return 1

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Ensure the Chris speaker node exists. Mirrors _neo4j_store_entities
    # 's MERGE pattern so the schema stays consistent (id-on-create, name as
    # MERGE key, mention_count + last_seen_at maintenance).
    run_query(
        "MERGE (e:Entity {name: 'chris'}) "
        "ON CREATE SET e.id = 'ent_chris_canonical', e.entity_type = 'person', "
        "  e.first_seen_at = $now, e.last_seen_at = $now, e.mention_count = 1, "
        "  e.memory_class = 'permanent' "
        "ON MATCH SET e.last_seen_at = $now, "
        "  e.mention_count = coalesce(e.mention_count, 0) + 1 "
        "RETURN e.id AS id",
        {"now": now},
    )

    created_entities = 0
    refreshed_entities = 0
    created_edges = 0
    refreshed_edges = 0

    for name, etype, mem_class in TOOLS:
        eid = f"ent_uses_{name.replace(' ', '_').replace('.', '').replace('/', '_')}"

        ent_result = run_query(
            "MERGE (e:Entity {name: $name}) "
            "ON CREATE SET e.id = $id, e.entity_type = $type, e.first_seen_at = $now, "
            "  e.last_seen_at = $now, e.mention_count = 1, e.memory_class = $mc "
            "ON MATCH SET e.last_seen_at = $now, "
            "  e.mention_count = coalesce(e.mention_count, 0) + 1, "
            "  e.memory_class = coalesce(e.memory_class, $mc) "
            "RETURN e.id AS id, e.mention_count AS mc",
            {"name": name, "id": eid, "type": etype, "now": now, "mc": mem_class},
        )
        if ent_result and ent_result[0].get("mc", 0) == 1:
            created_entities += 1
        else:
            refreshed_entities += 1

        # Hebbian-pattern relation matching the live extractor — a 0.5 weight
        # marks this as a confident curated fact rather than a 0.1 LLM-extracted
        # one, but it still saturates via the same ON MATCH formula on
        # subsequent runs.
        rid = f"rel_chris_uses_{name.replace(' ', '_').replace('.', '').replace('/', '_')}"
        rel_result = run_write(
            "MATCH (s:Entity {name: 'chris'}) "
            "MATCH (t:Entity {name: $name}) "
            "WHERE s <> t "
            "MERGE (s)-[r:RELATES_TO {relationship: 'uses'}]->(t) "
            "ON CREATE SET r.id = $rid, r.weight = 0.5, r.co_occurrence_count = 1, "
            "  r.confidence = 0.9, r.created_at = $now, r.source_memory_id = $src, "
            "  r.valid_from = $now, r.valid_to = '' "
            "ON MATCH SET r.co_occurrence_count = coalesce(r.co_occurrence_count, 0) + 1, "
            "  r.weight = CASE WHEN coalesce(r.weight, 0.5) + (0.1 * (1.0 - coalesce(r.weight, 0.5))) > 1.0 "
            "    THEN 1.0 ELSE coalesce(r.weight, 0.5) + (0.1 * (1.0 - coalesce(r.weight, 0.5))) END, "
            "  r.last_confirmed_at = $now",
            {"name": name, "rid": rid, "now": now, "src": SOURCE_TAG},
        )
        # run_write returns counters (writes_summary). Treat existence as
        # success — we'll verify with a count query below.
        if rel_result is not None:
            created_edges += 1

    # Verify final state.
    verify = run_query(
        "MATCH (s:Entity {name: 'chris'})-[r:RELATES_TO {relationship: 'uses'}]->(t:Entity) "
        "RETURN count(t) AS n"
    )
    final_count = verify[0]["n"] if verify else 0

    refreshed_edges = max(0, len(TOOLS) - created_edges)
    print(
        "Backfill complete. "
        f"Entities: created≈{created_entities}, refreshed≈{refreshed_entities}. "
        f"Edges merged: {created_edges + refreshed_edges} "
        f"(Neo4j now reports {final_count} chris-uses-* edges)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
