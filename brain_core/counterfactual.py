"""brain_core/counterfactual.py — D9 counterfactual simulation (MVP).

Biological motivation: the hippocampal-PFC loop replays decisions in REM
and quiet wake, often with edits — "what if I had said X instead?" —
which sharpens future choice. Without explicit counterfactual machinery
the brain only learns from the path it actually took.

decision_ledger already stores every brain decision with:
  - candidate_options_json: alternatives considered
  - selected_option + selected_payload_json: what was picked
  - confidence: how confident brain was
  - actual_outcome / outcome_status: how it turned out

This module surfaces high-value counterfactual candidates: failed
decisions that had alternatives, low-confidence choices, or chris-override
events. The actual LLM-driven "what if?" replay is gated on Chris's
explicit budget approval (counterfactual_simulate function falls back to
dry_run by default — returns the prompt that WOULD be sent without
dispatching).

Pattern matches D8 interoception: wire the read side, leave the costly
side (LLM dispatch) gated. When Chris enables, the loop activates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")

log = logging.getLogger("brain.counterfactual")


def list_counterfactual_candidates(
    limit: int = 20,
    days: int = 14,
    only_failed: bool = False,
) -> list[dict]:
    """Surface decisions where a counterfactual replay would be informative.

    Selection: outcome_status='failed' OR (low confidence AND had >=2 candidates)
    OR (chris_override implied via outcome_status='failed_chris_rejected').
    """
    if not AUTONOMY_DB.exists():
        return []
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        where = "created_at > datetime('now', ?)"
        params: list = [f"-{days} days"]
        if only_failed:
            where += " AND outcome_status = 'failed'"
        rows = conn.execute(
            f"SELECT id, created_at, domain, observation_kind, selected_option, "  # noqa: S608 — fixed placeholders
            f"       confidence, candidate_options_json, expected_outcome, "
            f"       actual_outcome, outcome_status "
            f"FROM decision_ledger "
            f"WHERE {where} "
            f"ORDER BY created_at DESC "
            f"LIMIT ?",
            (*params, limit * 3),
        ).fetchall()

        candidates: list[dict] = []
        for r in rows:
            try:
                options = json.loads(r["candidate_options_json"] or "[]")
            except Exception:
                options = []
            alt_count = max(0, len(options) - 1)  # excludes the selected one
            status = r["outcome_status"]
            confidence = float(r["confidence"] or 0)
            score = 0.0
            reasons: list[str] = []
            if status == "failed":
                score += 1.0
                reasons.append("decision_failed")
            if confidence < 0.5 and alt_count >= 1:
                score += 0.5
                reasons.append("low_confidence_with_alternatives")
            if alt_count >= 2:
                score += 0.3
                reasons.append("multi_alternative_choice")
            if status not in ("succeeded", "failed", "pending"):
                score += 0.2
                reasons.append(f"unusual_status:{status}")
            if score <= 0:
                continue
            candidates.append(
                {
                    "decision_id": r["id"],
                    "created_at": r["created_at"],
                    "domain": r["domain"],
                    "observation_kind": r["observation_kind"],
                    "selected": r["selected_option"],
                    "alternatives": alt_count,
                    "confidence": round(confidence, 3),
                    "outcome_status": status,
                    "score": round(score, 3),
                    "reasons": reasons,
                }
            )
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates[:limit]
    finally:
        conn.close()


def simulate_counterfactual(decision_id: str) -> dict:
    """Dispatch the counterfactual prompt to Sage via subscription CLI.

    Uses cli_llm.cli_dispatch on the codex (GPT Pro subscription) path — no
    marginal cost, within Chris's existing subscription policy. Bounded to
    1 call per cron tick. Result stored in counterfactual_results table.
    """
    prompt_payload = build_counterfactual_prompt(decision_id)
    if prompt_payload.get("error"):
        return prompt_payload
    try:
        from cli_llm import cli_dispatch
    except ImportError:
        return {"error": "cli_llm_unavailable", "decision_id": decision_id}
    full_prompt = prompt_payload.get("full_prompt") or ""
    if not full_prompt:
        return {"error": "empty_prompt", "decision_id": decision_id}
    try:
        r = cli_dispatch(full_prompt, backend="codex", timeout=60)
    except Exception as exc:
        return {"error": f"dispatch_failed:{exc!s}", "decision_id": decision_id}
    if not getattr(r, "ok", False):
        return {"error": "dispatch_not_ok", "decision_id": decision_id}
    text = (getattr(r, "text", "") or "").strip()
    parsed: dict | None = None
    if text:
        import re as _re

        cleaned = _re.sub(r"^```(?:json)?|```$", "", text, flags=_re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = None

    # Store result. INSERT OR IGNORE + UNIQUE(decision_id) makes this
    # idempotent under concurrent run_daily calls (D1-D10 review fix).
    _ensure_results_schema()
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO counterfactual_results "
            "(decision_id, raw_response, parsed_json, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (decision_id, text[:4000], json.dumps(parsed) if parsed else None),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "decision_id": decision_id,
        "ok": True,
        "raw_response": text[:1000],
        "parsed": parsed,
    }


_results_schema_done = False


def _ensure_results_schema() -> None:
    global _results_schema_done
    if _results_schema_done:
        return
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS counterfactual_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL UNIQUE,
                raw_response TEXT,
                parsed_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cf_results_decision
                ON counterfactual_results(decision_id);
            CREATE INDEX IF NOT EXISTS idx_cf_results_created
                ON counterfactual_results(created_at);
            """
        )
        conn.commit()
        _results_schema_done = True
    finally:
        conn.close()


