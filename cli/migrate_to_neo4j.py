#!/usr/bin/env python3
"""Migrate entity graph data from SQLite (autonomy.db) to Neo4j.

Also imports the OpenClaw ontology from graph.jsonl.
Idempotent (uses MERGE) — safe to re-run.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

from neo4j_client import run_query, run_write, is_healthy, ensure_schema

DB_PATH = Path("/Users/chrischo/server/brain/logs/autonomy.db")
ONTOLOGY_PATH = Path("/Users/chrischo/.openclaw/memory/ontology/graph.jsonl")


def migrate_sqlite():
    """Migrate entities, relations, and memory_access from SQLite."""
    if not DB_PATH.exists():
        print("No autonomy.db found, skipping SQLite migration")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Entities
    entities = [dict(r) for r in conn.execute("SELECT * FROM entities").fetchall()]
    print(f"SQLite entities: {len(entities)}")
    if entities:
        run_write(
            "UNWIND $entities AS e "
            "MERGE (n:Entity {name: e.name}) "
            "ON CREATE SET n.id = e.id, n.entity_type = e.entity_type, "
            "  n.first_seen_at = e.first_seen_at, n.last_seen_at = e.last_seen_at, "
            "  n.mention_count = e.mention_count "
            "ON MATCH SET n.last_seen_at = CASE WHEN e.last_seen_at > n.last_seen_at THEN e.last_seen_at ELSE n.last_seen_at END, "
            "  n.mention_count = CASE WHEN e.mention_count > n.mention_count THEN e.mention_count ELSE n.mention_count END",
            {"entities": entities},
        )

    # Relations
    relations = [dict(r) for r in conn.execute(
        "SELECT r.*, s.name AS src_name, t.name AS tgt_name "
        "FROM entity_relations r "
        "JOIN entities s ON r.source_entity = s.id "
        "JOIN entities t ON r.target_entity = t.id"
    ).fetchall()]
    print(f"SQLite relations: {len(relations)}")
    for rel in relations:
        run_write(
            "MATCH (s:Entity {name: $src}), (t:Entity {name: $tgt}) "
            "WHERE s <> t "
            "MERGE (s)-[r:RELATES_TO {id: $rid}]->(t) "
            "ON CREATE SET r.relationship = $rel_type, r.confidence = $conf, "
            "  r.created_at = $created, r.source_memory_id = $mid",
            {
                "src": rel["src_name"], "tgt": rel["tgt_name"],
                "rid": rel["id"], "rel_type": rel["relationship"],
                "conf": rel["confidence"], "created": rel["created_at"],
                "mid": rel.get("source_memory_id", ""),
            },
        )

    # Memory access
    access = [dict(r) for r in conn.execute("SELECT * FROM memory_access").fetchall()]
    print(f"SQLite memory_access: {len(access)}")
    if access:
        run_write(
            "UNWIND $rows AS a "
            "MERGE (m:MemoryAccess {memory_id: a.memory_id}) "
            "ON CREATE SET m.access_count = a.access_count, "
            "  m.first_accessed_at = a.first_accessed_at, m.last_accessed_at = a.last_accessed_at "
            "ON MATCH SET m.access_count = CASE WHEN a.access_count > m.access_count THEN a.access_count ELSE m.access_count END, "
            "  m.last_accessed_at = CASE WHEN a.last_accessed_at > m.last_accessed_at THEN a.last_accessed_at ELSE m.last_accessed_at END",
            {"rows": access},
        )

    conn.close()


def migrate_ontology():
    """Import OpenClaw ontology from graph.jsonl."""
    if not ONTOLOGY_PATH.exists():
        print("No ontology graph.jsonl found, skipping")
        return

    entities = []
    relations = []
    for line in ONTOLOGY_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("op") == "create" and "entity" in record:
            ent = record["entity"]
            props = ent.get("properties", {})
            entities.append({
                "id": ent["id"],
                "name": props.get("name", ent["id"]).lower(),
                "entity_type": ent.get("type", "concept").lower(),
            })
        elif record.get("op") == "relate":
            relations.append({"from_id": record["from"], "to_id": record["to"]})

    print(f"Ontology entities: {len(entities)}")
    if entities:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run_write(
            "UNWIND $entities AS e "
            "MERGE (n:Entity {name: e.name}) "
            "ON CREATE SET n.id = e.id, n.entity_type = e.entity_type, "
            "  n.first_seen_at = $now, n.last_seen_at = $now, n.mention_count = 1",
            {"entities": entities, "now": now},
        )

    # Resolve ontology relations (from/to are entity IDs, need to look up names)
    id_to_name = {e["id"]: e["name"] for e in entities}
    print(f"Ontology relations: {len(relations)}")
    for rel in relations:
        src = id_to_name.get(rel["from_id"])
        tgt = id_to_name.get(rel["to_id"])
        if src and tgt and src != tgt:
            run_write(
                "MATCH (s:Entity {name: $src}), (t:Entity {name: $tgt}) "
                "MERGE (s)-[:RELATES_TO {relationship: 'ontology_related', confidence: 0.8, created_at: $now}]->(t)",
                {"src": src, "tgt": tgt, "now": now},
            )


def verify():
    """Verify migration counts."""
    stats = run_query(
        "MATCH (e:Entity) WITH count(e) AS entities "
        "OPTIONAL MATCH ()-[r]->() WITH entities, count(r) AS relations "
        "OPTIONAL MATCH (m:MemoryAccess) "
        "RETURN entities, relations, count(m) AS access"
    )
    if stats:
        s = stats[0]
        print(f"\nNeo4j counts: entities={s['entities']}, relations={s['relations']}, memory_access={s['access']}")
    else:
        print("\nVerification query returned no results")


if __name__ == "__main__":
    if not is_healthy():
        print("ERROR: Neo4j is not reachable at bolt://127.0.0.1:7687")
        sys.exit(1)

    print("Ensuring schema...")
    ensure_schema()

    print("\n--- Migrating SQLite data ---")
    migrate_sqlite()

    print("\n--- Migrating Ontology ---")
    migrate_ontology()

    print("\n--- Verifying ---")
    verify()

    print("\nMigration complete.")
