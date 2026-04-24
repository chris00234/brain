from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import ROOT, iter_note_paths, parse_markdown_frontmatter, tokenize

TIER_WEIGHT = {"canonical": 100, "distilled": 60}
DEFAULT_RAG_SEARCH = Path("/Users/chrischo/server/brain/brain_core/search.py")
DOMAIN_BOOST = {
    "decisions": 22,
    "infra": 16,
    "incidents": 14,
    "projects": 8,
    "chris": 6,
}


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def score_note(query: str, metadata: dict, body: str, *, filter_domain: str | None = None) -> int:
    if filter_domain and metadata.get("domain") != filter_domain:
        return 0

    query_terms = tokenize(query)
    if not query_terms:
        return 0

    title_terms = set(tokenize(metadata.get("title", "")))
    body_terms = set(tokenize(body))
    entity_terms = {term.lower() for term in metadata.get("entities", [])}
    # 2026-04-18: relations are dicts like {"type": "mentions", "target": "entity_x"}.
    # Previous code used .keys() which pulled "type" / "target" as search terms
    # (noise), and crashed when any relation wasn't a dict. Use .values() and
    # guard against non-dict entries.
    relation_terms = {
        str(v).lower()
        for relation in metadata.get("relations", []) or []
        if isinstance(relation, dict)
        for v in relation.values()
        if v
    }
    domain_terms = {metadata.get("domain", "").lower(), metadata.get("subtype", "").lower()}
    haystack = title_terms | body_terms | entity_terms | relation_terms | domain_terms
    query_set = set(query_terms)

    # 2026-04-18: tolerate unknown note types (entity-page, consolidated-draft,
    # etc.). Previously KeyError'd on `TIER_WEIGHT[metadata["type"]]` and took
    # out the whole canonical search path for one stray note.
    score = TIER_WEIGHT.get(metadata.get("type"), 0)
    if score == 0 and metadata.get("type") not in TIER_WEIGHT:
        return 0

    overlap = len(query_set & haystack)
    if overlap:
        score += overlap * 16

    title_overlap = len(query_set & title_terms)
    if title_overlap:
        score += title_overlap * 20

    entity_overlap = len(query_set & entity_terms)
    score += entity_overlap * 10

    domain_overlap = len(query_set & domain_terms)
    if domain_overlap:
        score += domain_overlap * 8

    confidence = metadata.get("confidence")
    if isinstance(confidence, (int, float)):
        score += int(confidence * 20)

    if metadata.get("status") == "active":
        score += 10
    if metadata.get("review_state") == "confirmed":
        score += 8

    if metadata.get("type") == "canonical":
        score += DOMAIN_BOOST.get(metadata.get("domain"), 0)

    updated_at = _parse_dt(metadata.get("updated_at"))
    if updated_at:
        age_days = (datetime.now(UTC) - updated_at).days
        if age_days <= 30:
            score += 6
        elif age_days > 365:
            score -= 8

    if metadata.get("change_policy") == "review_required":
        score += 6

    if metadata.get("type") == "distilled" and metadata.get("review_state") == "proposed":
        score += 4

    return score


import threading as _threading

_notes_cache: list[tuple[Path, dict, str]] | None = None
_notes_mtime_map: dict[str, float] = {}  # path_str -> mtime at last parse
_notes_cache_ts: float = 0.0
_NOTES_TTL: float = 120.0  # 2 minutes
_notes_lock = _threading.Lock()


