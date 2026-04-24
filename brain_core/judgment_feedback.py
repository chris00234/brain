"""Persistence and reports for active-recall judgment decisions.

The hot-path judgment layer is deterministic and cheap. This module records its
decisions beside action_audit so later labelers can learn whether prompt-level
gating was useful or noisy without adding another daemon or LLM call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BRAIN_DB

log = logging.getLogger("brain.judgment_feedback")

NEGATIVE_OUTCOMES = frozenset({"restated", "judged_wrong", "wrong", "incorrect", "chris_override"})
MIN_TUNING_SAMPLES = 20
MAX_BLOCKS_CAP = 6
MIN_BLOCKS_CAP = 1
MAX_TOKENS_CAP = 1800
MIN_TOKENS_CAP = 600
MAX_SCORE_CAP = 0.9
MIN_SCORE_CAP = 0.65


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS active_recall_judgments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_audit_id INTEGER,
            session_id TEXT,
            actor TEXT NOT NULL DEFAULT 'unknown',
            prompt_intent TEXT NOT NULL,
            needs_memory INTEGER NOT NULL,
            allow_semantic INTEGER NOT NULL,
            allow_proactive INTEGER NOT NULL,
            max_blocks INTEGER NOT NULL,
            max_tokens INTEGER NOT NULL,
            min_semantic_score REAL NOT NULL,
            block_count INTEGER NOT NULL,
            semantic_count INTEGER NOT NULL,
            suppressed_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arj_audit
          ON active_recall_judgments(action_audit_id);
        CREATE INDEX IF NOT EXISTS idx_arj_intent_ts
          ON active_recall_judgments(prompt_intent, created_at);
        CREATE INDEX IF NOT EXISTS idx_arj_actor_ts
          ON active_recall_judgments(actor, created_at);
        """
    )
    conn.commit()


def record(
    *,
    action_audit_id: int | None,
    session_id: str | None,
    actor: str | None,
    judgment: object | None,
    arbitration: object | None,
    block_count: int,
    semantic_count: int,
    latency_ms: int,
    db_path: Path | None = None,
) -> None:
    """Best-effort write of one active-recall judgment decision."""

    if judgment is None or not hasattr(judgment, "to_dict"):
        return
    data = judgment.to_dict()
    suppressed: dict = {}
    if arbitration is not None and hasattr(arbitration, "to_quality_dict"):
        suppressed = arbitration.to_quality_dict().get("suppressed") or {}

    try:
        conn = sqlite3.connect(str(db_path or BRAIN_DB), timeout=5)
    except Exception:
        return
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO active_recall_judgments "
            "(action_audit_id, session_id, actor, prompt_intent, needs_memory, "
            " allow_semantic, allow_proactive, max_blocks, max_tokens, "
            " min_semantic_score, block_count, semantic_count, suppressed_json, "
            " latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_audit_id,
                session_id,
                actor or "unknown",
                str(data.get("intent") or "unknown"),
                1 if data.get("needs_memory") else 0,
                1 if data.get("allow_semantic") else 0,
                1 if data.get("allow_proactive") else 0,
                int(data.get("max_blocks") or 0),
                int(data.get("max_tokens") or 0),
                float(data.get("min_semantic_score") or 0.0),
                int(block_count),
                int(semantic_count),
                json.dumps(suppressed, sort_keys=True),
                int(latency_ms),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.debug("active_recall judgment record failed: %s", exc)
    finally:
        conn.close()


