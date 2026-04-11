#!/opt/homebrew/bin/python3
"""Backfill canonical decision notes into Neo4j as typed Decision nodes.

Scans canonical/decisions/*.md, extracts structured fields,
and creates Decision nodes with typed edges to affected entities.

Usage:
  backfill_decisions.py              # dry-run (show what would be created)
  backfill_decisions.py --apply      # actually write to Neo4j
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain_core"))

try:
    from config import KNOWLEDGE_DIR
    DECISIONS_DIR = KNOWLEDGE_DIR / "canonical" / "decisions"
except ImportError:
    DECISIONS_DIR = Path("/Users/chrischo/server/knowledge/canonical/decisions")

# Common prose/heading words that are NOT entities
_HEADING_WORDS = frozenset({
    "the", "this", "that", "review", "summary", "statement", "source",
    "decision", "background", "rationale", "impact", "alternative",
    "conclusion", "notes", "context", "details", "overview", "update",
    "status", "action", "item", "section", "date", "author", "evidence",
    "observations", "candidate", "proposed", "confirmed", "domain",
    "never", "always", "should", "when", "where", "what", "how",
    "merged", "summarized", "distilled",
})

# Known tech entities to always capture
_TECH_ENTITIES = re.compile(
    r'\b(?:docker|nginx|chromadb|ollama|neo4j|cloudflare|openclaw|brain|ghost|minio|'
    r'vaultwarden|searxng|nextjs|next\.js|fastapi|react|vite|typescript|python|'
    r'cloudflared|grafana|uptime.kuma|watchtower|couchdb|glance|orbstack|launchd)\b', re.I)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_decision(md_file: Path) -> dict | None:
    """Extract decision metadata from a canonical markdown note."""
    try:
        text = md_file.read_text(errors="replace")
    except Exception:
        return None

    if len(text.strip()) < 50:
        return None

    name = md_file.stem.replace("-", " ").replace("_", " ")

    # Extract summary from markdown
    summary_match = re.search(r'(?:^|\n)#+ (?:Summary|Statement)\s*\n(.+?)(?:\n#|\Z)', text, re.S)
    summary = summary_match.group(1).strip()[:300] if summary_match else text[:300]

    # Extract capitalized proper nouns (filter aggressively)
    entities = set()
    for word in re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', text):
        if len(word) >= 3 and word.lower() not in _HEADING_WORDS:
            entities.add(word.lower())
    # Add known tech entities
    for match in _TECH_ENTITIES.finditer(text):
        entities.add(match.group().lower())

    return {
        "name": name,
        "source_file": str(md_file),
        "summary": summary,
        "entities": list(entities)[:10],
    }


def backfill(apply: bool = False):
    if not DECISIONS_DIR.exists():
        print(f"Decisions dir not found: {DECISIONS_DIR}")
        return

    md_files = list(DECISIONS_DIR.glob("*.md"))
    print(f"Found {len(md_files)} decision files")

    decisions = []
    for f in md_files:
        d = extract_decision(f)
        if d:
            decisions.append(d)

    print(f"Extracted {len(decisions)} valid decisions")

    if not apply:
        print("\n[DRY RUN] Would create:")
        for d in decisions[:10]:
            print(f"  Decision: {d['name'][:60]}")
            print(f"    entities: {d['entities'][:5]}")
        if len(decisions) > 10:
            print(f"  ... and {len(decisions) - 10} more")
        print("\nRun with --apply to write to Neo4j")
        return

    from entity_graph import resolve_entity, add_alias
    from neo4j_client import run_write

    now = _now_iso()
    created = 0
    for d in decisions:
        name = d["name"]

        run_write(
            "MERGE (d:Entity {name: $name}) "
            "ON CREATE SET d.id = 'dec_' + left(randomUUID(), 12), "
            "  d.entity_type = 'decision', d.first_seen_at = $now, "
            "  d.last_seen_at = $now, d.mention_count = 1, "
            "  d.memory_class = 'seasonal', d.source_file = $source "
            "ON MATCH SET d.last_seen_at = $now, "
            "  d.mention_count = d.mention_count + 1, "
            "  d.entity_type = 'decision', d.source_file = $source",
            {"name": name, "now": now, "source": d["source_file"]},
        )
        created += 1

        for ent_name in d["entities"]:
            canonical = resolve_entity(ent_name) or ent_name
            run_write(
                "MATCH (d:Entity {name: $dec_name}) "
                "MERGE (e:Entity {name: $ent_name}) "
                "ON CREATE SET e.id = 'ent_' + left(randomUUID(), 12), "
                "  e.entity_type = 'concept', e.first_seen_at = $now, "
                "  e.last_seen_at = $now, e.mention_count = 1, "
                "  e.memory_class = 'ephemeral' "
                "MERGE (d)-[r:RELATES_TO {relationship: 'affects'}]->(e) "
                "ON CREATE SET r.weight = 0.3, r.co_occurrence_count = 1, "
                "  r.created_at = $now "
                "ON MATCH SET r.co_occurrence_count = r.co_occurrence_count + 1, "
                "  r.weight = CASE WHEN r.weight + 0.1 > 1.0 THEN 1.0 "
                "    ELSE r.weight + 0.1 END",
                {"dec_name": name, "ent_name": canonical, "now": now},
            )

    print(f"Created/updated {created} Decision nodes")

    CORE_ALIASES = {
        "chris cho": ["chris", "chrischo", "wheogus98"],
        "nginx": ["reverse proxy", "nginx-proxy"],
        "mcc": ["chrischodev", "main web app"],
        "chromadb": ["chroma", "vector db"],
        "openclaw": ["oc", "multi-agent"],
        "brain": ["brain server", "brain api", "brain system"],
        "docker": ["container", "orbstack"],
    }
    alias_count = 0
    for entity, aliases in CORE_ALIASES.items():
        for alias in aliases:
            if add_alias(entity, alias):
                alias_count += 1
    print(f"Seeded {alias_count} entity aliases")


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
