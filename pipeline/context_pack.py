from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from search_memory import DEFAULT_RAG_SEARCH, package_results


def build_context_block(payload: dict[str, Any], max_results: int = 5, max_evidence: int = 2) -> str:
    lines: list[str] = []
    lines.append(f"# Memory Context")
    lines.append(f"Query: {payload['query']}")
    lines.append("")
    lines.append("Use canonical notes as current truth. Use distilled notes as supporting context. Use RAG evidence as fallback evidence, not source-of-truth.")
    lines.append("")

    for index, hit in enumerate(payload.get("results", [])[:max_results], start=1):
        lines.append(f"## Result {index} — {hit['source_type']} — {hit['title']}")
        lines.append(f"Path: {hit['path']}")
        if hit.get("id"):
            lines.append(f"ID: {hit['id']}")
        metadata = hit.get("metadata", {})
        lines.append(
            f"Meta: domain={metadata.get('domain')} subtype={metadata.get('subtype')} confidence={metadata.get('confidence')} status={metadata.get('status')} review_state={metadata.get('review_state')}"
        )
        lines.append(f"Summary: {hit['summary']}")
        evidence = hit.get("evidence", [])[:max_evidence]
        if evidence:
            lines.append("Evidence:")
            for evidence_item in evidence:
                lines.append(
                    f"- {evidence_item.get('path')}: {evidence_item.get('summary')} (score={evidence_item.get('score')})"
                )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack merged search results into an LLM-ready context block")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--include-rag", action="store_true")
    parser.add_argument("--rag-limit", type=int, default=3)
    parser.add_argument("--rag-command", default=str(DEFAULT_RAG_SEARCH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = package_results(args.query, args.limit, args.include_rag, args.rag_limit, Path(args.rag_command))
    context = build_context_block(payload, max_results=args.limit)
    if args.json:
        print(json.dumps({"query": args.query, "context": context, "results": payload["results"]}, indent=2, ensure_ascii=False))
        return 0

    print(context, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
