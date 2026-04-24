#!/usr/bin/env python3
"""Backfill co_mention RELATES_TO edges from shared MemoryAccess.

Every pair of entities that share ≥2 MemoryAccess nodes (i.e. both
mentioned by the same memory) should have a RELATES_TO edge between
them. The extract_and_store_entities path caps relations at 5/note,
so most co-mentions never become edges. This script backfills them
deterministically.

Idempotent via NOT EXISTS guard — won't duplicate existing edges.

Usage:
  backfill_co_mention.py [--min-co 2] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
from neo4j_client import run_query

DEFAULT_MIN_CO = 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-co", type=int, default=DEFAULT_MIN_CO, help="minimum shared-memory count")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pre_count = next(iter(run_query("MATCH ()-[r:RELATES_TO]-() RETURN count(DISTINCT r) AS c")))["c"]

    if args.dry_run:
        candidate_cypher = """
        MATCH (m:MemoryAccess)-[:MENTIONS]->(e1:Entity)
        MATCH (m)-[:MENTIONS]->(e2:Entity)
        WHERE elementId(e1) < elementId(e2)
        WITH e1, e2, count(DISTINCT m) AS co
        WHERE co >= $min_co AND NOT EXISTS { MATCH (e1)-[:RELATES_TO]-(e2) }
        RETURN count(*) AS would_create
        """
        r = list(run_query(candidate_cypher, {"min_co": args.min_co}))
        print(
            json.dumps(
                {
                    "status": "dry-run",
                    "pre_relates_to": pre_count,
                    "would_create": r[0]["would_create"],
                    "min_co": args.min_co,
                }
            )
        )
        return 0

    cypher = """
    MATCH (m:MemoryAccess)-[:MENTIONS]->(e1:Entity)
    MATCH (m)-[:MENTIONS]->(e2:Entity)
    WHERE elementId(e1) < elementId(e2)
    WITH e1, e2, count(DISTINCT m) AS co
    WHERE co >= $min_co AND NOT EXISTS { MATCH (e1)-[:RELATES_TO]-(e2) }
    MERGE (e1)-[r:RELATES_TO {relationship: 'co_mention'}]->(e2)
    ON CREATE SET r.id = randomUUID(),
                  r.weight = 0.1,
                  r.co_occurrence_count = co,
                  r.confidence = 0.5,
                  r.created_at = datetime(),
                  r.created_by = 'graph_backfill_co_mention',
                  r.valid_from = datetime(),
                  r.valid_to = ''
    RETURN count(r) AS created
    """
    r = list(run_query(cypher, {"min_co": args.min_co}))
    created = r[0]["created"]
    post_count = next(iter(run_query("MATCH ()-[r:RELATES_TO]-() RETURN count(DISTINCT r) AS c")))["c"]

    # Also reseal the orphan count for reporting
    orphan = next(iter(run_query("MATCH (e:Entity) WHERE NOT (e)-[]-() RETURN count(e) AS c")))["c"]
    no_relates = next(
        iter(run_query("MATCH (e:Entity) WHERE NOT (e)-[:RELATES_TO]-() RETURN count(e) AS c"))
    )["c"]

    print(
        json.dumps(
            {
                "status": "ok",
                "pre_relates_to": pre_count,
                "post_relates_to": post_count,
                "created": created,
                "min_co": args.min_co,
                "orphan_entities": orphan,
                "entities_without_relates_to": no_relates,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
