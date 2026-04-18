#!/opt/homebrew/bin/python3
"""Structural lint for the canonical knowledge layer.

Complements pipeline/lint_memory.py (stale / low-confidence / duplicates /
broken outbound relations) with three checks that are currently invisible:

1. ORPHAN notes — active canonical notes that nothing links into
2. MISSING CROSS-REFS — notes that mention an entity in body text but
   don't have a relations[].target to the entity's canonical page
3. DATA GAPS — Neo4j entities with mention_count ≥ threshold that lack
   a canonical/entities/<slug>.md page

Output: ~/server/knowledge/reports/canonical_lint/YYYY-MM-DD.{json,md}
Returns a one-line JSON summary on stdout for the scheduler.

Inspired by Karpathy's llm-wiki gist — the lint operation detects orphan
pages, missing cross-references, and data gaps as part of routine wiki
maintenance. The LLM handles bookkeeping the Memex couldn't solve.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from common import ROOT, iter_note_paths, parse_note

CANONICAL_DIR = ROOT / "canonical"
ENTITY_DIR = CANONICAL_DIR / "entities"
REPORT_DIR = ROOT / "reports" / "canonical_lint"
ORPHAN_MIN_AGE_DAYS = 30
DATA_GAP_MIN_MENTIONS = 5
XREF_MAX_ENTITIES = 30  # cap which entities we check to keep runtime bounded
SKIP_NAMES = {"index.md", "_index.md", "_identity.md", "_state.md", "_profile.md"}
SKIP_ENTITIES = {"chris cho", "chris", "daehyun cho", "daehyun", "chrischo"}


def _age_days(value: str | None) -> int | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return int((datetime.now(UTC) - ts).days)
    except (ValueError, TypeError):
        return None


def _load_canonical() -> list[dict]:
    notes = []
    for path in iter_note_paths(CANONICAL_DIR):
        if path.name in SKIP_NAMES or path.name.endswith(".bak"):
            continue
        try:
            meta, body = parse_note(path)
        except Exception:
            continue
        if meta.get("type") != "canonical":
            continue
        if meta.get("status") != "active":
            continue
        notes.append(
            {
                "id": meta.get("id") or path.stem,
                "title": meta.get("title") or path.stem,
                "domain": meta.get("domain") or "",
                "path": str(path.relative_to(ROOT)),
                "updated_at": meta.get("updated_at"),
                "relations_out": [r.get("target") for r in meta.get("relations", []) if r.get("target")],
                "supersedes": [s for s in meta.get("supersedes") or [] if s],
                "age_days": _age_days(meta.get("updated_at")),
            }
        )
    return notes


def _find_orphans(notes: list[dict]) -> list[dict]:
    inbound: dict[str, int] = {n["id"]: 0 for n in notes}
    for n in notes:
        for target in n["relations_out"]:
            if target in inbound:
                inbound[target] += 1
        for target in n["supersedes"]:
            if target in inbound:
                inbound[target] += 1

    orphans = []
    for n in notes:
        age = n["age_days"]
        if age is None or age < ORPHAN_MIN_AGE_DAYS:
            continue
        if inbound[n["id"]] > 0:
            continue
        if n["relations_out"]:
            continue
        orphans.append(
            {
                "id": n["id"],
                "title": n["title"][:120],
                "domain": n["domain"],
                "path": n["path"],
                "age_days": age,
                "inbound_refs": inbound[n["id"]],
                "outbound_refs": len(n["relations_out"]),
            }
        )
    orphans.sort(key=lambda o: (o["domain"], -o["age_days"]))
    return orphans


def _load_hot_entities() -> list[dict]:
    """Query Neo4j for hot entities. Returns [] on error (lint degrades gracefully)."""
    try:
        from neo4j_client import run_query
    except Exception:
        return []
    cypher = (
        "MATCH (e:Entity) "
        "WHERE coalesce(e.mention_count, 0) >= $min "
        "RETURN e.name AS name, e.entity_type AS entity_type, "
        "       e.mention_count AS mentions, "
        "       coalesce(e.aliases, []) AS aliases "
        "ORDER BY e.mention_count DESC LIMIT 100"
    )
    try:
        return [dict(r) for r in run_query(cypher, {"min": DATA_GAP_MIN_MENTIONS})]
    except Exception as e:
        print(f"  (neo4j unavailable, skipping entity-based checks: {e})", file=sys.stderr)
        return []


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug[:64]


def _entity_page_exists(name: str) -> bool:
    return (ENTITY_DIR / f"{_slugify(name)}.md").exists()


def _find_data_gaps(hot_entities: list[dict]) -> list[dict]:
    gaps = []
    for e in hot_entities:
        name = (e.get("name") or "").strip()
        if not name or name.lower() in SKIP_ENTITIES:
            continue
        if _entity_page_exists(name):
            continue
        gaps.append(
            {
                "name": name,
                "entity_type": e.get("entity_type") or "unknown",
                "mentions": e.get("mentions", 0),
                "suggested_slug": _slugify(name),
            }
        )
    gaps.sort(key=lambda g: -g["mentions"])
    return gaps


def _load_body(note: dict) -> str:
    try:
        return (ROOT / note["path"]).read_text(errors="replace").lower()
    except Exception:
        return ""


def _find_missing_xrefs(notes: list[dict], hot_entities: list[dict]) -> list[dict]:
    """For entities that DO have a canonical page, find notes mentioning
    the entity in body text but not linking via relations[].target."""
    entities_with_pages = []
    for e in hot_entities[:XREF_MAX_ENTITIES]:
        name = (e.get("name") or "").strip()
        if not name or name.lower() in SKIP_ENTITIES:
            continue
        if not _entity_page_exists(name):
            continue
        aliases = e.get("aliases") or []
        search_terms = [name.lower()] + [a.lower() for a in aliases if isinstance(a, str)]
        entities_with_pages.append(
            {
                "name": name,
                "target_id": f"entity_{_slugify(name)}",
                "terms": [t for t in search_terms if len(t) >= 3],
            }
        )
    if not entities_with_pages:
        return []

    missing = []
    for n in notes:
        body = _load_body(n)
        if not body:
            continue
        out_targets = set(n["relations_out"])
        note_id = n["id"]
        for ent in entities_with_pages:
            if ent["target_id"] == note_id:
                continue  # the entity's own page
            if ent["target_id"] in out_targets:
                continue  # already linked
            if not any(term in body for term in ent["terms"]):
                continue
            missing.append(
                {
                    "note_id": note_id,
                    "note_title": n["title"][:100],
                    "note_path": n["path"],
                    "entity": ent["name"],
                    "entity_page_id": ent["target_id"],
                }
            )
    missing.sort(key=lambda m: (m["entity"], m["note_id"]))
    return missing


def _write_report(orphans: list[dict], data_gaps: list[dict], missing_xrefs: list[dict]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    json_path = REPORT_DIR / f"{date}.json"
    md_path = REPORT_DIR / f"{date}.md"

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checks": {
            "orphan_notes": {
                "description": f"Active canonical notes with 0 inbound refs, 0 outbound refs, age ≥ {ORPHAN_MIN_AGE_DAYS}d",
                "count": len(orphans),
                "items": orphans,
            },
            "data_gaps": {
                "description": f"Neo4j entities with mention_count ≥ {DATA_GAP_MIN_MENTIONS} that lack a canonical/entities/<slug>.md page",
                "count": len(data_gaps),
                "items": data_gaps,
            },
            "missing_cross_refs": {
                "description": "Canonical notes mentioning an entity in body text without a relations[].target to that entity's canonical page",
                "count": len(missing_xrefs),
                "items": missing_xrefs,
            },
        },
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    lines = [
        f"# Canonical Lint — {date}",
        "",
        f"_Generated {payload['generated_at']}_",
        "",
        f"- Orphan notes: **{len(orphans)}**",
        f"- Data gaps (entities lacking page): **{len(data_gaps)}**",
        f"- Missing cross-refs: **{len(missing_xrefs)}**",
        "",
        "---",
        "",
        f"## Orphan notes ({len(orphans)})",
        "",
        f"Active canonical notes with 0 inbound references, 0 outbound relations, age ≥ {ORPHAN_MIN_AGE_DAYS}d.",
        "Candidates for supersession, cross-referencing, or archival.",
        "",
    ]
    if not orphans:
        lines.append("_None — canonical layer is well-linked._")
    else:
        by_domain: dict[str, list[dict]] = {}
        for o in orphans:
            by_domain.setdefault(o["domain"] or "other", []).append(o)
        for domain in sorted(by_domain):
            lines.append(f"### {domain} ({len(by_domain[domain])})")
            lines.append("")
            for o in by_domain[domain]:
                lines.append(f"- `{o['id']}` — **{o['title']}** — {o['age_days']}d old — `{o['path']}`")
            lines.append("")

    lines.append("")
    lines.append(f"## Data gaps — entities without canonical pages ({len(data_gaps)})")
    lines.append("")
    lines.append(
        f"Neo4j entities with mention_count ≥ {DATA_GAP_MIN_MENTIONS} but no entry in `canonical/entities/`."
    )
    lines.append("These should be created via the weekly `entity_pages` job.")
    lines.append("")
    if not data_gaps:
        lines.append("_None — every hot entity has a canonical page._")
    else:
        for g in data_gaps[:30]:
            lines.append(
                f"- **{g['name']}** — {g['entity_type']} — {g['mentions']} mentions — suggested slug `{g['suggested_slug']}`"
            )
        if len(data_gaps) > 30:
            lines.append(f"- _… {len(data_gaps) - 30} more_")

    lines.append("")
    lines.append(f"## Missing cross-references ({len(missing_xrefs)})")
    lines.append("")
    lines.append(
        "Canonical notes that mention an entity in their body text but don't have a `relations[].target` to the entity's canonical page."
    )
    lines.append("")
    if not missing_xrefs:
        lines.append("_None — cross-references are in order._")
    else:
        by_entity: dict[str, list[dict]] = {}
        for m in missing_xrefs:
            by_entity.setdefault(m["entity"], []).append(m)
        for entity in sorted(by_entity):
            items = by_entity[entity]
            lines.append(f"### → `{entity}` ({len(items)} notes)")
            lines.append("")
            for m in items[:15]:
                lines.append(f"- `{m['note_id']}` — **{m['note_title']}** — `{m['note_path']}`")
            if len(items) > 15:
                lines.append(f"- _… {len(items) - 15} more_")
            lines.append("")

    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def main() -> int:
    notes = _load_canonical()
    orphans = _find_orphans(notes)
    hot_entities = _load_hot_entities()
    data_gaps = _find_data_gaps(hot_entities) if hot_entities else []
    missing_xrefs = _find_missing_xrefs(notes, hot_entities) if hot_entities else []
    json_path, md_path = _write_report(orphans, data_gaps, missing_xrefs)
    print(
        json.dumps(
            {
                "status": "ok",
                "canonical_notes": len(notes),
                "orphan_notes": len(orphans),
                "data_gaps": len(data_gaps),
                "missing_cross_refs": len(missing_xrefs),
                "report": str(md_path.relative_to(ROOT)),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