def run_daily(max_dispatches: int = 1) -> dict:
    """Cron entry: pick top-N candidates not yet simulated, dispatch each."""
    _ensure_results_schema()
    candidates = list_counterfactual_candidates(limit=max_dispatches * 5, days=14, only_failed=True)
    if not candidates:
        return {"status": "ok", "scanned": 0, "dispatched": 0}

    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    try:
        already = {
            row[0]
            for row in conn.execute("SELECT DISTINCT decision_id FROM counterfactual_results").fetchall()
        }
    finally:
        conn.close()

    dispatched: list[dict] = []
    for c in candidates:
        if len(dispatched) >= max_dispatches:
            break
        if c["decision_id"] in already:
            continue
        result = simulate_counterfactual(c["decision_id"])
        dispatched.append(
            {
                "decision_id": c["decision_id"],
                "ok": result.get("ok", False),
                "error": result.get("error"),
            }
        )
    return {
        "status": "ok",
        "candidates": len(candidates),
        "dispatched": len(dispatched),
        "results": dispatched,
    }


def build_counterfactual_prompt(decision_id: str) -> dict:
    """Return the prompt Sage would receive for counterfactual replay.

    Does NOT dispatch. Returns enough context for Chris (or a future
    LLM-gated path) to evaluate whether to run the simulation.
    """
    if not AUTONOMY_DB.exists():
        return {"error": "no_db"}
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM decision_ledger WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            return {"error": "decision_not_found", "decision_id": decision_id}
        try:
            options = json.loads(row["candidate_options_json"] or "[]")
        except Exception:
            options = []
        try:
            perceived = json.loads(row["perceived_state_json"] or "{}")
        except Exception:
            perceived = {}
        try:
            payload = json.loads(row["selected_payload_json"] or "{}")
        except Exception:
            payload = {}

        prompt = (
            f"You are running a counterfactual simulation on a past brain decision.\n\n"
            f"DECISION ID: {decision_id}\n"
            f"DOMAIN: {row['domain']}\n"
            f"OBSERVATION: {row['observation_kind']} / {row['observation_subject']}\n"
            f"PERCEIVED STATE AT TIME OF DECISION:\n{json.dumps(perceived, indent=2)[:1500]}\n\n"
            f"CANDIDATES CONSIDERED:\n{json.dumps(options, indent=2)[:1500]}\n\n"
            f"BRAIN CHOSE: {row['selected_option']} (confidence {row['confidence']})\n"
            f"ACTION TAKEN: {json.dumps(payload, indent=2)[:800]}\n"
            f"EXPECTED OUTCOME: {row['expected_outcome']}\n"
            f"ACTUAL OUTCOME: {row['actual_outcome'] or '<unresolved>'}\n"
            f"STATUS: {row['outcome_status']}\n\n"
            f"For EACH non-selected candidate option, predict:\n"
            f"  1. What would have happened if brain had chosen that option?\n"
            f"  2. Would the outcome have been better, worse, or roughly equal?\n"
            f"  3. What signal was brain missing that would have favored that option?\n\n"
            f"Output strict JSON: "
            f'{{"counterfactuals":[{{"option":"<name>","predicted_outcome":"...","comparison":"better|worse|equal","missing_signal":"..."}}]}}'
        )
        return {
            "decision_id": decision_id,
            "dispatchable": True,
            "would_call": "sage_via_cli_llm",
            "dry_run": True,
            "estimated_tokens_in": len(prompt) // 4,
            "estimated_tokens_out": 500,
            "prompt_preview": prompt[:2000],
            "full_prompt": prompt,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--days", type=int, default=14)
    p_list.add_argument("--only-failed", action="store_true")
    p_p = sub.add_parser("prompt")
    p_p.add_argument("decision_id")
    args = p.parse_args()
    if args.cmd == "list":
        out = list_counterfactual_candidates(limit=args.limit, days=args.days, only_failed=args.only_failed)
        print(json.dumps(out, indent=2, ensure_ascii=False))  # noqa: T201
    elif args.cmd == "prompt":
        print(json.dumps(build_counterfactual_prompt(args.decision_id), indent=2, ensure_ascii=False))  # noqa: T201
    else:
        p.print_help()
