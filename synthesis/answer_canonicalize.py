#!/opt/homebrew/bin/python3
"""Nightly promoter: query→canonical wiki loop.

Reads pending rows from brain.db/answer_candidates, scores each, and for
the top N (max 3/night) refactors the answer into a raw/inbox/ record
that flows through the existing distill → propose → score → canonical
pipeline (canonical_pipeline job, 02:00 daily).

Inspired by Karpathy's llm-wiki gist — "valuable queries produce new
pages." The brain distills from raw events; this adds the complementary
path where synthesized answers themselves become candidates for the
compounding wiki.

Guardrails:
- Max N=3 promotions per run (bounds LLM cost + canonical pollution risk)
- Novel-synthesis gate: reject if answer has >60% token overlap with any
  single recent canonical note (we don't want re-packaged existing content)
- Min answer length 200 chars
- Recency: candidates older than 14d are auto-skipped (stale context)

Usage:
  answer_canonicalize.py [--limit 3] [--dry-run] [--min-score 0.5]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import answer_candidates as ac
from common import ROOT, iter_note_paths, parse_note, tokenize

INBOX_DIR = ROOT / "raw" / "inbox"
CANONICAL_DIR = ROOT / "canonical"
LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

MAX_AGE_DAYS = 14
MIN_ANSWER_CHARS = 200
DEFAULT_LIMIT = 3
DEFAULT_MIN_SCORE = 0.45
MAX_OVERLAP = 0.60


def _age_days(iso: str) -> int:
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return int((datetime.now(UTC) - ts).days)
    except Exception:
        return 999


def _load_canonical_tokens() -> list[tuple[str, set[str]]]:
    """Return list of (note_id, token_set) for overlap detection."""
    out = []
    for path in iter_note_paths(CANONICAL_DIR):
        try:
            meta, body = parse_note(path)
        except Exception:
            continue
        if meta.get("type") != "canonical" or meta.get("status") != "active":
            continue
        tokens = set(tokenize((meta.get("title", "") + " " + body)[:3000]))
        if len(tokens) >= 10:
            out.append((meta.get("id", path.stem), tokens))
    return out


def _max_overlap(answer: str, canonical_tokens: list[tuple[str, set[str]]]) -> tuple[float, str]:
    answer_tokens = set(tokenize(answer[:3000]))
    if len(answer_tokens) < 10:
        return 0.0, ""
    best = 0.0
    best_id = ""
    for note_id, ctokens in canonical_tokens:
        union = answer_tokens | ctokens
        if not union:
            continue
        overlap = len(answer_tokens & ctokens) / len(answer_tokens)
        if overlap > best:
            best = overlap
            best_id = note_id
    return best, best_id


def _score(candidate: dict, overlap: float) -> float:
    """Heuristic score in [0, 1]. Higher = better canonical candidate."""
    s = 0.0
    # Length signal (longer answers more likely to be synthesis)
    answer_len = len(candidate.get("answer") or "")
    s += min(answer_len / 2000, 0.30)
    # Source route signal — /chris/think is first-person decisions
    if candidate.get("source_route") == "/chris/think":
        s += 0.25
    # Novelty — the less overlap with existing canonical, the higher the score
    s += max(0.0, (1.0 - overlap / MAX_OVERLAP)) * 0.30
    # Reason field indicates manual marking, bump score
    if candidate.get("reason"):
        s += 0.15
    return round(min(s, 1.0), 3)


def _slug(text: str) -> str:
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    return h


def _promote_to_inbox(candidate: dict, score: float) -> Path:
    """Write a schema-compliant raw/inbox/ record for canonical_pipeline to pick up."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slug(candidate["query"] + candidate["answer"])
    now = datetime.now(UTC).isoformat(timespec="seconds")
    query = candidate["query"].strip()
    answer = candidate["answer"].strip()
    reason = (candidate.get("reason") or "").strip()

    # Pack everything into content since raw.schema.json has additionalProperties: false
    content_parts = [
        f"# {query[:200]}",
        "",
        "## Query",
        query,
        "",
        "## Synthesized Answer",
        answer,
    ]
    if reason:
        content_parts.extend(["", "## Promotion reason", reason])
    content_parts.extend(
        [
            "",
            "## Provenance",
            f"- source_route: {candidate.get('source_route', 'unknown')}",
            f"- agent: {candidate.get('agent') or 'unknown'}",
            f"- candidate_id: {candidate['id']}",
            f"- score: {score}",
        ]
    )
    content = "\n".join(content_parts)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    record = {
        "id": f"raw_query_synthesis_{slug}",
        "timestamp": now,
        "source_type": "query_synthesis",
        "source_ref": f"answer_candidates:{candidate['id']}",
        "actor": candidate.get("agent") or "system",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content,
        "attachments": [],
        "entities": [],
        "hash": f"sha256:{content_hash}",
    }
    out_path = INBOX_DIR / f"query_synthesis_{slug}.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return out_path


