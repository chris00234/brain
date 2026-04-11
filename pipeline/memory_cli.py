from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from common import ROOT, iter_note_paths, load_json, parse_markdown_frontmatter
from context_pack import build_context_block
from search_memory import DEFAULT_RAG_SEARCH, package_results

NOTE_ROOTS = (ROOT / "canonical", ROOT / "distilled", ROOT / "reports" / "review-queue")
RAW_ROOT = ROOT / "raw"


def print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def resolve_note(identifier: str, *, expected_type: str | None = None) -> tuple[Path, dict[str, Any], str]:
    candidate = Path(identifier)
    if candidate.exists():
        path = candidate.resolve()
        metadata, body = parse_markdown_frontmatter(path)
    else:
        for root in NOTE_ROOTS:
            for path in iter_note_paths(root):
                metadata, body = parse_markdown_frontmatter(path)
                if metadata.get("id") == identifier:
                    break
            else:
                continue
            break
        else:
            raise SystemExit(f"Note not found: {identifier}")

    if expected_type and metadata.get("type") != expected_type:
        raise SystemExit(f"Expected {expected_type} note, got {metadata.get('type')}")
    return path, metadata, body


def resolve_raw(identifier: str) -> tuple[Path, dict[str, Any]] | None:
    candidate = Path(identifier)
    if candidate.exists() and candidate.suffix == ".json":
        payload = load_json(candidate.resolve())
        return candidate.resolve(), payload

    for path in sorted(RAW_ROOT.rglob("*.json")):
        payload = load_json(path)
        if payload.get("id") == identifier:
            return path, payload
    return None


