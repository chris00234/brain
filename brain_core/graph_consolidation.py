"""brain_core/graph_consolidation.py — nightly graph "sleep" cycle.

Implements biological sleep-inspired memory consolidation:
1. Synaptic downscaling (global decay based on Ebbinghaus curve)
2. Synaptic pruning (remove dead connections)
3. LTP promotion (consolidate strong short-term memories)
4. Cluster detection (find densely connected subgraphs)

Runs at 2:50am via scheduler, after brain_reflect (2:45am).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.graph_consolidation")


def run_consolidation() -> dict:
    """Execute all four phases of graph consolidation."""
    try:
        from neo4j_client import is_healthy, run_query

        if not is_healthy():
            return {"status": "skip", "reason": "neo4j unavailable"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    results = {}

    # Phase 1: Synaptic downscaling (Ebbinghaus decay)
    try:
        decayed = run_query(
            "MATCH ()-[r:RELATES_TO]->() "
            "WHERE (r.potentiation IS NULL OR r.potentiation <> 'ltp') "
            "  AND r.last_confirmed_at IS NOT NULL "
            "WITH r, "
            "  duration.between(datetime(r.last_confirmed_at), datetime()).days AS days_idle, "
            "  CASE "
            "    WHEN coalesce(r.co_occurrence_count, 0) >= 10 THEN 180.0 "
            "    WHEN coalesce(r.co_occurrence_count, 0) >= 5 THEN 90.0 "
            "    WHEN coalesce(r.co_occurrence_count, 0) >= 2 THEN 45.0 "
            "    ELSE 20.0 "
            "  END AS stability "
            "WITH r, days_idle, stability, "
            "  exp(-1.0 * toFloat(days_idle) / stability) AS retention "
            "WHERE retention < 0.99 "
            "SET r.weight = coalesce(r.weight, 0.5) * retention, "
            "    r.last_decay_at = datetime().epochMillis "
            "RETURN count(r) AS decayed"
        )
        results["phase1_decayed"] = decayed[0]["decayed"] if decayed else 0
    except Exception as e:
        results["phase1_error"] = str(e)[:200]
        log.warning("Phase 1 (decay) failed: %s", e)

    # Phase 2: Synaptic pruning (remove dead connections)
    try:
        # Count first, then delete (Neo4j doesn't allow DELETE + RETURN in same clause)
        to_prune = run_query(
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "WHERE coalesce(r.weight, 0.5) < 0.01 "
            "  AND coalesce(r.co_occurrence_count, 0) < 3 "
            "  AND (r.potentiation IS NULL OR r.potentiation <> 'ltp') "
            "RETURN count(r) AS cnt"
        )
        prune_count = to_prune[0]["cnt"] if to_prune else 0
        if prune_count > 0:
            from neo4j_client import run_write

            run_write(
                "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
                "WHERE coalesce(r.weight, 0.5) < 0.01 "
                "  AND coalesce(r.co_occurrence_count, 0) < 3 "
                "  AND (r.potentiation IS NULL OR r.potentiation <> 'ltp') "
                "DELETE r"
            )
        results["phase2_pruned_rels"] = prune_count

        # Prune orphan ephemeral entities
        orphan_count_result = run_query(
            "MATCH (e:Entity) "
            "WHERE NOT (e)--() "
            "  AND e.mention_count < 3 "
            "  AND e.memory_class = 'ephemeral' "
            "RETURN count(e) AS cnt"
        )
        orphan_count = orphan_count_result[0]["cnt"] if orphan_count_result else 0
        if orphan_count > 0:
            from neo4j_client import run_write

            run_write(
                "MATCH (e:Entity) "
                "WHERE NOT (e)--() "
                "  AND e.mention_count < 3 "
                "  AND e.memory_class = 'ephemeral' "
                "DELETE e"
            )
        results["phase2_pruned_orphans"] = orphan_count
    except Exception as e:
        results["phase2_error"] = str(e)[:200]
        log.warning("Phase 2 (prune) failed: %s", e)

    # Phase 3: LTP promotion (consolidate strong short-term memories)
    try:
        promoted = run_query(
            "MATCH ()-[r:RELATES_TO]->() "
            "WHERE coalesce(r.co_occurrence_count, 0) >= 5 "
            "  AND coalesce(r.weight, 0) >= 0.3 "
            "  AND (r.potentiation IS NULL OR r.potentiation = 'stp') "
            "SET r.potentiation = 'ltp', r.ltp_at = datetime().epochMillis "
            "RETURN count(r) AS promoted"
        )
        results["phase3_promoted_rels"] = promoted[0]["promoted"] if promoted else 0

        # Promote high-mention entities
        ent_promoted = run_query(
            "MATCH (e:Entity) "
            "WHERE e.mention_count >= 10 AND e.memory_class = 'ephemeral' "
            "SET e.memory_class = 'seasonal' "
            "RETURN count(e) AS promoted"
        )
        results["phase3_promoted_entities"] = ent_promoted[0]["promoted"] if ent_promoted else 0
    except Exception as e:
        results["phase3_error"] = str(e)[:200]
        log.warning("Phase 3 (LTP) failed: %s", e)

    # Phase 4: Cluster detection (find strongly connected subgraphs)
    try:
        triangles = run_query(
            "MATCH (a:Entity)-[r1:RELATES_TO]-(b:Entity)-[r2:RELATES_TO]-(c:Entity)-[r3:RELATES_TO]-(a) "
            "WHERE coalesce(r1.weight, 0.5) > 0.2 "
            "  AND coalesce(r2.weight, 0.5) > 0.2 "
            "  AND coalesce(r3.weight, 0.5) > 0.2 "
            "  AND id(a) < id(b) AND id(b) < id(c) "
            "RETURN [a.name, b.name, c.name] AS cluster, "
            "  (coalesce(r1.weight,0.5) + coalesce(r2.weight,0.5) + coalesce(r3.weight,0.5)) / 3.0 AS strength "
            "ORDER BY strength DESC LIMIT 10"
        )
        results["phase4_clusters"] = [
            {"entities": t["cluster"], "strength": round(t["strength"], 2)} for t in triangles
        ]
    except Exception as e:
        results["phase4_error"] = str(e)[:200]
        log.warning("Phase 4 (clusters) failed: %s", e)

    # Final stats
    try:
        from neo4j_client import get_stats

        results["final_stats"] = get_stats()
    except Exception:
        pass

    results["status"] = "ok"
    log.info("graph consolidation complete: %s", json.dumps(results))
    return results


if __name__ == "__main__":
    result = run_consolidation()
    print(json.dumps(result, indent=2))
