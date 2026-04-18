"""brain_core/confidence_calibration.py — Platt-scaling confidence calibration.

2026-04-16 Tier 3 #3: the atoms.confidence field is output of a Bayesian
ledger that's mathematically sound but has never been checked against
reality — at confidence 0.8, are those atoms actually right 80% of the
time? Without calibration, the value is directionally meaningful but
numerically untrustworthy, which breaks downstream uses (gating, filters,
proactive suggestions).

This module:
  1. Weekly job `calibrate_confidence` reads eval holdout results, pairs
     each query's returned atoms with their predicted confidence and
     observed correctness (`hit_content`), fits a logistic-regression
     (Platt, 1999) transform: calibrated = sigmoid(a * raw + b).
  2. Persists (a, b, fit_at, n_samples, reliability) to brain_config_store
     under key `confidence_calibration.v1`.
  3. `apply_calibration(raw: float) -> float` applied at read time by
     /recall/v2 so the surfaced confidence is the calibrated value,
     tagged with `raw_confidence` as a debug field.

When insufficient data exists (< MIN_SAMPLES), the calibration is an
identity transform and we tag it so callers can distinguish "calibrated"
from "uncalibrated." No LLM calls, no heavy deps — pure SQLite + math.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MIN_SAMPLES = 50
CALIBRATION_KEY = "confidence_calibration.v1"


def _load_params() -> dict | None:
    try:
        import brain_config_store

        raw = brain_config_store.get(CALIBRATION_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _save_params(params: dict) -> None:
    try:
        import brain_config_store

        brain_config_store.set(CALIBRATION_KEY, json.dumps(params), updated_by="confidence_calibration")
    except Exception:
        pass


def apply_calibration(raw: float) -> float:
    """Map raw confidence [0,1] to calibrated [0,1].

    No-op when calibration params are missing or stale — returns raw.
    """
    params = _load_params()
    if not params or not params.get("fitted"):
        return max(0.0, min(1.0, float(raw)))
    a = float(params.get("a", 1.0))
    b = float(params.get("b", 0.0))
    # Platt: P(correct | raw) = 1 / (1 + exp(-(a*raw + b)))
    try:
        return 1.0 / (1.0 + math.exp(-(a * raw + b)))
    except OverflowError:
        return 0.0 if (a * raw + b) < 0 else 1.0


def _logistic_fit(pairs: list[tuple[float, int]]) -> tuple[float, float]:
    """Minimal 2-parameter logistic regression (Newton-Raphson, 10 iter).

    pairs: list of (raw_confidence, correct_01).
    Returns (a, b). Returns (1.0, 0.0) on degenerate input.
    """
    if len(pairs) < 5:
        return (1.0, 0.0)
    a, b = 1.0, 0.0
    for _ in range(20):
        g_a = 0.0
        g_b = 0.0
        h_aa = 0.0
        h_ab = 0.0
        h_bb = 0.0
        for x, y in pairs:
            z = a * x + b
            # Clamp to avoid overflow
            if z > 30:
                p = 1.0
            elif z < -30:
                p = 0.0
            else:
                p = 1.0 / (1.0 + math.exp(-z))
            err = p - y
            g_a += err * x
            g_b += err
            w = p * (1 - p)
            h_aa += w * x * x
            h_ab += w * x
            h_bb += w
        # Add small L2 regularization to keep fit stable when data is sparse.
        h_aa += 1e-3
        h_bb += 1e-3
        # Solve 2x2 for step: H * [da; db] = [g_a; g_b]
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-9:
            break
        d_a = (h_bb * g_a - h_ab * g_b) / det
        d_b = (-h_ab * g_a + h_aa * g_b) / det
        a -= d_a
        b -= d_b
        if abs(d_a) < 1e-4 and abs(d_b) < 1e-4:
            break
    # Sanity: if the fit inverted the relationship (a < 0), fall back.
    if a <= 0:
        return (1.0, 0.0)
    return (a, b)


def _collect_pairs_from_eval() -> list[tuple[float, int]]:
    """Read eval holdout report + atoms.confidence to build training pairs."""
    pairs: list[tuple[float, int]] = []
    try:
        from atoms_store import BRAIN_DB
        from config import BRAIN_LOGS_DIR

        report_path = BRAIN_LOGS_DIR / "eval-report-stable.json"
        if not report_path.exists():
            report_path = BRAIN_LOGS_DIR / "eval-report.json"
        if not report_path.exists():
            return []
        data = json.loads(report_path.read_text())
        per_test = (data.get("v2") or {}).get("per_test") or []
        if not per_test:
            return []
        # Pull all atom confidences in one batch.
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            rows = conn.execute("SELECT chroma_id, confidence FROM atoms WHERE tier != 'obsolete'").fetchall()
        finally:
            conn.close()
        conf_by_id = {r[0]: float(r[1] or 0.5) for r in rows}
        for case in per_test:
            if not isinstance(case, dict):
                continue
            correct = 1 if case.get("hit_content") else 0
            top_ids = case.get("top_ids") or case.get("retrieved_ids") or []
            if not top_ids:
                continue
            # First retrieved id is the top-1 prediction.
            top = top_ids[0] if isinstance(top_ids, list) else None
            if top and top in conf_by_id:
                pairs.append((conf_by_id[top], correct))
    except Exception:
        return []
    return pairs


def _collect_pairs_from_outcomes(days_window: int = 90) -> list[tuple[float, int]]:
    """Pull (confidence_was, correctness) pairs from the task outcomes table.

    Ground truth signal: a task's recorded confidence was "correct" when Chris
    did not override (chris_override=0). This complements the eval holdout
    signal — outcomes measure real user-aligned decisions, not synthetic test
    queries. The two sources together close the calibration loop.
    """
    pairs: list[tuple[float, int]] = []
    try:
        from config import BRAIN_LOGS_DIR

        db_path = BRAIN_LOGS_DIR / "autonomy.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(str(db_path))
        try:
            # 2026-04-17 prod-review fix: parameterized days arg instead of
            # f-string interpolation. int cast makes it safe today but the
            # pattern violates brain's parameterized-SQL convention and is
            # one bad refactor away from a real injection.
            rows = conn.execute(
                "SELECT confidence_was, chris_override FROM outcomes "
                "WHERE confidence_was IS NOT NULL "
                "AND created_at > datetime('now', ? || ' days')",
                (f"-{int(days_window)}",),
            ).fetchall()
        finally:
            conn.close()
        for conf, override in rows:
            try:
                c = float(conf)
            except (TypeError, ValueError):
                continue
            if 0.0 <= c <= 1.0:
                correct = 0 if int(override or 0) else 1
                pairs.append((c, correct))
    except Exception:
        return []
    return pairs


def _collect_pairs() -> list[tuple[float, int]]:
    """Combined (confidence, correctness) pairs from eval holdout + task outcomes."""
    pairs = _collect_pairs_from_eval()
    pairs.extend(_collect_pairs_from_outcomes())
    return pairs


def _reliability(params_a: float, params_b: float, pairs: list[tuple[float, int]]) -> float:
    """Post-calibration Brier-inspired reliability score (lower = better).

    0.0 means perfect calibration at the sample pairs; higher means worse.
    """
    if not pairs:
        return 1.0
    err_sum = 0.0
    for x, y in pairs:
        z = params_a * x + params_b
        try:
            p = 1.0 / (1.0 + math.exp(-z))
        except OverflowError:
            p = 0.0 if z < 0 else 1.0
        err_sum += (p - y) ** 2
    return err_sum / len(pairs)


def run() -> dict:
    """Weekly calibration fit. Returns summary dict for the scheduler.

    W5 drift alarm (2026-04-17): compares the new reliability_brier against
    the previous fit's value and stores the absolute delta at
    brain_config_store key `confidence_calibration.drift_brier`. The
    `calibration_brier_drift_7d` SLO reads this value to alert on silent
    calibration drift (previously the self-learning loop updated parameters
    weekly with no oversight — if the ledger shifted toward miscalibration,
    no one would notice until stable eval regressed).
    """
    pairs = _collect_pairs()
    if len(pairs) < MIN_SAMPLES:
        return {
            "status": "insufficient_samples",
            "n_samples": len(pairs),
            "min_required": MIN_SAMPLES,
            "note": "Calibration remains identity; call /brain/doubt output is uncalibrated.",
        }
    a, b = _logistic_fit(pairs)
    reliability = _reliability(a, b, pairs)

    # Compute drift vs prior fit (absolute delta in reliability_brier)
    prior = _load_params() or {}
    prior_brier = prior.get("reliability_brier")
    drift = 0.0
    if prior_brier is not None:
        try:
            drift = round(abs(float(reliability) - float(prior_brier)), 4)
        except (TypeError, ValueError):
            drift = 0.0

    params = {
        "a": round(a, 4),
        "b": round(b, 4),
        "fitted": True,
        "fit_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_samples": len(pairs),
        "reliability_brier": round(reliability, 4),
        "prev_reliability_brier": prior_brier,
        "brier_drift": drift,
    }
    _save_params(params)

    # Persist the drift as a standalone key for the SLO measure function.
    # Separate key so the SLO read is cheap and the calibration payload stays clean.
    try:
        import brain_config_store

        brain_config_store.set(
            "confidence_calibration.drift_brier",
            json.dumps({"drift": drift, "fit_at": params["fit_at"]}),
            updated_by="confidence_calibration",
        )
    except Exception:
        pass

    return {"status": "ok", **params}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
