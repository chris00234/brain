"""brain_core/predictive.py — context-aware predictive prefetch (biological:
cerebellum + frontal cortex anticipation).

2026-04-17: Chris Priority #2 (self-thinking for coding ability).

Complements existing boot_context._predictive_queries (temporal/calendar).
That one asks "what time is it, what's on calendar?" — this one asks "what is
Chris CURRENTLY focused on, and what past knowledge matches?"

Inputs (all live, read-only):
  - focus_items (autonomy.db) where category IN ('focus', 'session_summary')
    and not expired. These accumulate as Chris types prompts — the latest 2-3
    are the strongest signal of session state.
  - Recent claude_code_session raw_events (last 2h).

Process:
  1. Build query text = concat of last N focus contents + recent session
     signal hashtags.
  2. Call search_unified.search_all(query) — gets normal RRF-fused candidates.
  3. Re-score with:
       priority = base_score x (1 + valence_boost) x novelty_factor x domain_match
     where domain_match favors atoms whose category aligns with the session
     (e.g. session mentions "coding" → canonical:decisions + procedures win).
  4. Return top N (default 3) with a reason string tagging why each was picked.

Output: list of {id, content, score, priority, reason}.

Integration:
  - /brain/predictive GET endpoint
  - boot_context section "Predictive Context" (surfaces top 3 per session boot)

Safety:
  - Read-only; no new tables, no schema changes
  - Hot-path budget: <400ms (reuses existing retrieval, one SQL read, one
    valence batch SQL)
  - Fails open — empty list on any exception
"""

from __future__ import annotations

import logging
import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.predictive")

try:
    from config import AUTONOMY_DB, BRAIN_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

# How many recent focus items feed the query. 3 = last 3 Chris prompts + focus.
MAX_FOCUS_ITEMS = 3
# Novelty decay: atoms shown > N times get halved weight (matches attention.py).
NOVELTY_HALF_LIFE = 3
# Max prefix chars per focus content when concatenating.
MAX_FOCUS_CHARS = 300


from db import now_iso as _now_iso  # noqa: E402  — single-source UTC stamp helper


