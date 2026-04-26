#!/Users/chrischo/server/brain/.venv/bin/python3
"""cli/eval_proposal_triage.py — LLM-based auto-triage for candidate eval_proposals.

Runs daily at 04:20. For each proposal in status='candidate', dispatches a
stateless CLI LLM call (codex CLI, subscription-backed) that returns a
JSON verdict {approve: bool, confidence: float, reason: str}. Proposals
with confidence >= AUTO_CONFIDENCE_THRESHOLD are auto-marked; the rest
remain candidates for human review.

Why CLI not Sage:
- This is pure classification, no file writes, no workspace state needed.
- ~2-3s per proposal via codex, $0 (subscription). Sage would be overkill.
- Sage handles *writing* canonical resolutions for contradictions; triage
  just decides if a proposal should enter the eval holdout corpus.

Safety:
- Dry-run by default. --apply flips to actual status change.
- Empty/malformed proposals auto-rejected (structural heuristic, not LLM).
- Low-confidence (< AUTO_CONFIDENCE_THRESHOLD) stays as candidate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

try:
    from cli_llm import cli_dispatch_with_schema

    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    cli_dispatch_with_schema = None  # type: ignore[assignment]

AUTO_CONFIDENCE_THRESHOLD = 0.8
MAX_PROPOSALS_PER_RUN = 30
TRIAGE_TIMEOUT_S = 30


SCHEMA = (
    '{"approve": boolean, "confidence": number (0.0-1.0), '
    '"reason": string (max 200 chars), "rationale_tag": string (one of: '
    '"current_fact_confirmed", "duplicate_of_approved", "stale_no_longer_relevant", '
    '"malformed_or_empty", "needs_human_review")}'
)


PROMPT_TEMPLATE = """You are triaging an `eval_proposal` candidate for Chris Cho's personal brain system.

Context about the system:
- `eval_proposals` are candidate queries for the eval holdout test set.
- `candidate` means pending triage. `promoted` means accepted into eval corpus.
- Proposals come from `brain_loop` observation sensors (contradictions, recall misses, breaker events).
- Chris wants to auto-approve proposals that capture a real, currently-true fact about his system,
  auto-reject duplicates or stale/malformed rows, and leave ambiguous ones for manual review.

Proposal metadata:
  id: {pid}
  source_event: {source}
  confidence_claimed: {confidence}
  query: {query}
  expected_evidence: {expected}

Decide: should this proposal be APPROVED (promoted to eval corpus) or REJECTED?

Rules:
- APPROVE (approve=true) if: the proposal describes a CURRENTLY true fact about Chris's brain/infra
  that would be useful as a retrieval test query.
- REJECT (approve=false) if: query is empty/garbage, subject is duplicate-of-approved, claim is
  already stale/outdated, or it's a one-off operational event (breaker_open that resolved) rather
  than durable knowledge.
- UNCERTAIN: return approve=false with rationale_tag="needs_human_review" and low confidence (<0.5)
  so a human can decide.

Confidence reflects how certain you are of your approve/reject call."""


def _fetch_candidates(limit: int) -> list[dict]:
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, query, expected, source_event, confidence, created_at "
            "FROM eval_proposals WHERE status='candidate' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _mark_status(pid: str, status: str) -> bool:
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        cur = conn.execute(
            "UPDATE eval_proposals SET status=? WHERE id=?",
            (status, pid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _is_structurally_empty(proposal: dict) -> bool:
    """Fast heuristic: empty query + no evidence = garbage row."""
    q = (proposal.get("query") or "").strip()
    ev = (proposal.get("expected") or "").strip()
    return len(q) < 5 and len(ev) < 20


def _triage_one(proposal: dict) -> dict:
    if _is_structurally_empty(proposal):
        return {
            "approve": False,
            "confidence": 0.95,
            "reason": "empty query + no evidence",
            "rationale_tag": "malformed_or_empty",
            "source": "structural",
        }
    if cli_dispatch_with_schema is None:
        return {
            "approve": False,
            "confidence": 0.0,
            "reason": "cli_llm unavailable",
            "rationale_tag": "needs_human_review",
            "source": "cli_unavailable",
        }
    prompt = PROMPT_TEMPLATE.format(
        pid=proposal["id"],
        source=proposal.get("source_event", ""),
        confidence=proposal.get("confidence", 0),
        query=(proposal.get("query") or "")[:500],
        expected=(proposal.get("expected") or "")[:800],
    )
    result = cli_dispatch_with_schema(
        prompt,
        schema_description=SCHEMA,
        timeout=TRIAGE_TIMEOUT_S,
    )
    if result is None:
        return {
            "approve": False,
            "confidence": 0.0,
            "reason": "LLM triage failed",
            "rationale_tag": "needs_human_review",
            "source": "llm_error",
        }
    result["source"] = "llm"
    return result


def run(apply_changes: bool = False, limit: int = MAX_PROPOSALS_PER_RUN) -> dict:
    candidates = _fetch_candidates(limit)
    if not candidates:
        return {
            "status": "ok",
            "scanned": 0,
            "approved": 0,
            "rejected": 0,
            "held": 0,
            "dry_run": not apply_changes,
        }

    approved: list[dict] = []
    rejected: list[dict] = []
    held: list[dict] = []

    for p in candidates:
        verdict = _triage_one(p)
        confidence = float(verdict.get("confidence") or 0)
        entry = {
            "id": p["id"],
            "approve": bool(verdict.get("approve")),
            "confidence": round(confidence, 3),
            "reason": (verdict.get("reason") or "")[:200],
            "tag": verdict.get("rationale_tag", ""),
            "source": verdict.get("source", ""),
        }
        if confidence < AUTO_CONFIDENCE_THRESHOLD:
            held.append(entry)
            continue
        if entry["approve"]:
            if apply_changes:
                _mark_status(p["id"], "promoted")
            approved.append(entry)
        else:
            if apply_changes:
                _mark_status(p["id"], "rejected")
            rejected.append(entry)

    return {
        "status": "ok",
        "scanned": len(candidates),
        "approved": len(approved),
        "rejected": len(rejected),
        "held": len(held),
        "auto_threshold": AUTO_CONFIDENCE_THRESHOLD,
        "dry_run": not apply_changes,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "details": {
            "approved": approved[:10],
            "rejected": rejected[:10],
            "held": held[:10],
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Actually update statuses (default dry-run)")
    p.add_argument("--limit", type=int, default=MAX_PROPOSALS_PER_RUN)
    args = p.parse_args()
    print(json.dumps(run(apply_changes=args.apply, limit=args.limit), indent=2, ensure_ascii=False))