def collect_notes() -> list[tuple[Path, dict, str]]:
    """Collect canonical + distilled notes with mtime-based incremental rebuild."""
    global _notes_cache, _notes_cache_ts, _notes_mtime_map
    now = time.time()
    with _notes_lock:
        if _notes_cache is not None and (now - _notes_cache_ts) < _NOTES_TTL:
            return _notes_cache

        note_paths = [
            *iter_note_paths(ROOT / "canonical"),
            *iter_note_paths(ROOT / "distilled"),
        ]

        if _notes_cache is None:
            # Cold start — full rebuild
            results: list[tuple[Path, dict, str]] = []
            new_mtime: dict[str, float] = {}
            for p in note_paths:
                try:
                    mt = p.stat().st_mtime
                    metadata, body = parse_markdown_frontmatter(p)
                    results.append((p, metadata, body))
                    new_mtime[str(p)] = mt
                except Exception:
                    continue
            _notes_cache = results
            _notes_mtime_map = new_mtime
            _notes_cache_ts = time.time()
            return results

        # Incremental: detect changed/added/deleted files via mtime
        current_files = {str(p): p for p in note_paths}
        old_paths = set(_notes_mtime_map.keys())
        new_paths = set(current_files.keys())
        deleted = old_paths - new_paths
        added = new_paths - old_paths
        changed: set[str] = set()
        for ps in old_paths & new_paths:
            try:
                if current_files[ps].stat().st_mtime != _notes_mtime_map.get(ps):
                    changed.add(ps)
            except Exception:
                changed.add(ps)

        if not deleted and not added and not changed:
            _notes_cache_ts = time.time()
            return _notes_cache

        # Keep unchanged, re-parse dirty
        dirty = added | changed
        kept = [(p, m, b) for p, m, b in _notes_cache if str(p) not in deleted and str(p) not in dirty]
        new_mtime = {str(p): _notes_mtime_map[str(p)] for p, _, _ in kept if str(p) in _notes_mtime_map}
        for ps in dirty:
            fp = current_files[ps]
            try:
                mt = fp.stat().st_mtime
                metadata, body = parse_markdown_frontmatter(fp)
                kept.append((fp, metadata, body))
                new_mtime[ps] = mt
            except Exception:
                continue
        _notes_cache = kept
        _notes_mtime_map = new_mtime
        _notes_cache_ts = time.time()
        return _notes_cache


def search_notes(
    query: str, limit: int, filter_domain: str | None = None
) -> list[tuple[int, Path, dict, str]]:
    scored: list[tuple[int, Path, dict, str]] = []
    for path, metadata, body in collect_notes():
        score = score_note(query, metadata, body, filter_domain=filter_domain)
        if metadata["type"] in TIER_WEIGHT and score > TIER_WEIGHT[metadata["type"]]:
            scored.append((score, path, metadata, body))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[:limit]