def _recent_focus_signal() -> tuple[str, list[dict]]:
    """Return (concat_query, raw_focus_items) — the freshest Chris signal.

    Pulls `focus` items only (not `session_summary`). Including session
    summaries in the signal created a feedback loop: yesterday's summary
    would become today's predictive query, which would retrieve the same
    summary back out under "predictive". Focus items are the ground-truth
    signal of what Chris is actively doing.
    """
    if not AUTONOMY_DB.exists():
        return "", []
    try:
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            now_iso = _now_iso()
            rows = conn.execute(
                "SELECT id, content, category, created_at, expires_at "
                "FROM focus_items "
                "WHERE category = 'focus' "
                "  AND (expires_at IS NULL OR expires_at >= ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (now_iso, MAX_FOCUS_ITEMS * 2),  # pull 2x so we can filter noise
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("focus read failed: %s", exc)
        return "", []

    items = []
    for r in rows:
        content = (r[1] or "").strip()
        # Filter shell-stdout noise and trivial /clear commands
        if len(content) < 15:
            continue
        if content.startswith("<bash-stdout>") or content.startswith("<command-name>"):
            continue
        items.append(
            {
                "id": r[0],
                "content": content[:MAX_FOCUS_CHARS],
                "category": r[2],
                "created_at": r[3],
            }
        )
        if len(items) >= MAX_FOCUS_ITEMS:
            break
    query = " ".join(it["content"] for it in items)[:800]
    return query, items


def _get_session_shown_counts() -> dict[str, int]:
    """Pull shown_count per atom_id from attention_queue for novelty scoring.
    Empty dict on failure."""
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            rows = conn.execute("SELECT id, shown_count FROM attention_queue").fetchall()
            return {r[0]: int(r[1] or 0) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def _domain_match(candidate: dict, focus_items: list[dict]) -> float:
    """Tiny bonus when the candidate category/source aligns with the session
    focus. Prefers canonical/procedures/lessons over raw session dumps when
    the session looks task-oriented. Returns multiplier in [1.0, 1.1]."""
    cat = str(candidate.get("metadata", {}).get("category") or "").lower()
    src = str(candidate.get("source") or "").lower()
    focus_text = " ".join(it["content"] for it in focus_items).lower()
    if "error" in focus_text or "bug" in focus_text or "failed" in focus_text:
        # Session is diagnosing — lessons + corrections are most useful
        if cat in ("lesson", "correction", "reflection"):
            return 1.10
        if "canonical" in src or cat == "canonical":
            return 1.05
    if ("deploy" in focus_text or "infra" in focus_text or "docker" in focus_text) and (
        cat in ("decision", "entity") or "infra" in src
    ):
        return 1.08
    if ("코딩" in focus_text or "coding" in focus_text or "refactor" in focus_text) and cat in (
        "procedure",
        "heuristic",
        "preference",
    ):
        return 1.07
    return 1.0


def predict_relevant_context(limit: int = 3) -> list[dict]:
    """Main entry — returns top candidates predicted relevant to current session.

    Each candidate: {id, content, base_score, priority, reason, source_category}
    """
    query, focus_items = _recent_focus_signal()
    if not query or len(query) < 20:
        return []

    # Retrieve candidates via existing brain pipeline
    try:
        import search_unified

        resp = search_unified.search_all(
            query,
            limit=max(10, limit * 4),
            sources=["rag", "canonical"],
            original_query=query,
        )
    except Exception as exc:
        log.debug("search failed: %s", exc)
        return []
    candidates = resp.get("results", []) if isinstance(resp, dict) else []
    if not candidates:
        return []

    # Batch-fetch valence for all candidates
    atom_ids = [c.get("id") for c in candidates if c.get("id")]
    try:
        from valence import get_valence_batch, valence_to_boost

        valence_map = get_valence_batch(atom_ids) if atom_ids else {}
    except Exception:
        valence_map = {}
        valence_to_boost = lambda v: 0.0  # noqa: E731

    shown_counts = _get_session_shown_counts()
    now_ts = datetime.now(UTC)

    # Re-score
    scored = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or ""
        base = float(c.get("score", 0) or 0)
        if base <= 0:
            continue
        meta = c.get("metadata", {}) if isinstance(c.get("metadata"), dict) else {}
        cat_lower = str(meta.get("category") or "").lower()
        src_lower = str(c.get("source") or "").lower()
        # Exclude stale session summaries and raw event dumps — they're what
        # the predictor was surfacing as "predictive" even though they carry
        # no durable signal.
        if cat_lower == "session_summary" or "session_summary" in src_lower:
            continue
        v = valence_map.get(cid, 0.0)
        v_boost = valence_to_boost(v)
        shown = shown_counts.get(cid, 0)
        novelty = 1.0 / (1.0 + shown / NOVELTY_HALF_LIFE)
        domain = _domain_match(c, focus_items)
        # Temporal decay — atoms are exponentially less "predictive" as they
        # age. Half-life 30 days. Prevents 2026-01 session fragments from
        # outscoring last week's decisions on pure cosine.
        created_at = meta.get("created_at") or c.get("created_at") or ""
        age_decay = 1.0
        if created_at:
            try:
                c_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                if c_dt.tzinfo is None:
                    c_dt = c_dt.replace(tzinfo=UTC)
                age_days = max(0.0, (now_ts - c_dt).total_seconds() / 86400.0)
                age_decay = math.exp(-age_days / 30.0)
            except (ValueError, TypeError):
                age_decay = 1.0
        priority = base * (1.0 + v_boost) * novelty * domain * age_decay

        reasons = []
        if v > 0.1:
            reasons.append(f"+valence({v:.2f})")
        if v < -0.1:
            reasons.append(f"-valence({v:.2f})")
        if shown == 0:
            reasons.append("novel")
        elif shown >= NOVELTY_HALF_LIFE:
            reasons.append(f"habituated({shown})")
        if domain > 1.0:
            reasons.append(f"domainx{domain:.2f}")
        if age_decay < 0.5:
            reasons.append(f"aged({age_decay:.2f})")
        if not reasons:
            reasons.append("base-match")

        scored.append(
            {
                "id": cid,
                "content": (c.get("content") or c.get("document") or "")[:300],
                "title": (c.get("title") or "")[:120],
                "source_category": str(c.get("metadata", {}).get("category") or "unknown"),
                "base_score": round(base, 2),
                "priority": round(priority, 3),
                "valence": round(v, 4),
                "shown_count": shown,
                "reason": ", ".join(reasons),
            }
        )

    scored.sort(key=lambda x: x["priority"], reverse=True)
    # Filter self-referential matches (focus_items mirror documents) + dedup
    seen_keys = set()
    deduped = []
    _noise_titles = ("manual focus items", "focus_items", "working memory")
    _noise_content = ("<command-name>", "<bash-stdout>")
    for s in scored:
        title_lo = s["title"].lower()
        content_lo = s["content"].lower()
        if any(n in title_lo for n in _noise_titles):
            continue
        if any(n in content_lo[:80] for n in _noise_content):
            continue
        key = (s["title"][:60], s["source_category"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(s)
        if len(deduped) >= limit:
            break
    return deduped


def debug_signal() -> dict:
    """For /brain/predictive/debug — shows the exact signal being used."""
    query, focus_items = _recent_focus_signal()
    return {
        "query_text": query,
        "query_len": len(query),
        "focus_items": [
            {"id": it["id"], "category": it["category"], "content": it["content"][:150]} for it in focus_items
        ],
    }


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run")
    p_run.add_argument("--limit", type=int, default=3)
    sub.add_parser("debug")
    args = p.parse_args()
    if args.cmd == "run":
        print(json.dumps(predict_relevant_context(limit=args.limit), indent=2, ensure_ascii=False))
    elif args.cmd == "debug":
        print(json.dumps(debug_signal(), indent=2, ensure_ascii=False))
    else:
        p.print_help()