def report(hours: int = 24, db_path: Path | None = None) -> dict:
    """Return lightweight judgment-gate telemetry for the trailing window."""

    conn = sqlite3.connect(str(db_path or BRAIN_DB))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT prompt_intent,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN needs_memory = 0 THEN 1 ELSE 0 END) AS suppressed_prompts,
                   AVG(block_count) AS avg_blocks,
                   AVG(semantic_count) AS avg_semantic_blocks,
                   AVG(latency_ms) AS avg_latency_ms
            FROM active_recall_judgments
            WHERE created_at > datetime('now', ? || ' hours')
            GROUP BY prompt_intent
            ORDER BY calls DESC
            """,
            (f"-{int(hours)}",),
        ).fetchall()
        suppressed_rows = conn.execute(
            """
            SELECT suppressed_json
            FROM active_recall_judgments
            WHERE created_at > datetime('now', ? || ' hours')
            """,
            (f"-{int(hours)}",),
        ).fetchall()
    finally:
        conn.close()

    suppressed: dict[str, int] = {}
    for row in suppressed_rows:
        try:
            parsed = json.loads(row["suppressed_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key, value in parsed.items():
            try:
                suppressed[str(key)] = suppressed.get(str(key), 0) + int(value)
            except (TypeError, ValueError):
                continue

    by_intent = [
        {
            "intent": row["prompt_intent"],
            "calls": int(row["calls"] or 0),
            "suppressed_prompts": int(row["suppressed_prompts"] or 0),
            "avg_blocks": round(float(row["avg_blocks"] or 0.0), 2),
            "avg_semantic_blocks": round(float(row["avg_semantic_blocks"] or 0.0), 2),
            "avg_latency_ms": round(float(row["avg_latency_ms"] or 0.0), 1),
        }
        for row in rows
    ]
    return {"window_hours": hours, "by_intent": by_intent, "suppressed": dict(sorted(suppressed.items()))}


def tuning_report(
    hours: int = 24,
    *,
    min_samples: int = MIN_TUNING_SAMPLES,
    db_path: Path | None = None,
) -> dict:
    """Recommend deterministic policy adjustments from judgment + outcome data.

    This does not apply changes. It turns accumulated active_recall_judgments
    rows into reviewable recommendations so threshold/budget updates remain
    evidence-based and reversible.
    """

    rows = _load_tuning_rows(hours=hours, db_path=db_path)
    by_intent: dict[str, dict] = {}
    for row in rows:
        intent = row["prompt_intent"]
        rec = by_intent.setdefault(
            intent,
            {
                "intent": intent,
                "calls": 0,
                "needs_memory_calls": 0,
                "suppressed_prompts": 0,
                "negative_outcomes": 0,
                "outcome_labeled": 0,
                "block_sum": 0,
                "semantic_sum": 0,
                "latency_sum": 0,
                "max_blocks_sum": 0,
                "max_tokens_sum": 0,
                "min_score_sum": 0.0,
                "suppressed": {},
            },
        )
        rec["calls"] += 1
        if row["needs_memory"]:
            rec["needs_memory_calls"] += 1
        else:
            rec["suppressed_prompts"] += 1
        outcome = row["outcome"]
        if outcome:
            rec["outcome_labeled"] += 1
            if str(outcome) in NEGATIVE_OUTCOMES:
                rec["negative_outcomes"] += 1
        rec["block_sum"] += int(row["block_count"] or 0)
        rec["semantic_sum"] += int(row["semantic_count"] or 0)
        rec["latency_sum"] += int(row["latency_ms"] or 0)
        rec["max_blocks_sum"] += int(row["max_blocks"] or 0)
        rec["max_tokens_sum"] += int(row["max_tokens"] or 0)
        rec["min_score_sum"] += float(row["min_semantic_score"] or 0.0)
        _merge_suppressed(rec["suppressed"], row["suppressed_json"])

    intent_reports = [_recommend_for_intent(rec, min_samples=min_samples) for rec in by_intent.values()]
    intent_reports.sort(key=lambda r: (r["action"] == "observe_more", -r["calls"], r["intent"]))
    return {
        "window_hours": hours,
        "min_samples": min_samples,
        "recommendations": intent_reports,
    }


def _load_tuning_rows(hours: int, db_path: Path | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path or BRAIN_DB))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn)
        has_action_audit = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_audit'").fetchone()
            is not None
        )
        if not has_action_audit:
            return conn.execute(
                """
                SELECT prompt_intent,
                       needs_memory,
                       max_blocks,
                       max_tokens,
                       min_semantic_score,
                       block_count,
                       semantic_count,
                       suppressed_json,
                       latency_ms,
                       NULL AS outcome
                FROM active_recall_judgments
                WHERE created_at > datetime('now', ? || ' hours')
                """,
                (f"-{int(hours)}",),
            ).fetchall()
        return conn.execute(
            """
            SELECT arj.prompt_intent,
                   arj.needs_memory,
                   arj.max_blocks,
                   arj.max_tokens,
                   arj.min_semantic_score,
                   arj.block_count,
                   arj.semantic_count,
                   arj.suppressed_json,
                   arj.latency_ms,
                   aa.outcome
            FROM active_recall_judgments arj
            LEFT JOIN action_audit aa ON aa.id = arj.action_audit_id
            WHERE arj.created_at > datetime('now', ? || ' hours')
            """,
            (f"-{int(hours)}",),
        ).fetchall()
    finally:
        conn.close()


def _recommend_for_intent(rec: dict, *, min_samples: int) -> dict:
    calls = int(rec["calls"])
    negative_rate = _rate(rec["negative_outcomes"], max(1, rec["outcome_labeled"]))
    outcome_coverage = _rate(rec["outcome_labeled"], calls)
    suppression_rate = _rate(rec["suppressed_prompts"], calls)
    avg_blocks = rec["block_sum"] / calls if calls else 0.0
    avg_semantic = rec["semantic_sum"] / calls if calls else 0.0
    avg_latency = rec["latency_sum"] / calls if calls else 0.0
    current = {
        "max_blocks": round(rec["max_blocks_sum"] / calls) if calls else 0,
        "max_tokens": round(rec["max_tokens_sum"] / calls) if calls else 0,
        "min_semantic_score": round(rec["min_score_sum"] / calls, 3) if calls else 0.0,
    }
    suppressed = dict(sorted(rec["suppressed"].items()))
    per_call = {key: round(value / calls, 2) for key, value in suppressed.items()} if calls else {}

    recommendation = {
        "intent": rec["intent"],
        "calls": calls,
        "outcome_coverage": round(outcome_coverage, 3),
        "negative_rate": round(negative_rate, 3),
        "suppression_rate": round(suppression_rate, 3),
        "avg_blocks": round(avg_blocks, 2),
        "avg_semantic_blocks": round(avg_semantic, 2),
        "avg_latency_ms": round(avg_latency, 1),
        "suppressed_per_call": per_call,
        "current_policy": current,
        "proposed_policy": current.copy(),
        "action": "hold",
        "reason": "policy appears balanced for the observed window",
        "confidence": "medium",
    }

    if calls < min_samples:
        recommendation.update(
            {
                "action": "observe_more",
                "reason": f"need at least {min_samples} samples before tuning this intent",
                "confidence": "low",
            }
        )
        return recommendation

    if rec["intent"] in {"execution_control", "generic"}:
        if suppression_rate >= 0.8 and negative_rate <= 0.05:
            recommendation.update(
                {
                    "action": "keep_silent",
                    "reason": "short/generic prompts are being suppressed with no strong negative outcome signal",
                    "confidence": "high" if outcome_coverage >= 0.2 else "medium",
                }
            )
        elif negative_rate >= 0.2:
            proposed = current.copy()
            proposed["max_blocks"] = max(MIN_BLOCKS_CAP, current["max_blocks"] or 1)
            proposed["max_tokens"] = max(MIN_TOKENS_CAP, current["max_tokens"] or 600)
            recommendation.update(
                {
                    "action": "loosen_silence_gate",
                    "reason": "suppressed prompts show negative outcomes; allow a small memory budget",
                    "proposed_policy": proposed,
                    "confidence": "medium",
                }
            )
        return recommendation

    if avg_latency >= 900 and negative_rate <= 0.1:
        proposed = current.copy()
        proposed["max_blocks"] = max(MIN_BLOCKS_CAP, current["max_blocks"] - 1)
        proposed["max_tokens"] = max(MIN_TOKENS_CAP, current["max_tokens"] - 200)
        proposed["min_semantic_score"] = min(MAX_SCORE_CAP, round(current["min_semantic_score"] + 0.03, 3))
        recommendation.update(
            {
                "action": "tighten_for_latency",
                "reason": "latency is high without a matching negative-outcome signal",
                "proposed_policy": proposed,
                "confidence": "medium",
            }
        )
        return recommendation

    if negative_rate >= 0.2 and per_call.get("below_intent_score", 0.0) >= 0.5:
        proposed = current.copy()
        proposed["min_semantic_score"] = max(MIN_SCORE_CAP, round(current["min_semantic_score"] - 0.03, 3))
        recommendation.update(
            {
                "action": "lower_semantic_threshold",
                "reason": "many candidates are below threshold and labeled outcomes are negative",
                "proposed_policy": proposed,
                "confidence": "medium",
            }
        )
        return recommendation

    if negative_rate >= 0.2 and (
        per_call.get("over_budget", 0.0) >= 0.5 or avg_blocks >= current["max_blocks"] - 0.25
    ):
        proposed = current.copy()
        proposed["max_blocks"] = min(MAX_BLOCKS_CAP, current["max_blocks"] + 1)
        proposed["max_tokens"] = min(MAX_TOKENS_CAP, current["max_tokens"] + 200)
        recommendation.update(
            {
                "action": "increase_context_budget",
                "reason": "budget pressure coincides with negative outcomes",
                "proposed_policy": proposed,
                "confidence": "medium",
            }
        )
        return recommendation

    if per_call.get("near_duplicate", 0.0) >= 1.0 and negative_rate <= 0.1:
        recommendation.update(
            {
                "action": "keep_dedup",
                "reason": "duplicate suppression is active and not correlated with negative outcomes",
                "confidence": "medium",
            }
        )
        return recommendation

    if per_call.get("stale_or_superseded", 0.0) >= 0.3 and negative_rate <= 0.1:
        recommendation.update(
            {
                "action": "keep_stale_filter",
                "reason": "stale suppression is active and not correlated with negative outcomes",
                "confidence": "medium",
            }
        )
        return recommendation

    return recommendation


def _merge_suppressed(target: dict[str, int], raw_json: str | None) -> None:
    try:
        parsed = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    for key, value in parsed.items():
        try:
            target[str(key)] = target.get(str(key), 0) + int(value)
        except (TypeError, ValueError):
            continue


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--tuning", action="store_true")
    parser.add_argument("--min-samples", type=int, default=MIN_TUNING_SAMPLES)
    args = parser.parse_args()
    payload = (
        tuning_report(hours=args.hours, min_samples=args.min_samples)
        if args.tuning
        else report(hours=args.hours)
    )
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
