from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import ROOT, iter_note_paths, parse_markdown_frontmatter

REVIEW_QUEUE = ROOT / "reports" / "review-queue"
REJECTED_DIR = REVIEW_QUEUE / "rejected"

DOMAIN_WEIGHT = {
    "decisions": 30,
    "infra": 24,
    "incidents": 20,
    "projects": 12,
    "chris": 8,
}


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def score_proposal(metadata: dict[str, Any], body: str) -> tuple[int, str, list[str]]:
    score = 0
    reasons: list[str] = []

    source_count = len(metadata.get("sources", []))
    if source_count >= 2:
        score += 22
        reasons.append("multiple_sources")
    elif source_count == 1:
        score += 12
        reasons.append("single_source")

    confidence = float(metadata.get("confidence", 0) or 0)
    score += int(confidence * 35)
    if confidence >= 0.90:
        reasons.append("high_confidence")
    if confidence >= 0.75:
        reasons.append("solid_confidence")

    domain = metadata.get("domain", "chris")
    score += DOMAIN_WEIGHT.get(domain, 8)
    if domain in DOMAIN_WEIGHT:
        reasons.append(f"domain_{domain}")

    if metadata.get("change_policy") == "review_required":
        score += 12
        reasons.append("review_required")
    if metadata.get("change_policy") == "manual_only":
        score -= 10
        reasons.append("manual_only")

    updated_at = _parse_dt(metadata.get("updated_at"))
    if updated_at:
        age_days = (datetime.now(UTC) - updated_at).days
        if age_days <= 14:
            score += 8
            reasons.append("fresh")
        elif age_days >= 180:
            score -= 12
            reasons.append("stale")

    text = (metadata.get("provenance_summary", "") + " " + body).lower()
    if "explicit" in text or "standing" in text or "repeated" in text:
        score += 10
        reasons.append("strong_provenance_language")
    if "hypothesis" in text or "maybe" in text or "likely" in text:
        score -= 6
        reasons.append("hedge_language")

    if metadata.get("supersedes"):
        score += min(10, 4 * len(metadata.get("supersedes", [])))
        reasons.append("merge_candidate")

    if not metadata.get("sources"):
        score -= 20
        reasons.append("no_sources")

    # ADM counterfactual verification: would this knowledge have helped in past interactions?
    cf_bonus = _counterfactual_check(metadata, body)
    if cf_bonus > 0:
        score += cf_bonus
        reasons.append("counterfactual_verified")
    elif cf_bonus < 0:
        score += cf_bonus
        reasons.append("counterfactual_weak")

    # 2026-04-16 Tier 3 #6: Bayesian Surprise (Itti & Baldi 2009). A
    # proposal that merely restates canonical knowledge is low-priority;
    # one that contradicts/extends a highly-held belief is high-priority.
    # Operationalized via embedding-space distance to the nearest
    # canonical note: low distance (cos sim > 0.85) → redundant → penalty,
    # mid distance (0.55–0.85) → extends something → bonus,
    # high distance (< 0.55) → entirely new territory → small bonus.
    # All canonical trust tiers treated equally here; the real "surprise"
    # signal is how much the posterior belief shifts, approximated by
    # how different the proposal is from its nearest existing claim.
    try:
        surprise_delta = _bayesian_surprise(metadata, body)
        if surprise_delta:
            score += surprise_delta
            if surprise_delta > 0:
                reasons.append(f"surprise_bonus_{surprise_delta}")
            else:
                reasons.append(f"redundancy_penalty_{surprise_delta}")
    except Exception:
        pass

    return score, "high" if score >= 65 else ("medium" if score >= 42 else "low"), reasons


def _bayesian_surprise(metadata: dict[str, Any], body: str) -> int:
    """Approximate Itti & Baldi's surprise via embedding-space distance.

    Searches canonical for the nearest existing claim; returns a score
    delta in {-15, 0, +5, +10} based on cosine similarity band.
    """
    try:
        import sys as _s

        _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from search_unified import search_all

        title = metadata.get("title", "")
        head = body[:500]
        query = f"{title}\n{head}"
        if not query.strip():
            return 0
        payload = search_all(
            query,
            limit=3,
            sources=["canonical"],
        )
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not results:
            # No canonical to compare against → novel but unverifiable. Small bonus.
            return 3
        # Use the best canonical match's normalized score as inverse surprise.
        # vector_score is the post-search normalized [0,1] cosine similarity.
        top = results[0]
        meta = top.get("metadata") or {}
        sim = float(meta.get("vector_score", 0) or top.get("vector_score", 0) or 0)
        if sim >= 0.85:
            return -15  # near-duplicate of existing canonical → redundant
        if sim >= 0.55:
            return 10  # extends/refines an existing belief → high surprise
        return 5  # entirely new territory → moderate surprise
    except Exception:
        return 0


def _counterfactual_check(metadata: dict[str, Any], body: str) -> int:
    """ADM pattern: check if this knowledge would have helped in past experiences.

    Returns bonus points (positive = verified useful, negative = not useful, 0 = couldn't check).
    """
    try:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from search_unified import search_all

        title = metadata.get("title", "")
        results = search_all(
            title,
            limit=5,
            sources=["rag"],
            collections=["experience"],
        )
        result_list = results.get("results", []) if isinstance(results, dict) else []
        if len(result_list) < 2:
            return 0  # not enough experience data to verify

        # Count how many past experiences are semantically related (score > 40)
        related = sum(1 for r in result_list if r.get("score", 0) > 40)
        if related >= 3:
            return 15  # strong signal: this knowledge relates to many past experiences
        if related >= 1:
            return 5  # moderate signal
        return -5  # no past experiences relate — this knowledge may be speculative
    except Exception:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Score proposal queue items for review priority")
    parser.add_argument("--review-queue", type=Path, default=REVIEW_QUEUE)
    parser.add_argument("--report", type=Path, default=REVIEW_QUEUE / "proposal_score_report.json")
    parser.add_argument(
        "--domain", default=None, choices=["chris", "projects", "infra", "decisions", "incidents"]
    )
    parser.add_argument("--min-urgency", choices=["low", "medium", "high"])
    args = parser.parse_args()

    items: list[dict[str, Any]] = []
    for path in iter_note_paths(args.review_queue):
        if path.parent == REJECTED_DIR:
            continue
        metadata, body = parse_markdown_frontmatter(path)
        if metadata.get("type") != "canonical" or metadata.get("review_state") != "proposed":
            continue

        if args.domain and metadata.get("domain") != args.domain:
            continue

        score, urgency, reasons = score_proposal(metadata, body)
        if args.min_urgency == "high" and urgency != "high":
            continue
        if args.min_urgency == "medium" and urgency == "low":
            continue
        items.append(
            {
                "id": metadata["id"],
                "title": metadata["title"],
                "domain": metadata.get("domain"),
                "status": metadata.get("status"),
                "path": str(path.relative_to(ROOT)),
                "score": score,
                "urgency": urgency,
                "reasons": reasons,
                "merge_candidates": list(metadata.get("supersedes", [])),
            }
        )

    items.sort(key=lambda item: item["score"], reverse=True)
    payload = {
        "status": "ok",
        "count": len(items),
        "items": items,
        "top": items[0]["id"] if items else None,
        "high_priority_count": len([item for item in items if item["urgency"] == "high"]),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
