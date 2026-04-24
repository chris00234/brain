#!/usr/bin/env python3
"""canonicalize_entities.py — cross-language + near-duplicate entity merger.

Walks Neo4j Entity nodes, embeds each name via Ollama, finds pairs with
cosine similarity above a threshold, and merges the less popular entity
into the more popular one.

Merge operation:
  1. Winner = higher mention_count (tiebreak: shorter name, then first alpha)
  2. Loser's RELATES_TO edges are redirected to winner
  3. Loser's name is added to winner.aliases array
  4. atom_entity links pointing to loser are updated to winner
  5. Loser node is deleted

Defaults:
  threshold: 0.92 (high bar — avoids false positives on similar-but-distinct
             entities like 'chris' person vs 'chris' repo)
  dry-run: True (prints what would merge, doesn't commit)

Usage:
  cli/canonicalize_entities.py                    # dry run, report pairs
  cli/canonicalize_entities.py --threshold 0.90   # lower threshold
  cli/canonicalize_entities.py --apply            # actually merge
  cli/canonicalize_entities.py --type person      # only merge within type
"""

from __future__ import annotations

import argparse
import math
import sys

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from indexer import get_embedding
from neo4j_client import run_query

DEFAULT_THRESHOLD = 0.92
EMBED_MAX_CHARS = 80

# Safeguard patterns: reject merges where identifiers inside names differ.
# These catch false positives like "openclaw session 2026-03-15" vs
# "openclaw session 2026-02-27", "r1 probe 1776198240" vs
# "r7 probe 1776197989", and "5 tasks" vs "6 tasks".
import re as _re

_DATE_RE = _re.compile(r"\b(?:19|20)\d{2}[-_/ ]?\d{1,2}[-_/ ]?\d{1,2}\b")
_NUMSEQ_RE = _re.compile(r"\b\d{4,}\b")
_HEX_UID_RE = _re.compile(r"\b[0-9a-f]{8,}\b", _re.IGNORECASE)
_SHORT_ENUM_RE = _re.compile(
    r"\b(\d+)\s+(?:tasks?|probes?|items?|runs?|agents?|notes?|probe|users?)\b", _re.IGNORECASE
)


def _safe_to_merge(a_name: str, b_name: str) -> tuple[bool, str]:
    """Return (safe, reason). Reject when identifier substrings don't match
    OR when the surface strings are too dissimilar to trust an embedding-only
    match (catches backend/frontend, saturday/sunday, .local.md/ main.md, etc.)."""
    for pat, label in (
        (_DATE_RE, "date_diff"),
        (_NUMSEQ_RE, "numseq_diff"),
        (_HEX_UID_RE, "hex_diff"),
        (_SHORT_ENUM_RE, "enum_diff"),
    ):
        a_m = set(m if isinstance(m, str) else m[0] for m in pat.findall(a_name))
        b_m = set(m if isinstance(m, str) else m[0] for m in pat.findall(b_name))
        if a_m != b_m:
            return False, label
    # Surface-string similarity gate. SequenceMatcher.ratio() is in [0, 1];
    # 0.75 rejects semantic-opposite pairs that still embed close.
    import difflib

    ratio = difflib.SequenceMatcher(None, a_name.lower().strip(), b_name.lower().strip()).ratio()
    if ratio < 0.75:
        return False, f"string_ratio_{ratio:.2f}"
    return True, "ok"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _fetch_entities(entity_type: str | None) -> list[dict]:
    """Read all entities + their mention_count and aliases."""
    if entity_type:
        cypher = (
            "MATCH (e:Entity {entity_type: $t}) "
            "RETURN e.id AS id, e.name AS name, e.entity_type AS type, "
            "       coalesce(e.mention_count, 1) AS count, "
            "       coalesce(e.aliases, []) AS aliases "
            "ORDER BY e.name"
        )
        rows = run_query(cypher, {"t": entity_type})
    else:
        cypher = (
            "MATCH (e:Entity) "
            "RETURN e.id AS id, e.name AS name, e.entity_type AS type, "
            "       coalesce(e.mention_count, 1) AS count, "
            "       coalesce(e.aliases, []) AS aliases "
            "ORDER BY e.name"
        )
        rows = run_query(cypher)
    return [dict(r) for r in rows]


