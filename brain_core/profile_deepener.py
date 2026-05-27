"""brain_core/profile_deepener.py — daily candidate-profile distiller.

The Honcho-equivalent for brain: derives candidate preference/identity atoms
from operational signals (agent activity, belief snapshot, recent outcomes)
without bypassing brain's governance gates. Submissions go through the
standard ``POST /memory`` path so atoms inherit:

  - ingest_classifier kind/category tagging
  - cosine-similarity supersession gate
  - action_audit + provenance via ``source='profile_deepener:daily'``
  - normal atom_deboost / outcome_feedback consumption downstream

Contract: this module NEVER writes to canonical and NEVER asserts beliefs at
high confidence. Outputs are observations the brain pipeline may promote to
preference/fact atoms after corroboration. The journal at
``logs/profile_deepener_journal.jsonl`` records every run for audit.

Scheduling: daily 03:45 PT via ``profile_deepener_daily`` job in
``job_definitions.py``. Runs before the weekly ``profile_regen`` (Sunday 04:00)
so Sage sees fresh candidates when it recompiles Chris's canonical profile.

2026-05-20 W3.5: ships as a peer to Hermes Honcho dialectic user modeling.
Honcho writes its own canonical store; this writes through brain's governed
pipeline so paraphrases get supersession instead of accumulating duplicates.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.profile_deepener")

sys.path.insert(0, str(Path(__file__).resolve().parent))

BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = Path("~/.brain/credentials/.personal_webhook_secret").expanduser()

JOURNAL_PATH = Path("/Users/chrischo/server/brain/logs/profile_deepener_journal.jsonl")
MAX_CANDIDATES_PER_RUN = 5


def _load_secret() -> str:
    try:
        return SECRET_FILE.read_text().strip()
    except Exception as exc:
        log.warning("profile_deepener: secret read failed: %s", exc)
        return ""


def _post_memory(content: str, source: str) -> dict:
    """Submit a candidate atom through the governed /memory path."""
    secret = _load_secret()
    if not secret:
        return {"error": "no_secret"}
    body = json.dumps(
        {
            "content": content,
            "category": "other",
            "agent": "profile_deepener",
            "source": source,
        }
    ).encode()
    req = urllib.request.Request(f"{BRAIN_URL}/memory", data=body, method="POST")  # noqa: S310
    req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-agent", "profile_deepener")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": str(e)[:200]}
    except Exception as e:
        return {"error": str(e)[:200]}


def _gather_activity() -> dict:
    """Read agent activity report (last 7d). Failures are non-fatal."""
    try:
        import agent_activity_report

        return agent_activity_report.run() or {}
    except Exception as exc:
        log.debug("agent_activity_report failed: %s", exc)
        return {}


def _gather_beliefs() -> dict:
    """Read current belief state snapshot. Failures are non-fatal."""
    try:
        import belief_state

        return belief_state.build_belief_state() or {}
    except Exception as exc:
        log.debug("belief_state failed: %s", exc)
        return {}


def _gather_recent_outcomes(limit: int = 20) -> list[dict]:
    """Read recent decision-outcome aggregates. Failures are non-fatal."""
    try:
        from decision_ledger import decision_feedback_report

        report = decision_feedback_report() or {}
        # The report shape varies by version; grab whatever decision list it
        # exposes and return up to `limit` recent items for distillation.
        decisions = report.get("decisions") or report.get("recent") or []
        return list(decisions)[:limit]
    except Exception as exc:
        log.debug("decision_feedback_report failed: %s", exc)
        return []


def _compose_candidates(activity: dict, beliefs: dict, outcomes: list[dict]) -> list[str]:
    """Template-based candidate text generation. No LLM in the hot path.

    Returns up to MAX_CANDIDATES_PER_RUN short observational sentences. The
    claim text is DATE-FREE on purpose: profile_hypotheses.record_observation
    hashes the normalized claim to derive ``hyp_id``, so a leading
    ``YYYY-MM-DD`` would mint a fresh hypothesis every day and support could
    never accrete past 1 (codex round-3 defect 1). The day-of-observation
    lives in the support_json's ``at`` timestamp instead.

    2026-05-20 W3.5 round 3 fix: also reads ``activity['agents']`` — the
    actual key emitted by agent_activity_report.run() — rather than the
    older ``per_agent`` placeholder that the v1 deepener guessed at.
    """
    candidates: list[str] = []

    # 1. Top-active agents (operational fact). Stable claim text — names are
    # ordered by stores_7d so a single agent's burst doesn't reorder it day
    # to day.
    agents = activity.get("agents") if isinstance(activity, dict) else None
    if isinstance(agents, list) and agents:
        ranked = [a for a in agents if isinstance(a, dict) and int(a.get("stores_7d", 0) or 0) > 0][:3]
        names = [a.get("agent", "?") for a in ranked]
        if names:
            candidates.append(f"top brain writers (7d): {', '.join(names)}")

    # 2. Belief-state pulse — high-confidence themes that surfaced today.
    # belief_state._belief_from_row emits ``{text: ...}`` (see
    # brain_core/belief_state.py:222). Older deepener versions guessed at
    # ``content``/``subject``/``topic``, which always resolved to "" and
    # produced blank claim text (codex round-4 defect A).
    high_conf = beliefs.get("beliefs") if isinstance(beliefs, dict) else None
    if isinstance(high_conf, list) and high_conf:
        themes = [
            (b.get("text") or b.get("content") or b.get("subject") or "")[:120]
            for b in high_conf[:2]
            if isinstance(b, dict)
        ]
        themes = [t for t in themes if t]
        if themes:
            candidates.append(f"top beliefs in current snapshot: {' | '.join(themes)}")

    # 3. Uncertainty pulse — what brain is currently doubting. Same shape
    # as beliefs: ``text`` is the canonical field, fall back to others for
    # forward-compat across belief_state versions.
    uncertainties = beliefs.get("uncertainties") if isinstance(beliefs, dict) else None
    if isinstance(uncertainties, list) and uncertainties:
        u_summary = [
            (u.get("text") or u.get("topic") or u.get("subject") or u.get("content") or "")[:80]
            for u in uncertainties[:2]
            if isinstance(u, dict)
        ]
        u_summary = [s for s in u_summary if s]
        if u_summary:
            candidates.append(f"open uncertainties: {' | '.join(u_summary)}")

    # 4. Recent decision outcome aggregate. The claim itself is the qualitative
    # bucket (mostly_succeeded / mixed / mostly_failed) instead of raw counts,
    # so day-over-day stability is the norm — only structural shifts mint a
    # new hypothesis.
    if outcomes:
        succeeded = sum(
            1
            for d in outcomes
            if isinstance(d, dict)
            and (str(d.get("outcome_status") or d.get("status") or "").lower() == "succeeded")
        )
        total = len(outcomes)
        if total > 0:
            ratio = succeeded / total
            if ratio >= 0.75:
                bucket = "mostly_succeeded"
            elif ratio >= 0.4:
                bucket = "mixed_outcomes"
            else:
                bucket = "mostly_failed"
            candidates.append(f"recent decision outcomes trend: {bucket}")

    return candidates[:MAX_CANDIDATES_PER_RUN]


def _journal_write(entry: dict) -> None:
    try:
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_PATH.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("profile_deepener journal write failed: %s", exc)


def run(dry_run: bool = False) -> dict:
    """One profile-deepening pass.

    Two-phase pipeline (2026-05-20 W3.5 round 3, codex gap 2):
      1. Record each candidate observation as a hypothesis via
         profile_hypotheses.record_observation — fuzzy-matched + support
         accumulating across runs. The same paraphrased claim accretes
         support across days instead of spamming /memory with duplicates.
      2. Check for promotable hypotheses (status=supported, support>=3,
         confidence>=0.75) and canonicalize them through /memory. The
         hypothesis row is then linked to the durable atom id.

    ``dry_run=True`` records hypotheses + identifies promotables but does
    NOT canonicalize. Used for offline preview + tests.
    """
    started = time.time()
    activity = _gather_activity()
    beliefs = _gather_beliefs()
    outcomes = _gather_recent_outcomes()
    candidates = _compose_candidates(activity, beliefs, outcomes)

    import profile_hypotheses

    # Phase 1: record candidates as hypotheses (dialectic accrual). dry_run
    # short-circuits the DB write — codex round-3 flagged the earlier impl
    # for being misleading: dry_run only skipped /memory promotion but still
    # mutated profile_hypotheses.db, so "dry" runs left durable state behind.
    hypothesis_results: list[dict] = []
    for text in candidates:
        evidence = {
            "summary": text,
            "signal": {
                "activity_seen": bool(activity),
                "beliefs_seen": bool(beliefs),
                "outcomes_count": len(outcomes),
            },
        }
        if dry_run:
            hypothesis_results.append({"claim": text[:200], "status": "would_record", "dry_run": True})
            continue
        try:
            res = profile_hypotheses.record_observation(text, evidence, actor="profile_deepener")
        except Exception as exc:
            res = {"error": str(exc)[:200]}
        hypothesis_results.append({"claim": text[:200], **res})

    # Phase 2: canonicalize promotable hypotheses
    promotions: list[dict] = []
    if not dry_run:
        try:
            promotable = profile_hypotheses.find_promotable()
        except Exception as exc:
            log.warning("profile_hypotheses.find_promotable failed: %s", exc)
            promotable = []
        for hyp in promotable:
            mem_result = _post_memory(
                hyp.get("claim") or "",
                source=f"profile_hypotheses:canonicalize:{hyp.get('id')}",
            )
            ok = "error" not in mem_result
            atom_id = mem_result.get("id") if ok else ""
            if ok and atom_id:
                try:
                    profile_hypotheses.mark_canonicalized(hyp.get("id") or "", atom_id)
                except Exception as exc:
                    log.warning("mark_canonicalized failed: %s", exc)
            promotions.append(
                {
                    "hypothesis_id": hyp.get("id"),
                    "claim": (hyp.get("claim") or "")[:120],
                    "support_count": hyp.get("support_count"),
                    "confidence": hyp.get("confidence"),
                    "ok": ok,
                    "atom_id": atom_id,
                }
            )

    try:
        hyp_summary = profile_hypotheses.summary()
    except Exception as exc:
        log.warning("profile_hypotheses.summary failed: %s", exc)
        hyp_summary = {}

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "duration_ms": int((time.time() - started) * 1000),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "hypotheses": hypothesis_results,
        "promotions": promotions,
        "hypothesis_summary": hyp_summary,
        "dry_run": bool(dry_run),
        "activity_seen": bool(activity),
        "beliefs_seen": bool(beliefs),
        "outcomes_seen": len(outcomes),
    }
    _journal_write(summary)
    return summary


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the daily profile deepener.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Compose candidates without submitting to /memory"
    )
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))  # noqa: T201 — CLI entry point
    return 0


if __name__ == "__main__":
    sys.exit(main())
