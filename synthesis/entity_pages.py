#!/opt/homebrew/bin/python3
"""Auto-generate canonical entity pages from Neo4j.

Queries Neo4j for "hot" entities (mention_count >= threshold) that don't
yet have a canonical page at canonical/entities/<slug>.md. Dispatches
Sage to synthesize a structured summary from /recall/v2 context, then
writes the result as a canonical note with full JSON frontmatter.

Inspired by Karpathy's llm-wiki gist — entity pages are first-class wiki
nodes that give the knowledge graph explicit topic landmarks.

Cadence: one entity per run to bound LLM cost. Weekly via scheduler.

Usage:
  entity_pages.py [--limit 1] [--dry-run] [--entity NAME]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

from cli_llm import dispatch  # migrated 2026-04-17
from common import ROOT
from neo4j_client import run_query

ENTITY_DIR = ROOT / "canonical" / "entities"
LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
BRAIN_URL = "http://127.0.0.1:8791"
SECRET_PATH = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")

MIN_MENTIONS = 5
SKIP_ENTITIES = {"chris cho", "chris", "daehyun cho", "daehyun", "chrischo"}
DISPATCH_TIMEOUT = 180
REGEN_MIN_AGE_DAYS = 7

PROMPT = """You are Sage. Generate a canonical ENTITY PAGE for Chris's knowledge base.

ENTITY: {name}
TYPE: {entity_type}
ALIASES: {aliases}
MENTIONS: {mentions}
FIRST SEEN: {first_seen}
LAST SEEN: {last_seen}

RELATED ENTITIES (top {n_related}):
{related_block}

