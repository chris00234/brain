#!/usr/bin/env python3
"""Fast policy gate for ontology query expansion.

This complements the retrieval A/B gate by checking expansion semantics directly:
intent gating, inverse-only relations, and typed Neo4j edge guards. It is small
enough to run before every ontology expansion rollout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = ROOT / "brain_core"
if str(BRAIN_CORE) not in sys.path:
    sys.path.insert(0, str(BRAIN_CORE))

import search_unified  # noqa: E402

DEFAULT_CASES: list[dict[str, Any]] = [
    {
        "query": "nginx proxy route",
        "must_include": [],
        "must_exclude": ["searxng"],
        "why": "proxies expands inverse-only; nginx must not fan out to every proxied service",
    },
    {
        "query": "searxng proxy route",
        "must_include": ["nginx"],
        "must_exclude": [],
        "why": "service behind nginx should expand back to the proxy host",
    },
    {
        "query": "neo4j dependency",
        "include_any": ["brain server", "brain system", "rag stack"],
        "must_exclude": [],
        "why": "dependency queries may expand from dependency to dependent systems",
    },
    {
        "query": "brain system dependency",
        "must_include": [],
        "must_exclude": ["neo4j", "qdrant"],
        "why": "systems should not expand outward to every dependency",
    },
    {
        "query": "MCC owner",
        "must_include": ["chris cho"],
        "must_exclude": [],
        "why": "ownership remains in the base allowlist and should not need manages intent",
    },
    {
        "query": "Jenna responsibilities",
        "must_include": [],
        "must_exclude": ["chris cho"],
        "why": "responsibility phrasing alone must not invert has_agent into owner spam",
    },
    {
        "query": "Chris Cho agents",
        "include_any": ["jenna", "liz", "ellie"],
        "must_exclude": [],
        "why": "person-to-agent relation stays useful for agent lookup",
    },
]


def _reset_cache() -> None:
    search_unified._ontology_cache = None
    search_unified._ontology_cache_ts = 0.0


def _terms_for(query: str) -> tuple[str, list[str], int, tuple[str, ...]]:
    expanded, terms, elapsed_ms = search_unified.maybe_expand_query_with_ontology(query)
    return (
        expanded,
        [str(term).lower() for term in terms],
        elapsed_ms,
        search_unified.ontology_relations_for_query(query),
    )


def _case_result(case: dict[str, Any]) -> dict[str, Any]:
    query = str(case["query"])
    expanded, terms, elapsed_ms, relations = _terms_for(query)
    failures: list[str] = []

    for expected in case.get("must_include") or []:
        expected_l = str(expected).lower()
        if expected_l not in terms:
            failures.append(f"missing required term: {expected_l}")

    include_any = [str(term).lower() for term in case.get("include_any") or []]
    if include_any and not any(term in terms for term in include_any):
        failures.append(f"missing any acceptable term: {include_any}")

    for forbidden in case.get("must_exclude") or []:
        forbidden_l = str(forbidden).lower()
        if forbidden_l in terms:
            failures.append(f"forbidden term present: {forbidden_l}")

    return {
        "query": query,
        "relations": list(relations),
        "terms": terms,
        "expanded_query": expanded,
        "elapsed_ms": elapsed_ms,
        "why": case.get("why", ""),
        "passed": not failures,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast ontology expansion policy gate")
    parser.add_argument("--source", choices=["neo4j", "file"], default="neo4j")
    parser.add_argument(
        "--relations",
        default="has_agent,owned_by,owns",
        help="Base comma-separated expansion allowlist.",
    )
    parser.add_argument("--max-terms", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    search_unified.BRAIN_ONTOLOGY_EXPANSION_ENABLED = True
    search_unified.BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED = True
    search_unified.BRAIN_ONTOLOGY_EXPANSION_SOURCE = args.source
    search_unified.BRAIN_ONTOLOGY_EXPANSION_RELATIONS = tuple(
        rel.strip() for rel in args.relations.split(",") if rel.strip()
    )
    search_unified.BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS = args.max_terms
    _reset_cache()

    results = [_case_result(case) for case in DEFAULT_CASES]
    report = {
        "source": search_unified.BRAIN_ONTOLOGY_EXPANSION_SOURCE,
        "base_relations": list(search_unified.BRAIN_ONTOLOGY_EXPANSION_RELATIONS),
        "conditional": bool(search_unified.BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED),
        "cases": len(results),
        "passed": all(result["passed"] for result in results),
        "results": results,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for result in results:
            status = "PASS" if result["passed"] else "FAIL"
            print(f"{status} {result['query']} -> {result['terms']}")
            for failure in result["failures"]:
                print(f"  - {failure}")
        print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False))

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
