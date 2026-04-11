from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from common import ROOT, iter_note_paths, parse_markdown_frontmatter, tokenize

CANONICAL_ROOT = ROOT / "canonical"


def existing_proposal_sources(review_queue: Path) -> set[str]:
    seen: set[str] = set()
    for path in iter_note_paths(review_queue):
        metadata, _body = parse_markdown_frontmatter(path)
        for source in metadata.get("sources", []):
            seen.add(source)
    return seen


def existing_queue_signatures(review_queue: Path) -> list[tuple[str, str, list[str], str]]:
    signatures: list[tuple[str, str, list[str], str]] = []
    rejected_dir = review_queue / "rejected"
    for path in iter_note_paths(review_queue):
        if path.parent == rejected_dir:
            continue
        metadata, body = parse_markdown_frontmatter(path)
        if metadata.get("type") != "canonical" or metadata.get("review_state") != "proposed":
            continue
        signatures.append(
            (metadata["id"], metadata.get("title", ""), list(metadata.get("entities", [])), body)
        )
    return signatures


def _ratio(a: str, b: str) -> float:
    a_tokens = set(tokenize(a))
    b_tokens = set(tokenize(b))
    if not a_tokens and not b_tokens:
        return 0.0
    union = len(a_tokens | b_tokens)
    if union == 0:
        return 0.0
    return len(a_tokens & b_tokens) / union


def _merge_or_duplicate_score(
    a_title: str,
    a_body: str,
    a_entities: list[str],
    b_title: str,
    b_body: str,
    b_entities: list[str],
) -> float:
    title_score = _ratio(a_title, b_title)
    body_score = _ratio(f"{a_title} {a_body}", f"{b_title} {b_body}")
    entity_overlap = len(set(a_entities) & set(b_entities))
    return max(title_score, body_score * 0.7) + (0.18 if entity_overlap else 0)


def find_merge_targets(
    metadata: dict[str, Any],
    body: str,
    *,
    domain: str,
    limit: int,
    score_threshold: float,
) -> list[str]:
    candidates: list[tuple[str, float]] = []
    combined_text = f"{metadata.get('title', '')} {body}"
    for note_path in iter_note_paths(CANONICAL_ROOT):
        canonical_metadata, canonical_body = parse_markdown_frontmatter(note_path)
        if canonical_metadata.get("type") != "canonical":
            continue
        if canonical_metadata.get("domain") != domain:
            continue
        title_score = _ratio(metadata.get("title", ""), canonical_metadata.get("title", ""))
        body_score = _ratio(combined_text, f"{canonical_metadata.get('title', '')} {canonical_body}")
        entity_overlap = len(set(metadata.get("entities", [])) & set(canonical_metadata.get("entities", [])))

        score = max(title_score, body_score * 0.6)
        if entity_overlap:
            score += 0.2
        if score >= score_threshold:
            candidates.append((canonical_metadata["id"], score))

    candidates.sort(key=lambda item: item[1], reverse=True)
    return [note_id for note_id, _score in candidates[:limit]]


def find_duplicate_proposal(
    metadata: dict[str, Any],
    body: str,
    existing_signatures: list[tuple[str, str, list[str], str]],
    *,
    score_threshold: float,
) -> str | None:
    candidate_title = metadata.get("title", "")
    candidate_entities = list(metadata.get("entities", []))
    for proposal_id, title, entities, existing_body in existing_signatures:
        score = _merge_or_duplicate_score(candidate_title, body, candidate_entities, title, existing_body, entities)
        if score >= score_threshold:
            return proposal_id
    return None


def quality_reject_reason(metadata: dict[str, Any], min_confidence: float, min_sources: int) -> list[str]:
    reasons: list[str] = []
    confidence = float(metadata.get("confidence", 0) or 0)
    if confidence < min_confidence:
        reasons.append(f"low_confidence_{confidence:.2f}")
    if len(metadata.get("sources", [])) < min_sources:
        reasons.append("low_source_count")
    if metadata.get("status") != "active":
        reasons.append("status_not_active")
    return reasons