def run_subprocess(script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    script_path = Path(__file__).resolve().parent / script_name
    return subprocess.run(["python3", str(script_path), *args], cwd=ROOT, text=True, capture_output=True, check=False)


def parse_output_path(stdout: str) -> Path:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise SystemExit("No output path returned by sub-command")
    return Path(lines[-1]).resolve()


def command_search(args: argparse.Namespace) -> int:
    payload = package_results(
        args.query,
        args.limit,
        args.include_rag,
        args.rag_limit,
        Path(args.rag_command),
        domain=args.domain,
    )

    if args.json:
        print_json(payload)
        return 0

    for hit in payload["results"]:
        if hit["kind"] == "note":
            print(f"{hit['rank_score']}\t{hit['source_type']}\t{hit['id']}\t{hit['path']}\t{hit['title']}")
        else:
            collection = hit["metadata"].get("collection", "")
            score = hit["metadata"].get("score", "")
            print(f"{hit['rank_score']}\trag\t{collection}\t{score}\t{hit['path']}\t{hit['summary']}")
    return 0


def command_get(args: argparse.Namespace) -> int:
    raw_match = resolve_raw(args.identifier)
    if raw_match:
        path, payload = raw_match
        print_json({"kind": "raw", "path": str(path.relative_to(ROOT)), "metadata": payload, "body": payload.get("content", "")})
        return 0

    path, metadata, body = resolve_note(args.identifier)
    print_json({"kind": metadata["type"], "path": str(path.relative_to(ROOT)), "metadata": metadata, "body": body})
    return 0


def command_context(args: argparse.Namespace) -> int:
    payload = package_results(
        args.query,
        args.limit,
        args.include_rag,
        args.rag_limit,
        Path(args.rag_command),
        domain=args.domain,
    )
    context = build_context_block(payload, max_results=args.limit)
    if args.json:
        print_json({"query": args.query, "context": context, "results": payload["results"]})
        return 0
    print(context, end="")
    return 0


def command_propose(args: argparse.Namespace) -> int:
    distilled_path, metadata, _body = resolve_note(args.distilled_note, expected_type="distilled")
    result = run_subprocess("propose_canonical.py", [str(distilled_path)])
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        return result.returncode

    proposal_path = parse_output_path(result.stdout)
    proposal_metadata, proposal_body = parse_markdown_frontmatter(proposal_path)
    if args.json:
        print_json(
            {
                "source_id": metadata["id"],
                "proposal_id": proposal_metadata["id"],
                "path": str(proposal_path.relative_to(ROOT)),
                "metadata": proposal_metadata,
                "body": proposal_body,
            }
        )
        return 0

    print(proposal_path.relative_to(ROOT))
    return 0


def command_promote(args: argparse.Namespace) -> int:
    proposal_path, _metadata, _body = resolve_note(args.proposal, expected_type="canonical")
    command_args = [str(proposal_path), "--owner", args.owner, "--scope", args.scope]
    if args.target_id:
        command_args.extend(["--target-id", args.target_id])
    for note_id in args.supersede:
        command_args.extend(["--supersede", note_id])

    result = run_subprocess("promote_canonical.py", command_args)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        return result.returncode

    canonical_path = parse_output_path(result.stdout)
    canonical_metadata, canonical_body = parse_markdown_frontmatter(canonical_path)
    if args.json:
        print_json(
            {
                "path": str(canonical_path.relative_to(ROOT)),
                "metadata": canonical_metadata,
                "body": canonical_body,
            }
        )
        return 0

    print(canonical_path.relative_to(ROOT))
    return 0


def command_review_list(args: argparse.Namespace) -> int:
    result = run_subprocess("review_queue.py", ["list", "--json"] if args.json else ["list"])
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    return result.returncode


def command_review_reject(args: argparse.Namespace) -> int:
    command_args = ["reject", args.proposal, "--reviewer", args.reviewer, "--reason", args.reason]
    if args.json:
        command_args.append("--json")
    result = run_subprocess("review_queue.py", command_args)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    return result.returncode


def command_review_score(args: argparse.Namespace) -> int:
    command_args = []
    if args.report:
        command_args.extend(["--report", args.report])
    if args.domain:
        command_args.extend(["--domain", args.domain])
    if args.min_urgency:
        command_args.extend(["--min-urgency", args.min_urgency])
    result = run_subprocess("score_proposals.py", command_args)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    return result.returncode


def command_observability(args: argparse.Namespace) -> int:
    command_args = [
        "--out",
        args.out,
    ]
    if args.review_queue:
        command_args.extend(["--review-queue", args.review_queue])
    result = run_subprocess("memory_observability.py", command_args)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Thin agent access layer for personal intelligence memory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="search notes with optional RAG fallback")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=5)
    search_parser.add_argument("--include-rag", action="store_true")
    search_parser.add_argument("--rag-limit", type=int, default=3)
    search_parser.add_argument("--rag-command", default=str(DEFAULT_RAG_SEARCH))
    search_parser.add_argument("--domain", choices=["chris", "projects", "infra", "decisions", "incidents"])
    search_parser.add_argument("--json", action="store_true")
    search_parser.set_defaults(func=command_search)

    get_parser = subparsers.add_parser("get", help="get note or raw record by id or path")
    get_parser.add_argument("identifier")
    get_parser.set_defaults(func=command_get)

    context_parser = subparsers.add_parser("context", help="build LLM-ready context from merged retrieval results")
    context_parser.add_argument("query")
    context_parser.add_argument("--limit", type=int, default=5)
    context_parser.add_argument("--include-rag", action="store_true")
    context_parser.add_argument("--rag-limit", type=int, default=3)
    context_parser.add_argument("--rag-command", default=str(DEFAULT_RAG_SEARCH))
    context_parser.add_argument("--domain", choices=["chris", "projects", "infra", "decisions", "incidents"])
    context_parser.add_argument("--json", action="store_true")
    context_parser.set_defaults(func=command_context)

    propose_parser = subparsers.add_parser("propose", help="create proposal from distilled note id or path")
    propose_parser.add_argument("distilled_note")
    propose_parser.add_argument("--json", action="store_true")
    propose_parser.set_defaults(func=command_propose)

    promote_parser = subparsers.add_parser("promote", help="promote proposal id or path into canonical store")
    promote_parser.add_argument("proposal")
    promote_parser.add_argument("--owner", default="chris")
    promote_parser.add_argument("--scope", default="global", choices=["global", "project", "time-bounded"])
    promote_parser.add_argument("--target-id")
    promote_parser.add_argument("--supersede", action="append", default=[])
    promote_parser.add_argument("--json", action="store_true")
    promote_parser.set_defaults(func=command_promote)

    review_parser = subparsers.add_parser("review-list", help="list pending proposal queue items")
    review_parser.add_argument("--json", action="store_true")
    review_parser.set_defaults(func=command_review_list)

    reject_parser = subparsers.add_parser("review-reject", help="reject a pending proposal")
    reject_parser.add_argument("proposal")
    reject_parser.add_argument("--reviewer", default="chris")
    reject_parser.add_argument("--reason", default="rejected")
    reject_parser.add_argument("--json", action="store_true")
    reject_parser.set_defaults(func=command_review_reject)

    score_parser = subparsers.add_parser("review-score", help="score pending proposals for review priority")
    score_parser.add_argument("--report")
    score_parser.add_argument("--domain", choices=["chris", "projects", "infra", "decisions", "incidents"])
    score_parser.add_argument("--min-urgency", choices=["low", "medium", "high"])
    score_parser.set_defaults(func=command_review_score)

    observability_parser = subparsers.add_parser("observability", help="generate pipeline observability metrics")
    observability_parser.add_argument("--review-queue", default=str(ROOT / "reports" / "review-queue"))
    observability_parser.add_argument("--out", default=str(ROOT / "reports" / "review-queue" / "observability_report.json"))
    observability_parser.set_defaults(func=command_observability)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
