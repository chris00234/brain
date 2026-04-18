#!/usr/bin/env python3
"""cli/rebuild_atom_entity.py — rebuild sqlite atom_entity mirror + Neo4j MENTIONS.

Background: the sqlite `atom_entity` join table was only populated by
entity_graph.extract_and_store_entities at ingest time, and that code path
had a name/id mismatch bug which left the mirror nearly empty (~25 edges
for 600+ atoms). Retrieval's entity boost relied on this table and was
effectively disabled.

This script rebuilds the mirror deterministically using name-match against
existing Neo4j Entity nodes:

  1. Pull every Entity from Neo4j with (name, entity_type, aliases)
  2. For each atom in brain.db::atoms (tier != obsolete, text >= 40)
  3. Find Entity names (+aliases) that appear as substrings in atom.text
     - Case-insensitive match
     - Word-boundary preferred (via regex \\b word edges)
  4. For each match:
     - upsert_entity(name, type) → entity_id in sqlite mirror
     - link_atom_entity(atom_id, entity_id, role='mention')
     - Neo4j: MERGE (m:MemoryAccess {memory_id: chroma_id})
              -[:MENTIONS]-> (e:Entity {name: name})

Phase A (sqlite mirror rebuild) + Phase B (Neo4j MENTIONS edges) together.

Run:
  .venv/bin/python cli/rebuild_atom_entity.py [--dry-run] [--limit N]

Idempotent: INSERT OR IGNORE on atom_entity, MERGE on Neo4j.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from atoms_store import BRAIN_ATOMS_ENABLED, _conn, link_atom_entity, upsert_entity
from config import BRAIN_LOGS_DIR

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
MIN_TEXT_LEN = 40
MIN_NAME_LEN = 3  # skip 1-2 char "entities" that match everything
MAX_NAME_LEN = 50  # skip long-sentence entities


# Case-insensitive, word-boundary substring match
def _build_matcher(name: str):
    # Escape regex metachars in entity name, wrap in word boundaries.
    escaped = re.escape(name)
    # Korean names have no word boundary, so also allow direct substring
    # for non-ASCII. For ASCII names, use \b for strict match.
    if name.isascii():
        return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def fetch_neo4j_entities() -> list[dict]:
    """Pull every Entity with name, type, aliases from Neo4j."""
    from neo4j_client import run_query

    rows = run_query(
        "MATCH (e:Entity) "
        "RETURN e.name AS name, e.entity_type AS etype, "
        "       coalesce(e.aliases, []) AS aliases, "
        "       e.mention_count AS mentions",
    )
    return [dict(r) for r in rows]


def fetch_atoms(limit: int = 0) -> list[dict]:
    """Pull all non-obsolete atoms with enough text to bother matching."""
    with _conn() as conn:
        cursor = conn.execute(
            "SELECT id, chroma_id, text FROM atoms "
            "WHERE tier != 'obsolete' AND length(text) >= ? "
            "ORDER BY created_at DESC" + (f" LIMIT {int(limit)}" if limit > 0 else ""),
            (MIN_TEXT_LEN,),
        )
        return [dict(row) for row in cursor]


def build_entity_matchers(entities: list[dict]) -> list[tuple]:
    """Build (match_name, canonical_name, type) tuples. Each alias becomes
    a separate matcher pointing to the canonical entity."""
    out: list[tuple] = []
    for ent in entities:
        name = (ent.get("name") or "").strip()
        etype = (ent.get("etype") or "concept").strip().lower()
        if not name or len(name) < MIN_NAME_LEN or len(name) > MAX_NAME_LEN:
            continue
        # Primary name
        out.append((name, name, etype, _build_matcher(name)))
        # Aliases
        for alias in ent.get("aliases") or []:
            alias = (alias or "").strip()
            if alias and MIN_NAME_LEN <= len(alias) <= MAX_NAME_LEN:
                out.append((alias, name, etype, _build_matcher(alias)))
    return out


def neo4j_link_mentions(memory_id: str, entity_name: str) -> bool:
    """Create MENTIONS edge from MemoryAccess → Entity. Idempotent via MERGE."""
    try:
        from neo4j_client import run_write

        run_write(
            "MERGE (m:MemoryAccess {memory_id: $mid}) "
            "  ON CREATE SET m.utility_score = 0.5, m.access_count = 0, "
            "    m.first_accessed_at = $now, m.last_accessed_at = $now, "
            "    m.memory_class = 'unknown' "
            "MERGE (e:Entity {name: $name}) "
            "MERGE (m)-[r:MENTIONS]->(e) "
            "  ON CREATE SET r.created_at = $now, r.confidence = 0.8",
            {
                "mid": memory_id,
                "name": entity_name,
                "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild atom_entity mirror + Neo4j MENTIONS")
    parser.add_argument("--dry-run", action="store_true", help="Print matches, don't write")
    parser.add_argument("--limit", type=int, default=0, help="Max atoms to process (0=all)")
    parser.add_argument("--skip-neo4j", action="store_true", help="Only rebuild sqlite mirror")
    parser.add_argument(
        "--min-mentions", type=int, default=0, help="Skip entities with fewer than N Neo4j mentions"
    )
    args = parser.parse_args()

    if not BRAIN_ATOMS_ENABLED:
        print("BRAIN_ATOMS_ENABLED=false — aborting", file=sys.stderr)
        return 1

    print("[1/4] Fetching Neo4j entities...")
    entities = fetch_neo4j_entities()
    if args.min_mentions > 0:
        entities = [e for e in entities if (e.get("mentions") or 0) >= args.min_mentions]
    print(f"  loaded {len(entities)} entities")

    print("[2/4] Building matchers...")
    matchers = build_entity_matchers(entities)
    print(f"  built {len(matchers)} matchers (names + aliases)")

    print("[3/4] Fetching atoms...")
    atoms = fetch_atoms(args.limit)
    print(f"  loaded {len(atoms)} atoms to scan")

    print("[4/4] Matching + writing...")
    t0 = time.time()
    atoms_with_match = 0
    total_sqlite_links = 0
    total_neo4j_edges = 0
    errors = 0

    for i, atom in enumerate(atoms, 1):
        atom_id = atom["id"]
        chroma_id = atom["chroma_id"] or atom_id
        text = atom["text"] or ""
        matches: set[tuple[str, str]] = set()  # (canonical_name, etype)

        for match_name, canonical_name, etype, matcher in matchers:
            if matcher.search(text):
                matches.add((canonical_name, etype))

        if not matches:
            continue
        atoms_with_match += 1

        for canonical_name, etype in sorted(matches)[:10]:  # cap at 10 per atom
            if args.dry_run:
                total_sqlite_links += 1
                continue
            try:
                eid = upsert_entity(canonical_name, etype)
                if eid:
                    if link_atom_entity(atom_id, eid, role="mention"):
                        total_sqlite_links += 1
                    if not args.skip_neo4j:
                        if neo4j_link_mentions(chroma_id, canonical_name):
                            total_neo4j_edges += 1
            except Exception as e:
                errors += 1
                print(f"  error on atom {atom_id[:16]} / {canonical_name}: {e}", file=sys.stderr)

        if i % 50 == 0 or i == len(atoms):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(
                f"  [{i}/{len(atoms)}] matched={atoms_with_match} "
                f"sqlite_edges={total_sqlite_links} "
                f"neo4j_edges={total_neo4j_edges} "
                f"rate={rate:.1f}/s errors={errors}",
                flush=True,
            )

    elapsed = time.time() - t0
    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"  atoms with matches: {atoms_with_match}/{len(atoms)}")
    print(f"  sqlite atom_entity edges: {total_sqlite_links}")
    print(f"  neo4j MENTIONS edges: {total_neo4j_edges}")
    print(f"  errors: {errors}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