def build_proposal(
    metadata: dict[str, Any],
    body: str,
    *,
    merge_candidates: list[str],
    max_summary_len: int = 280,
) -> tuple[dict[str, Any], str]:
    proposal = {
        "id": metadata["id"].replace("dist_", "proposal_", 1),
        "type": "canonical",
        "domain": metadata["domain"],
        "subtype": metadata["subtype"],
        "title": metadata["title"],
        "status": "draft",
        "visibility": metadata["visibility"],
        "confidence": metadata["confidence"],
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
        "last_reviewed_at": metadata["updated_at"],
        "owner": "system",
        "scope": "global",
        "valid_from": None,
        "valid_to": None,
        "sources": metadata["sources"] + [metadata["id"]],
        "provenance_summary": metadata.get("provenance_summary", "Derived from distilled note"),
        "entities": metadata["entities"],
        "relations": metadata["relations"],
        "review_state": "proposed",
        "change_policy": "review_required",
        "supersedes": merge_candidates,
        "superseded_by": None,
    }

    proposal_body = (
        "## Statement\n\nReview this proposed canonical note.\n\n"
        f"## Source Summary\n\n{body}\n\n"
        "## Distilled Evidence\n\n"
        + " ".join(body.split())[:max_summary_len]
        + "\n"
    )
    if merge_candidates:
        proposal_body += "\n## Merge Suggestion\n\nPotential overlap with existing canonical note(s):\n" + "\n".join(
            [f"- {note_id}" for note_id in merge_candidates]
        )

    return proposal, proposal_body


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-generate canonical proposals from distilled notes")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "distilled")
    parser.add_argument("--review-queue", type=Path, default=ROOT / "reports" / "review-queue")
    parser.add_argument("--manifest", type=Path, default=ROOT / "reports" / "review-queue" / "batch_propose_manifest.json")
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--min-sources", type=int, default=1)
    parser.add_argument("--max-merge-hints", type=int, default=2)
    parser.add_argument("--merge-threshold", type=float, default=0.45)
    parser.add_argument("--duplicate-threshold", type=float, default=0.82)
    args = parser.parse_args()

    seen_sources = existing_proposal_sources(args.review_queue)
    queue_signatures = existing_queue_signatures(args.review_queue)
    created: list[str] = []
    skipped: list[str] = []
    skipped_reasons: dict[str, list[str]] = {}
    domain_rejections = Counter()

    for path in iter_note_paths(args.input_dir):
        metadata, body = parse_markdown_frontmatter(path)
        if metadata.get("type") != "distilled":
            continue

        source_key = metadata["id"]
        if source_key in seen_sources:
            skipped.append(source_key)
            skipped_reasons[source_key] = ["already_in_queue"]
            continue

        reasons = quality_reject_reason(metadata, args.min_confidence, args.min_sources)
        if reasons:
            skipped.append(source_key)
            skipped_reasons[source_key] = reasons
            domain_rejections[metadata.get("domain", "unknown")] += 1
            continue

        duplicate_of = find_duplicate_proposal(
            metadata,
            body,
            queue_signatures,
            score_threshold=args.duplicate_threshold,
        )
        if duplicate_of:
            skipped.append(source_key)
            skipped_reasons[source_key] = [f"duplicate_of:{duplicate_of}"]
            domain_rejections[metadata.get("domain", "unknown")] += 1
            continue

        merge_candidates = find_merge_targets(
            metadata,
            body,
            domain=metadata["domain"],
            limit=args.max_merge_hints,
            score_threshold=args.merge_threshold,
        )
        proposal, proposal_body = build_proposal(metadata, body, merge_candidates=merge_candidates)

        target = args.review_queue / f"{proposal['id']}.md"
        if target.exists():
            skipped.append(source_key)
            skipped_reasons[source_key] = ["target_exists"]
            domain_rejections[metadata.get("domain", "unknown")] += 1
            continue

        from common import write_markdown_frontmatter

        write_markdown_frontmatter(target, proposal, proposal_body)
        created.append(str(target))
        seen_sources.add(source_key)
        queue_signatures.append((proposal["id"], proposal["title"], proposal["entities"], proposal_body))

    manifest = {
        "status": "ok",
        "input_dir": str(args.input_dir),
        "review_queue": str(args.review_queue),
        "created": created,
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "domain_rejection_count": dict(domain_rejections),
        "config": {
            "min_confidence": args.min_confidence,
            "min_sources": args.min_sources,
            "max_merge_hints": args.max_merge_hints,
            "merge_threshold": args.merge_threshold,
            "duplicate_threshold": args.duplicate_threshold,
        },
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