def search_rag(query: str, limit: int, command: Path) -> list[dict[str, Any]]:
    if not command.exists():
        return []
    result = subprocess.run(
        ["python3", str(command), query, "-n", str(limit), "--json"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return payload


_PROPOSAL_BOILERPLATE = (
    "## Statement",
    "Review this proposed canonical note",
    "## Source Summary",
    "## Distilled Evidence",
    "## Observations",
    "- Derived from raw evidence",
    "## Merge Suggestion",
)


def _strip_proposal_boilerplate(body: str) -> str:
    """2026-04-18: line-based strip. Previous version treated `## Source Summary`
    as a section header to skip entirely, which dropped the actual content
    the section holds (shell commands, evidence, summaries). When every
    `## ` header in a file was boilerplate, the section-skip version
    returned empty → fell back to raw body including all stubs. Now just
    drop the boilerplate header/stub LINES themselves and keep everything
    else. Collapse runs of 3+ blank lines to keep output tidy.
    """
    _DROP_EXACT = {
        "## Statement",
        "## Source Summary",
        "## Distilled Evidence",
        "## Observations",
        "## Merge Suggestion",
        "Review this proposed canonical note.",
        "Review this proposed canonical note",
        "- Derived from raw evidence.",
        "- Derived from raw evidence",
    }
    out: list[str] = []
    for line in body.splitlines():
        if line.strip() in _DROP_EXACT:
            continue
        out.append(line)
    cleaned = "\n".join(out).strip()
    import re as _re

    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned if cleaned else body


def _source_aliases_from_metadata(metadata: dict[str, Any], path: Path) -> list[str]:
    """Expose canonical provenance aliases for downstream eval/audit tools."""
    aliases: set[str] = {metadata.get("id") or "", path.stem}
    for key in ("sources", "supersedes", "superseded_by"):
        values = metadata.get(key) or []
        if isinstance(values, str):
            values = [values]
        aliases.update(str(v) for v in values if v)

    for relation in metadata.get("relations", []) or []:
        if not isinstance(relation, dict):
            continue
        rel_type = str(relation.get("type") or "")
        target = relation.get("target")
        if target and rel_type in {"supersedes", "superseded_by", "derived_from", "source"}:
            aliases.add(str(target))

    expanded: set[str] = set()
    for alias in aliases:
        if not alias:
            continue
        expanded.add(alias)
        expanded.add(alias.replace("_", "-"))
        expanded.add(alias.replace("-", "_"))
    return sorted(expanded)


def build_note_hit(score: int, path: Path, metadata: dict[str, Any], body: str) -> dict[str, Any]:
    clean_body = _strip_proposal_boilerplate(body)
    return {
        "kind": "note",
        "rank_score": score,
        "trust_tier": 3 if metadata["type"] == "canonical" else 2,
        "source_type": metadata["type"],
        "id": metadata["id"],
        "title": metadata["title"],
        "path": str(path.relative_to(ROOT)),
        "summary": " ".join(clean_body.split())[:2000],
        "metadata": {
            "domain": metadata.get("domain"),
            "subtype": metadata.get("subtype"),
            "confidence": metadata.get("confidence"),
            "review_state": metadata.get("review_state"),
            "status": metadata.get("status"),
            "change_policy": metadata.get("change_policy"),
            "sources": metadata.get("sources") or [],
            "supersedes": metadata.get("supersedes") or [],
            "superseded_by": metadata.get("superseded_by") or [],
            "relations": metadata.get("relations") or [],
            "source_aliases": _source_aliases_from_metadata(metadata, path),
        },
        "evidence": [],
    }


def build_rag_hit(hit: dict[str, Any]) -> dict[str, Any]:
    rag_score = float(hit.get("score", 0) or 0)
    return {
        "kind": "rag",
        "rank_score": round(40 + (rag_score * 50), 4),
        "trust_tier": 1,
        "source_type": "rag",
        "id": None,
        "title": hit.get("source") or hit.get("collection") or "rag-hit",
        "path": hit.get("source", ""),
        "summary": " ".join(str(hit.get("content", "")).split())[:180],
        "metadata": {
            "collection": hit.get("collection"),
            "score": rag_score,
            "agent": hit.get("agent"),
            "service": hit.get("service"),
        },
        "evidence": [],
    }


def collapse_results(
    note_hits: list[dict[str, Any]], rag_hits: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    merged = sorted(
        note_hits + rag_hits, key=lambda item: (item["rank_score"], item["trust_tier"]), reverse=True
    )
    collapsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    note_titles = {hit["title"].strip().lower() for hit in note_hits}

    for hit in merged:
        dedupe_key = (hit.get("id") or hit.get("path") or hit.get("title") or "").strip().lower()
        if not dedupe_key or dedupe_key in seen:
            continue
        if hit["kind"] == "rag":
            summary = hit["summary"].lower()
            if any(title and title in summary for title in note_titles):
                continue
        seen.add(dedupe_key)
        collapsed.append(hit)
        if len(collapsed) >= limit:
            break

    return collapsed


def package_results(
    query: str, limit: int, include_rag: bool, rag_limit: int, rag_command: Path, domain: str | None = None
) -> dict[str, Any]:
    note_results = [
        build_note_hit(score, path, metadata, body)
        for score, path, metadata, body in search_notes(query, limit, filter_domain=domain)
    ]
    rag_results = (
        [build_rag_hit(hit) for hit in search_rag(query, rag_limit, rag_command)] if include_rag else []
    )
    combined = collapse_results(note_results, rag_results, limit)

    for hit in combined:
        if hit["kind"] == "note" and include_rag:
            hit["evidence"] = [
                {
                    "kind": "rag",
                    "path": rag_hit["path"],
                    "summary": rag_hit["summary"],
                    "score": rag_hit["metadata"]["score"],
                }
                for rag_hit in rag_results[:2]
            ]
    return {
        "query": query,
        "results": combined,
        "notes": note_results,
        "rag": rag_results,
        "filter_domain": domain,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search personal intelligence notes with canonical-first ranking"
    )
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--include-rag", action="store_true")
    parser.add_argument("--rag-limit", type=int, default=3)
    parser.add_argument("--rag-command", default=str(DEFAULT_RAG_SEARCH))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--domain",
        default=None,
        choices=["chris", "projects", "infra", "decisions", "incidents"],
        help="limit note search to a specific domain",
    )
    args = parser.parse_args()

    payload = package_results(
        args.query, args.limit, args.include_rag, args.rag_limit, Path(args.rag_command), domain=args.domain
    )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    for hit in payload["results"]:
        if hit["kind"] == "note":
            print(f"{hit['rank_score']}\t{hit['source_type']}\t{hit['id']}\t{hit['path']}\t{hit['title']}")
        else:
            collection = hit["metadata"].get("collection", "")
            score = hit["metadata"].get("score", "")
            print(f"{hit['rank_score']}\trag\t{collection}\t{score}\t{hit['path']}\t{hit['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