RECENT CONTEXT (from Chris's RAG, top {n_context}):
{context_block}

Return ONLY a JSON object (no prose, no fences):
{{
  "title": "human-readable title under 90 chars",
  "summary": "1-2 sentences describing what this entity IS and why it matters to Chris",
  "key_facts": ["fact1", "fact2", "fact3"],
  "related_entities": ["most-related-1", "most-related-2"],
  "open_questions": ["q1 if any"]
}}

Rules:
- Be specific and concrete. Use information from the context block.
- Key facts should be declarative ground truths, not observations or dates.
- If the entity is a project or service, lead with what it does.
- Prefer names Chris actually uses (from the aliases list when applicable).
- Return an empty list for open_questions if there are none — do NOT invent.
- Do NOT wrap in ```json``` fences.

JSON only:"""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return slug.strip("_")[:64]


def _hot_entities(limit: int) -> list[dict]:
    cypher = (
        "MATCH (e:Entity) "
        "WHERE coalesce(e.mention_count, 0) >= $min "
        "RETURN e.name AS name, e.entity_type AS entity_type, "
        "       e.mention_count AS mentions, "
        "       e.first_seen_at AS first_seen, e.last_seen_at AS last_seen, "
        "       coalesce(e.aliases, []) AS aliases "
        "ORDER BY e.mention_count DESC, e.last_seen_at DESC "
        "LIMIT $limit"
    )
    return [dict(r) for r in run_query(cypher, {"min": MIN_MENTIONS, "limit": limit})]


def _related(name: str, limit: int = 10) -> list[dict]:
    cypher = (
        "MATCH (e:Entity {name: $n})-[r:RELATES_TO]-(other:Entity) "
        "RETURN other.name AS name, other.entity_type AS entity_type, "
        "       coalesce(other.mention_count, 0) AS mentions, count(r) AS co "
        "ORDER BY co DESC, mentions DESC LIMIT $limit"
    )
    return [dict(r) for r in run_query(cypher, {"n": name, "limit": limit})]


def _recall_context(query: str, n: int = 10) -> list[dict]:
    """Fetch top-n retrieval hits for the entity name via /recall/v2 (GET)."""
    try:
        secret = SECRET_PATH.read_text().strip()
    except Exception:
        return []
    from urllib.parse import urlencode

    qs = urlencode({"q": query, "n": n})
    req = urllib.request.Request(
        f"{BRAIN_URL}/recall/v2?{qs}",
        headers={"Authorization": f"Bearer {secret}", "x-agent": "entity_pages"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  recall failed: {e}", file=sys.stderr)
        return []
    hits = data.get("results") or data.get("hits") or []
    out = []
    for h in hits[:n]:
        text = (h.get("content") or h.get("text") or h.get("body") or h.get("document") or "")[:500]
        src_parts = [p for p in (h.get("collection"), h.get("title"), h.get("path")) if p]
        src = " / ".join(src_parts) or h.get("source") or ""
        if text.strip():
            out.append({"text": text.strip(), "source": src})
    return out


def _load_existing_page(slug: str) -> tuple[Path, dict | None]:
    path = ENTITY_DIR / f"{slug}.md"
    if not path.exists():
        return path, None
    try:
        text = path.read_text()
        lines = text.splitlines()
        if not lines[0].startswith("---"):
            return path, None
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            return path, None
        meta = json.loads("\n".join(lines[1:end]))
        return path, meta
    except Exception:
        return path, None


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return int((datetime.now(UTC) - ts).days)
    except Exception:
        return None


def _dispatch_sage(prompt: str) -> dict | None:
    result = dispatch(
        agent="sage",
        message=prompt,
        thinking="medium",
        timeout=DISPATCH_TIMEOUT,
        backlog_kind="synthesis",
        backlog_payload={"source": "entity_pages", "prompt": prompt[:500]},
    )
    if not result.ok:
        print(f"  sage dispatch failed: {(result.error or '')[:200]}", file=sys.stderr)
        return None
    text = (result.text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start : brace_end + 1])
            except Exception:
                pass
        print(f"  sage returned non-JSON: {text[:200]}", file=sys.stderr)
        return None


def _render_body(entity: dict, synth: dict, related: list[dict], context_count: int) -> str:
    aliases = entity.get("aliases") or []
    lines = [
        "## Summary",
        "",
        synth.get("summary", "").strip() or "_Pending synthesis._",
        "",
        f"**Type:** {entity.get('entity_type') or 'unknown'}  ",
        f"**Mentions:** {entity.get('mentions', 0)}  ",
    ]
    if entity.get("first_seen"):
        lines.append(f"**First seen:** {entity['first_seen'][:10]}  ")
    if entity.get("last_seen"):
        lines.append(f"**Last seen:** {entity['last_seen'][:10]}  ")
    if aliases:
        lines.append(f"**Aliases:** {', '.join(aliases)}")
    lines.append("")

    facts = synth.get("key_facts") or []
    if facts:
        lines.append("## Key Facts")
        lines.append("")
        for f in facts:
            if isinstance(f, str) and f.strip():
                lines.append(f"- {f.strip()}")
        lines.append("")

    if related:
        lines.append("## Related Entities")
        lines.append("")
        for r in related:
            tag = f" _{r.get('entity_type')}_" if r.get("entity_type") else ""
            lines.append(f"- **{r['name']}**{tag} — co-occurrence {r.get('co', 0)}")
        lines.append("")

    questions = synth.get("open_questions") or []
    valid_q = [q for q in questions if isinstance(q, str) and q.strip()]
    if valid_q:
        lines.append("## Open Questions")
        lines.append("")
        for q in valid_q:
            lines.append(f"- {q.strip()}")
        lines.append("")

    lines.append(f"_Auto-generated by entity_pages from {context_count} recalled chunks._")
    return "\n".join(lines)


def _write_page(slug: str, entity: dict, synth: dict, related: list[dict], context_count: int) -> Path:
    ENTITY_DIR.mkdir(parents=True, exist_ok=True)
    path = ENTITY_DIR / f"{slug}.md"
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    note_id = f"entity_{slug}"
    title = (synth.get("title") or f"{entity['name']} — {entity.get('entity_type') or 'entity'}")[:180]

    meta = {
        "id": note_id,
        "type": "canonical",
        "domain": "entities",
        "subtype": "entity-page",
        "title": title,
        "status": "active",
        "visibility": "private",
        "confidence": 0.85,
        "created_at": now,
        "updated_at": now,
        "last_reviewed_at": now,
        "owner": "system",
        "scope": "global",
        "valid_from": None,
        "valid_to": None,
        "sources": [
            f"neo4j:entity:{entity['name']}",
            "sage:entity_pages",
        ],
        "provenance_summary": (
            f"Auto-generated entity page for {entity['name']} "
            f"({entity.get('entity_type') or 'entity'}, {entity.get('mentions', 0)} mentions)"
        )[:200],
        "entities": [entity["name"]] + [r["name"] for r in related[:10]],
        "relations": [{"type": "describes", "target": f"entity_{_slugify(entity['name'])}"}]
        + [{"type": "related_to", "target": f"entity_{_slugify(r['name'])}"} for r in related[:5]],
        "review_state": "proposed",
        "change_policy": "auto",
        "supersedes": [],
        "superseded_by": None,
    }
    body = _render_body(entity, synth, related, context_count)
    rendered = "---json\n" + json.dumps(meta, indent=2, ensure_ascii=False) + "\n---\n" + body + "\n"

    tmp = path.with_suffix(".tmp")
    tmp.write_text(rendered)
    tmp.replace(path)
    return path


def _log_run(record: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "entity_pages.jsonl"
    with log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _pick_next(entities: list[dict]) -> dict | None:
    for entity in entities:
        name = entity["name"]
        if name.lower() in SKIP_ENTITIES:
            continue
        slug = _slugify(name)
        path, existing_meta = _load_existing_page(slug)
        if existing_meta:
            updated = existing_meta.get("updated_at")
            if _age_days(updated) is not None and _age_days(updated) < REGEN_MIN_AGE_DAYS:
                continue
        return entity
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1, help="entities to process per run (default 1)")
    parser.add_argument("--entity", type=str, default=None, help="force a specific entity name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--candidates", type=int, default=20, help="pool size to pick from")
    args = parser.parse_args()

    if args.entity:
        cypher = (
            "MATCH (e:Entity {name: $n}) "
            "RETURN e.name AS name, e.entity_type AS entity_type, "
            "       e.mention_count AS mentions, "
            "       e.first_seen_at AS first_seen, e.last_seen_at AS last_seen, "
            "       coalesce(e.aliases, []) AS aliases"
        )
        rows = list(run_query(cypher, {"n": args.entity}))
        if not rows:
            print(json.dumps({"status": "entity_not_found", "name": args.entity}))
            return 1
        pool = [dict(rows[0])]
    else:
        pool = _hot_entities(args.candidates)

    processed: list[dict] = []
    for _ in range(args.limit):
        entity = _pick_next(pool) if not args.entity else (pool[0] if pool else None)
        if entity is None:
            break
        pool = [e for e in pool if e["name"] != entity["name"]]
        slug = _slugify(entity["name"])
        print(f"[entity_pages] processing {entity['name']} (slug={slug}, mentions={entity.get('mentions')})")

        related = _related(entity["name"], limit=10)
        context = _recall_context(entity["name"], n=10)
        context_block = (
            "\n\n".join(f"[{i + 1}] ({c['source']}) {c['text']}" for i, c in enumerate(context))
            or "_no context available_"
        )
        related_block = (
            "\n".join(
                f"- {r['name']} ({r.get('entity_type') or 'unknown'}) — co-occurrence {r.get('co', 0)}"
                for r in related
            )
            or "_no related entities_"
        )

        prompt = PROMPT.format(
            name=entity["name"],
            entity_type=entity.get("entity_type") or "unknown",
            aliases=", ".join(entity.get("aliases") or []) or "none",
            mentions=entity.get("mentions", 0),
            first_seen=entity.get("first_seen") or "unknown",
            last_seen=entity.get("last_seen") or "unknown",
            n_related=len(related),
            related_block=related_block,
            n_context=len(context),
            context_block=context_block[:6000],
        )

        if args.dry_run:
            print(f"  [dry-run] prompt={len(prompt)} chars, related={len(related)}, context={len(context)}")
            processed.append({"entity": entity["name"], "status": "dry-run"})
            continue

        synth = _dispatch_sage(prompt)
        if synth is None:
            processed.append({"entity": entity["name"], "status": "dispatch_failed"})
            _log_run(
                {"entity": entity["name"], "status": "dispatch_failed", "at": datetime.now(UTC).isoformat()}
            )
            continue

        path = _write_page(slug, entity, synth, related, len(context))
        processed.append({"entity": entity["name"], "status": "written", "path": str(path.relative_to(ROOT))})
        _log_run(
            {
                "entity": entity["name"],
                "status": "written",
                "path": str(path.relative_to(ROOT)),
                "context_count": len(context),
                "related_count": len(related),
                "at": datetime.now(UTC).isoformat(),
            }
        )

    print(json.dumps({"status": "ok", "processed": processed}))
    return 0 if processed else 1


if __name__ == "__main__":
    raise SystemExit(main())