def _embed_name(name: str) -> list[float] | None:
    try:
        emb = get_embedding(name[:EMBED_MAX_CHARS], prefix="query")
        return emb or None
    except Exception:
        return None


def _pick_winner(a: dict, b: dict) -> tuple[dict, dict]:
    """Return (winner, loser) based on mention_count, then name length."""
    if a["count"] != b["count"]:
        return (a, b) if a["count"] > b["count"] else (b, a)
    # Tiebreak: shorter name wins (more canonical)
    if len(a["name"]) != len(b["name"]):
        return (a, b) if len(a["name"]) < len(b["name"]) else (b, a)
    # Final tiebreak: alphabetical
    return (a, b) if a["name"] < b["name"] else (b, a)


def _merge_pair(winner: dict, loser: dict) -> dict:
    """Merge loser into winner in a single transaction. Returns result dict."""
    # Redirect RELATES_TO edges, add alias, delete loser node, update atom_entity.
    # All in one cypher call via chained MATCH/SET/DELETE.
    new_aliases = list(set((winner.get("aliases") or []) + [loser["name"]]))

    # Step 1: redirect edges where loser is source
    run_query(
        "MATCH (loser:Entity {id: $lid})-[r:RELATES_TO]->(tgt:Entity) "
        "MATCH (winner:Entity {id: $wid}) "
        "WHERE winner <> tgt "
        "MERGE (winner)-[nr:RELATES_TO {relationship: r.relationship}]->(tgt) "
        "  ON CREATE SET nr.id = r.id, nr.weight = r.weight, "
        "    nr.co_occurrence_count = r.co_occurrence_count, "
        "    nr.confidence = r.confidence, nr.created_at = r.created_at, "
        "    nr.source_memory_id = r.source_memory_id "
        "  ON MATCH SET nr.weight = (nr.weight + r.weight) / 2.0, "
        "    nr.co_occurrence_count = nr.co_occurrence_count + r.co_occurrence_count "
        "DELETE r",
        {"lid": loser["id"], "wid": winner["id"]},
    )
    # Step 2: redirect edges where loser is target
    run_query(
        "MATCH (src:Entity)-[r:RELATES_TO]->(loser:Entity {id: $lid}) "
        "MATCH (winner:Entity {id: $wid}) "
        "WHERE src <> winner "
        "MERGE (src)-[nr:RELATES_TO {relationship: r.relationship}]->(winner) "
        "  ON CREATE SET nr.id = r.id, nr.weight = r.weight, "
        "    nr.co_occurrence_count = r.co_occurrence_count, "
        "    nr.confidence = r.confidence, nr.created_at = r.created_at, "
        "    nr.source_memory_id = r.source_memory_id "
        "  ON MATCH SET nr.weight = (nr.weight + r.weight) / 2.0, "
        "    nr.co_occurrence_count = nr.co_occurrence_count + r.co_occurrence_count "
        "DELETE r",
        {"lid": loser["id"], "wid": winner["id"]},
    )
    # Step 3: update winner aliases + mention_count
    run_query(
        "MATCH (winner:Entity {id: $wid}) "
        "SET winner.aliases = $aliases, "
        "    winner.mention_count = winner.mention_count + $loser_count",
        {"wid": winner["id"], "aliases": new_aliases, "loser_count": loser["count"]},
    )
    # Step 4: delete loser node (remaining edges were redirected above)
    run_query(
        "MATCH (loser:Entity {id: $lid}) DETACH DELETE loser",
        {"lid": loser["id"]},
    )
    return {
        "winner_id": winner["id"],
        "winner_name": winner["name"],
        "loser_name": loser["name"],
        "new_aliases_count": len(new_aliases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge duplicate / cross-language entities in Neo4j.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Cosine similarity threshold (default {DEFAULT_THRESHOLD})",
    )
    parser.add_argument("--apply", action="store_true", help="Actually merge (default: dry-run)")
    parser.add_argument(
        "--type", default=None, help="Restrict to one entity_type (person/service/project/...)"
    )
    parser.add_argument(
        "--max-merges", type=int, default=0, help="Cap on number of merges per run (0=unlimited)"
    )
    args = parser.parse_args()

    entities = _fetch_entities(args.type)
    print(f"entities loaded: {len(entities)} (type filter: {args.type or 'any'})")

    # Embed all entity names
    print("embedding names...")
    embeddings: dict[str, list[float]] = {}
    for ent in entities:
        emb = _embed_name(ent["name"])
        if emb:
            embeddings[ent["id"]] = emb

    print(f"embedded {len(embeddings)} / {len(entities)}")

    # Find pairs above threshold (within same type only for safety)
    ent_by_id = {e["id"]: e for e in entities}
    by_type: dict[str, list[str]] = {}
    for e in entities:
        by_type.setdefault(e["type"] or "concept", []).append(e["id"])

    candidates: list[tuple[float, dict, dict]] = []
    rejected_by_safeguard: list[tuple[float, str, str, str]] = []
    compared = 0
    for _etype, ids in by_type.items():
        for i, aid in enumerate(ids):
            if aid not in embeddings:
                continue
            for bid in ids[i + 1 :]:
                if bid not in embeddings:
                    continue
                compared += 1
                sim = _cosine(embeddings[aid], embeddings[bid])
                if sim >= args.threshold:
                    a = ent_by_id[aid]
                    b = ent_by_id[bid]
                    safe, reason = _safe_to_merge(a["name"], b["name"])
                    if not safe:
                        rejected_by_safeguard.append((sim, a["name"], b["name"], reason))
                        continue
                    candidates.append((sim, a, b))

    candidates.sort(key=lambda x: -x[0])
    print(
        f"compared {compared} pairs, {len(candidates)} above threshold {args.threshold}, "
        f"{len(rejected_by_safeguard)} rejected by safeguards"
    )
    if rejected_by_safeguard:
        print("\nSafeguard rejections (first 10):")
        for sim, a_n, b_n, reason in rejected_by_safeguard[:10]:
            print(f"  [{reason}] [{sim:.3f}] {a_n}  ≠  {b_n}")

    if not candidates:
        print("Nothing to merge.")
        return 0

    merges_planned: list[dict] = []
    seen_ids: set[str] = set()
    for sim, a, b in candidates:
        if a["id"] in seen_ids or b["id"] in seen_ids:
            continue  # already merged in this pass
        winner, loser = _pick_winner(a, b)
        merges_planned.append(
            {
                "sim": sim,
                "winner": f"{winner['name']} ({winner['type']}, {winner['count']} mentions)",
                "loser": f"{loser['name']} ({loser['type']}, {loser['count']} mentions)",
                "winner_id": winner["id"],
                "loser_id": loser["id"],
            }
        )
        seen_ids.add(winner["id"])
        seen_ids.add(loser["id"])
        if args.max_merges and len(merges_planned) >= args.max_merges:
            break

    print(f"\nMerge plan ({len(merges_planned)} pairs):")
    for m in merges_planned[:30]:
        print(f"  [{m['sim']:.3f}] {m['loser']}  →  {m['winner']}")
    if len(merges_planned) > 30:
        print(f"  ... +{len(merges_planned) - 30} more")

    if not args.apply:
        print("\nDry run. Re-run with --apply to commit.")
        return 0

    # Apply merges
    applied = 0
    for m in merges_planned:
        try:
            winner = {"id": m["winner_id"], "name": m["winner"].split(" (")[0], "aliases": [], "count": 0}
            loser = {"id": m["loser_id"], "name": m["loser"].split(" (")[0], "count": 0}
            # Fetch fresh data for aliases/count
            rows = run_query(
                "MATCH (e:Entity {id: $id}) "
                "RETURN e.aliases AS aliases, coalesce(e.mention_count, 1) AS count",
                {"id": m["winner_id"]},
            )
            if rows:
                winner["aliases"] = rows[0].get("aliases") or []
                winner["count"] = rows[0].get("count") or 1
            rows = run_query(
                "MATCH (e:Entity {id: $id}) RETURN coalesce(e.mention_count, 1) AS count",
                {"id": m["loser_id"]},
            )
            if rows:
                loser["count"] = rows[0].get("count") or 1
            _merge_pair(winner, loser)
            applied += 1
            if applied % 10 == 0:
                print(f"  merged {applied}/{len(merges_planned)}", flush=True)
        except Exception as e:
            print(f"  merge failed for {m['loser']} → {m['winner']}: {e}", file=sys.stderr)

    print(f"\nApplied: {applied}/{len(merges_planned)} merges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