def _log(record: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "answer_canonicalize.jsonl"
    with log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pending = ac.list_pending(limit=100)
    print(f"[answer_canonicalize] {len(pending)} pending candidates")

    if not pending:
        print(json.dumps({"status": "ok", "promoted": 0, "pending": 0}))
        return 0

    canonical_tokens = _load_canonical_tokens()
    print(f"  loaded {len(canonical_tokens)} canonical notes for overlap check")

    scored: list[tuple[dict, float, float, str]] = []
    pre_skips: list[tuple[int, str, float | None]] = []
    for c in pending:
        age = _age_days(c["created_at"])
        if age > MAX_AGE_DAYS:
            pre_skips.append((c["id"], f"stale ({age}d old)", None))
            continue
        answer = c.get("answer") or ""
        if len(answer) < MIN_ANSWER_CHARS:
            pre_skips.append((c["id"], "answer too short", 0.0))
            continue
        overlap, overlap_id = _max_overlap(answer, canonical_tokens)
        if overlap >= MAX_OVERLAP:
            pre_skips.append((c["id"], f"overlap {overlap:.2f} with {overlap_id}", 0.0))
            continue
        score = _score(c, overlap)
        scored.append((c, score, overlap, overlap_id))

    if not args.dry_run:
        for cid, reason, score_val in pre_skips:
            if "stale" in reason:
                ac.mark_skipped(cid, reason)
            else:
                ac.mark_rejected(cid, reason, score=score_val)

    scored.sort(key=lambda t: -t[1])
    to_promote = [t for t in scored if t[1] >= args.min_score][: args.limit]

    print(f"  {len(scored)} scored, {len(to_promote)} above min_score {args.min_score}")

    promoted = []
    for c, score, overlap, overlap_id in to_promote:
        if args.dry_run:
            print(f"  [dry-run] would promote id={c['id']} score={score} overlap={overlap:.2f}")
            continue
        path = _promote_to_inbox(c, score)
        ac.mark_promoted(c["id"], str(path.relative_to(ROOT)), score)
        promoted.append(
            {
                "id": c["id"],
                "source_route": c.get("source_route"),
                "score": score,
                "overlap": round(overlap, 3),
                "overlap_with": overlap_id,
                "path": str(path.relative_to(ROOT)),
            }
        )
        _log(
            {
                "at": datetime.now(UTC).isoformat(),
                "candidate_id": c["id"],
                "score": score,
                "overlap": overlap,
                "path": str(path),
            }
        )

    if not args.dry_run:
        promoted_ids = {p["id"] for p in promoted}
        for c, score, overlap, _ in scored:
            if score < args.min_score and c["id"] not in promoted_ids:
                ac.mark_rejected(c["id"], f"score {score} below min {args.min_score}", score=score)

    out = {
        "status": "ok",
        "pending": len(pending),
        "scored": len(scored),
        "promoted": len(promoted),
        "items": promoted,
        "stats": ac.stats(),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
