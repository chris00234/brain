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
SECRET_FILE = Path("~/.openclaw/credentials/.personal_webhook_secret").expanduser()

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
    brain's ingest_classifier decides kind/category; we just emit signal-rich
    one-liners that capture the past 24h pattern.
    """
    candidates: list[str] = []
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    # 1. Top-active agents (operational fact)
    per_agent = activity.get("per_agent") if isinstance(activity, dict) else None
    if isinstance(per_agent, dict) and per_agent:
        ranked = sorted(
            per_agent.items(),
            key=lambda kv: int((kv[1] or {}).get("stores", 0) or 0),
            reverse=True,
        )[:3]
        names = [
            f"{name}({int((stats or {}).get('stores', 0))})"
            for name, stats in ranked
            if int((stats or {}).get("stores", 0) or 0) > 0
        ]
        if names:
            candidates.append(f"profile_deepener {today}: top brain writers (last 7d) — {', '.join(names)}")

    # 2. Belief-state pulse — high-confidence themes that surfaced today
    high_conf = beliefs.get("beliefs") if isinstance(beliefs, dict) else None
    if isinstance(high_conf, list) and high_conf:
        themes = [
            (b.get("content") or b.get("subject") or "")[:120] for b in high_conf[:2] if isinstance(b, dict)
        ]
        themes = [t for t in themes if t]
        if themes:
            candidates.append(
                f"profile_deepener {today}: top beliefs in current snapshot — {' | '.join(themes)}"
            )

    # 3. Uncertainty pulse — what brain is currently doubting
    uncertainties = beliefs.get("uncertainties") if isinstance(beliefs, dict) else None
    if isinstance(uncertainties, list) and uncertainties:
        u_summary = [
            (u.get("topic") or u.get("subject") or u.get("content") or "")[:80]
            for u in uncertainties[:2]
            if isinstance(u, dict)
        ]
        u_summary = [s for s in u_summary if s]
        if u_summary:
            candidates.append(f"profile_deepener {today}: open uncertainties — {' | '.join(u_summary)}")

    # 4. Recent decision outcome aggregate
    if outcomes:
        succeeded = sum(
            1
            for d in outcomes
            if isinstance(d, dict)
            and (str(d.get("outcome_status") or d.get("status") or "").lower() == "succeeded")
        )
        total = len(outcomes)
        if total > 0:
            candidates.append(
                f"profile_deepener {today}: decision outcome window — {succeeded}/{total} succeeded"
            )

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

    ``dry_run=True`` composes candidates and writes the journal entry but
    does NOT submit atoms to /memory — used for offline preview + tests.
    """
    started = time.time()
    activity = _gather_activity()
    beliefs = _gather_beliefs()
    outcomes = _gather_recent_outcomes()
    candidates = _compose_candidates(activity, beliefs, outcomes)

    submissions: list[dict] = []
    if not dry_run:
        for text in candidates:
            result = _post_memory(text, source="profile_deepener:daily")
            submissions.append(
                {
                    "content": text,
                    "ok": "error" not in result,
                    "result": result if "error" in result else {"id": result.get("id")},
                }
            )

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "duration_ms": int((time.time() - started) * 1000),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "submissions": submissions,
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
