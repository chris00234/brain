#!/usr/bin/env python3
"""cli/canonicalize_entities_cross_lang.py — Layer B entity canonicalization.

Cross-language / spelling-variant deduplication for the Neo4j Entity graph.
Fixes cases like "크리스" ↔ "chris cho", "브레인" ↔ "brain system",
"오픈클로" ↔ "openclaw" that ended up as separate nodes because Sage
extracted them from Korean vs English text and the _neo4j_store_entities
MERGE is name-exact.

Algorithm:
  1. Pull every Entity (name, type, mention_count, aliases) from Neo4j
  2. Embed each entity name via Ollama (multilingual-e5-large-instruct
     handles Korean and English in the same vector space)
  3. For each (type, cluster), find pairs with cosine similarity >= threshold
     where names differ — these are likely cross-language variants
  4. Within each cluster, pick canonical = highest mention_count
     (or first alphabetic if tied)
  5. For each non-canonical member:
     - Append its name + existing aliases to canonical.aliases
     - Redirect its RELATES_TO + MENTIONS edges to the canonical
     - DETACH DELETE the duplicate
  6. Write an audit log of every merge decision

Safety:
  - --dry-run prints what WOULD merge, doesn't write
  - Only merges within same entity_type (person+person, service+service)
  - Only triggers on similarity >= THRESHOLD (default 0.88, high bar)
  - Skips merges where both sides have mention_count >= 5 (high-activity
    entities need human review, might be genuinely different)
  - Writes audit to logs/entity_canonicalize_audit.jsonl

Run:
  .venv/bin/python cli/canonicalize_entities_cross_lang.py --dry-run
  .venv/bin/python cli/canonicalize_entities_cross_lang.py --apply
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from config import BRAIN_LOGS_DIR
from indexer import get_embeddings_batch
from neo4j_client import run_query, run_write

AUDIT_LOG = BRAIN_LOGS_DIR / "entity_canonicalize_audit.jsonl"
DEFAULT_THRESHOLD = 0.93  # raised from 0.88 after dry-run showed false positives
HIGH_ACTIVITY_SKIP = 5  # don't auto-merge if both have >= this many mentions

# Entity types excluded from auto-merge. These types have formulaic naming
# that clusters by grammatical structure rather than semantic identity —
# e.g. agent nodes (liz/jenna/ellie are DIFFERENT agents that embed
# similarly because they're all short agent names), preference sentences
# (all "chris wants X" phrases look alike), event nodes (all holidays
# share structure). Merging these would cause data loss.
EXCLUDED_TYPES = {"agent", "preference", "event"}

# Names containing date/timestamp patterns are date-distinct by design
# (e.g. "screen time pattern 2026 03 20" vs same for 2026 03 18) — the
# embedder treats the date as noise and lumps them together, but they
# refer to different temporal snapshots.
import re as _re

DATE_PATTERNS = [
    _re.compile(r"\b\d{4}[-_ ]\d{1,2}[-_ ]\d{1,2}\b"),  # 2026-04-08 / 2026 04 08
    _re.compile(r"\d{4}-\d{2}-\d{2}t\d{2}"),  # ISO timestamps
]


def _has_date(name: str) -> bool:
    return any(p.search(name.lower()) for p in DATE_PATTERNS)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def fetch_entities() -> list[dict]:
    rows = run_query(
        "MATCH (e:Entity) "
        "RETURN e.name AS name, e.entity_type AS etype, "
        "       coalesce(e.aliases, []) AS aliases, "
        "       e.mention_count AS mentions, "
        "       id(e) AS node_id "
        "ORDER BY e.mention_count DESC",
    )
    return [dict(r) for r in rows]


def cluster_by_similarity(
    entities: list[dict],
    embeddings: list[list[float]],
    threshold: float,
) -> list[list[int]]:
    """Return list of index clusters where each cluster shares similarity >= threshold
    AND same entity_type. Single-element clusters are omitted.
    Uses single-linkage greedy clustering."""
    n = len(entities)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        if _has_date(entities[i]["name"]):
            continue
        for j in range(i + 1, n):
            if entities[i]["etype"] != entities[j]["etype"]:
                continue
            if entities[i]["etype"] in EXCLUDED_TYPES:
                continue
            if entities[i]["name"] == entities[j]["name"]:
                continue
            if _has_date(entities[j]["name"]):
                continue
            sim = cosine(embeddings[i], embeddings[j])
            if sim >= threshold:
                union(i, j)

    from collections import defaultdict

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return [g for g in groups.values() if len(g) > 1]


def merge_cluster(
    entities: list[dict],
    cluster: list[int],
    embeddings: list[list[float]],
    dry_run: bool,
    audit_fh,
) -> tuple[bool, str]:
    """Merge a cluster of entity indices. Returns (merged, reason)."""
    members = [entities[i] for i in cluster]

    # Skip if any pair has both mention_count >= HIGH_ACTIVITY_SKIP
    high_activity = [m for m in members if (m.get("mentions") or 0) >= HIGH_ACTIVITY_SKIP]
    if len(high_activity) >= 2:
        return False, "high_activity_cluster_needs_review"

    # Canonical = highest mention_count, tiebreak alphabetical
    members_sorted = sorted(
        members,
        key=lambda m: (-(m.get("mentions") or 0), m["name"]),
    )
    canonical = members_sorted[0]
    duplicates = members_sorted[1:]

    canonical_name = canonical["name"]
    new_aliases = list(canonical.get("aliases") or [])
    for dup in duplicates:
        new_aliases.append(dup["name"])
        for a in dup.get("aliases") or []:
            if a and a != canonical_name:
                new_aliases.append(a)
    # Dedupe while preserving order
    seen = set()
    aliases_clean = [a for a in new_aliases if not (a in seen or seen.add(a))]

    # Compute pairwise similarities for audit
    idx_canonical = entities.index(canonical)
    sims = [cosine(embeddings[idx_canonical], embeddings[entities.index(d)]) for d in duplicates]

    audit_entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "canonical": canonical_name,
        "etype": canonical["etype"],
        "merged_in": [d["name"] for d in duplicates],
        "similarities": [round(s, 4) for s in sims],
        "canonical_mentions": canonical.get("mentions") or 0,
        "duplicate_mentions": [d.get("mentions") or 0 for d in duplicates],
        "dry_run": dry_run,
    }
    audit_fh.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

    if dry_run:
        return True, "dry_run_ok"

    # 1. Add aliases to canonical
    run_write(
        "MATCH (c:Entity {name: $name}) SET c.aliases = $aliases, c.last_seen_at = $now",
        {
            "name": canonical_name,
            "aliases": aliases_clean,
            "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    # 2. Redirect every edge from duplicates to canonical, then delete duplicates.
    # Use APOC-free cypher: copy relationships with SET properties, then DETACH DELETE.
    for dup in duplicates:
        dup_name = dup["name"]
        if dup_name == canonical_name:
            continue

        # Redirect outgoing RELATES_TO
        run_write(
            "MATCH (d:Entity {name: $dup})-[r:RELATES_TO]->(t:Entity) "
            "WHERE t.name <> $canonical "
            "MATCH (c:Entity {name: $canonical}) "
            "MERGE (c)-[nr:RELATES_TO {relationship: coalesce(r.relationship, 'related_to')}]->(t) "
            "  ON CREATE SET nr.id = r.id, nr.weight = coalesce(r.weight, 0.1), "
            "    nr.co_occurrence_count = coalesce(r.co_occurrence_count, 1), "
            "    nr.confidence = coalesce(r.confidence, 0.5), "
            "    nr.created_at = coalesce(r.created_at, $now) "
            "DELETE r",
            {"dup": dup_name, "canonical": canonical_name, "now": audit_entry["ts"]},
        )

        # Redirect incoming RELATES_TO
        run_write(
            "MATCH (s:Entity)-[r:RELATES_TO]->(d:Entity {name: $dup}) "
            "WHERE s.name <> $canonical "
            "MATCH (c:Entity {name: $canonical}) "
            "MERGE (s)-[nr:RELATES_TO {relationship: coalesce(r.relationship, 'related_to')}]->(c) "
            "  ON CREATE SET nr.id = r.id, nr.weight = coalesce(r.weight, 0.1), "
            "    nr.co_occurrence_count = coalesce(r.co_occurrence_count, 1), "
            "    nr.confidence = coalesce(r.confidence, 0.5), "
            "    nr.created_at = coalesce(r.created_at, $now) "
            "DELETE r",
            {"dup": dup_name, "canonical": canonical_name, "now": audit_entry["ts"]},
        )

        # Redirect MENTIONS from MemoryAccess
        run_write(
            "MATCH (m:MemoryAccess)-[r:MENTIONS]->(d:Entity {name: $dup}) "
            "MATCH (c:Entity {name: $canonical}) "
            "MERGE (m)-[nr:MENTIONS]->(c) "
            "  ON CREATE SET nr.created_at = coalesce(r.created_at, $now), "
            "    nr.confidence = coalesce(r.confidence, 0.8) "
            "DELETE r",
            {"dup": dup_name, "canonical": canonical_name, "now": audit_entry["ts"]},
        )

        # Bump canonical mention_count by the duplicate's count
        run_write(
            "MATCH (c:Entity {name: $canonical}), (d:Entity {name: $dup}) "
            "SET c.mention_count = coalesce(c.mention_count, 0) + coalesce(d.mention_count, 0)",
            {"canonical": canonical_name, "dup": dup_name},
        )

        # Finally delete the duplicate
        run_write(
            "MATCH (d:Entity {name: $dup}) DETACH DELETE d",
            {"dup": dup_name},
        )

    return True, f"merged {len(duplicates)} into {canonical_name}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-language entity canonicalization")
    parser.add_argument("--dry-run", action="store_true", help="Show merges, don't write")
    parser.add_argument("--apply", action="store_true", help="Actually apply merges")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Cosine similarity threshold (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--min-mentions", type=int, default=1, help="Only consider entities with at least N mentions"
    )
    args = parser.parse_args()

    if not (args.dry_run or args.apply):
        args.dry_run = True  # default safe

    print("[1/4] Fetching entities from Neo4j...")
    all_entities = fetch_entities()
    entities = [e for e in all_entities if (e.get("mentions") or 0) >= args.min_mentions]
    print(f"  loaded {len(all_entities)} total, {len(entities)} after min-mentions filter")

    print("[2/4] Embedding entity names (Ollama)...")
    t0 = time.time()
    names = [e["name"] for e in entities]
    embeddings = get_embeddings_batch(names, prefix="passage", batch_size=32)
    print(f"  embedded {len(embeddings)} names in {time.time()-t0:.1f}s")
    # Filter out any failed embeddings
    good_idx = [i for i, e in enumerate(embeddings) if e]
    if len(good_idx) != len(entities):
        print(f"  WARN: {len(entities)-len(good_idx)} entities failed to embed, dropping")
    entities = [entities[i] for i in good_idx]
    embeddings = [embeddings[i] for i in good_idx]

    print(f"[3/4] Clustering by cosine >= {args.threshold}...")
    clusters = cluster_by_similarity(entities, embeddings, args.threshold)
    print(f"  found {len(clusters)} merge clusters")

    print(f"[4/4] {'DRY RUN' if args.dry_run else 'APPLY'} — processing clusters...")
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    audit_fh = AUDIT_LOG.open("a")
    merged = 0
    skipped = 0
    for cluster in clusters:
        members = [entities[i] for i in cluster]
        names_str = " ⟷ ".join(f"{m['name']}({m.get('mentions') or 0})" for m in members)
        ok, reason = merge_cluster(entities, cluster, embeddings, args.dry_run, audit_fh)
        status = "MERGE" if ok else "SKIP"
        print(f"  {status} [{members[0]['etype']:10}] {names_str} — {reason}")
        if ok:
            merged += 1
        else:
            skipped += 1
    audit_fh.close()

    print()
    print(f"Clusters merged: {merged}")
    print(f"Clusters skipped: {skipped}")
    print(f"Audit: {AUDIT_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
