"""brain_core/metric_trend_tracker.py — daily snapshots + 7d drift alerts.

belief_state today shows a single instant: override_pct, breached SLOs,
judge coverage right now. There is no trend signal — "is this getting
worse?" requires querying historical data the brain doesn't surface.

This module persists a small daily metric vector and computes deltas
against the same vector from 7 days ago, surfacing the worst-drifting
metrics as alerts.

Storage: `brain_config_store` key `metric_trend.history` — a bounded
JSON list of `{ts, snapshot: {metric_name: value, ...}}` capped at 30
entries. O(1) memory.

Outputs:
  * `snapshot_now()` — record today's vector, return summary.
  * `compute_trend_alerts()` — read history, return drift alerts.
  * Integrated into belief_state.world_model.trend_alerts.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.metric_trend_tracker")


HISTORY_KEY = "metric_trend.history"
HISTORY_MAX_ENTRIES = 30
DRIFT_BASELINE_HOURS = 24 * 7
DRIFT_BASELINE_TOLERANCE_HOURS = 36  # accept 6d-9d gap as ~7d


# Metric-name → human-readable description, used for alert text. Adding a
# metric here is the only step needed to track its 7d drift.
TRACKED_METRICS: dict[str, dict] = {
    "override_pct.coding": {
        "label": "coding override rate",
        "lower_is_better": True,
        "drift_pct_alert": 10.0,  # alert when 7d delta worsens by 10 pct points
    },
    "override_pct.infra": {
        "label": "infra override rate",
        "lower_is_better": True,
        "drift_pct_alert": 10.0,
    },
    "override_pct.brain": {
        "label": "brain domain override rate",
        "lower_is_better": True,
        "drift_pct_alert": 10.0,
    },
    "recall_judge.judged_pct_7d": {
        "label": "/recall judge coverage",
        "lower_is_better": False,
        "drift_pct_alert": 1.0,
    },
    "atoms.low_confidence_count": {
        "label": "non-conjecture low-confidence atoms",
        "lower_is_better": True,
        "drift_pct_alert": None,  # absolute delta alert below
        "drift_abs_alert": 5.0,
    },
    "slo.breached_count": {
        "label": "breached SLOs",
        "lower_is_better": True,
        "drift_pct_alert": None,
        "drift_abs_alert": 1.0,
    },
}


def snapshot_now() -> dict:
    """Sample the tracked metrics and append a bounded history row."""
    snap = _build_snapshot()
    if not snap:
        return {"status": "no_snapshot", "ts": _now_iso()}
    try:
        import brain_config_store

        raw = brain_config_store.get(HISTORY_KEY) or "[]"
        try:
            history = json.loads(raw)
            if not isinstance(history, list):
                history = []
        except json.JSONDecodeError:
            history = []
        history.append({"ts": _now_iso(), "snapshot": snap})
        history = history[-HISTORY_MAX_ENTRIES:]
        brain_config_store.set(
            HISTORY_KEY,
            json.dumps(history, separators=(",", ":")),
            updated_by="metric_trend_tracker.snapshot_now",
        )
        return {"status": "ok", "ts": _now_iso(), "metrics": len(snap), "entries": len(history)}
    except Exception as exc:
        log.warning("metric_trend snapshot write failed: %s", exc)
        return {"status": f"error:{str(exc)[:120]}", "ts": _now_iso()}


def compute_trend_alerts() -> list[dict]:
    """Return alerts where a tracked metric drifted in the bad direction over ~7d."""
    try:
        import brain_config_store

        raw = brain_config_store.get(HISTORY_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        history = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(history, list) or len(history) < 2:
        return []
    latest = history[-1]
    baseline = _baseline_entry(history)
    if not baseline:
        return []
    latest_snap = latest.get("snapshot") or {}
    baseline_snap = baseline.get("snapshot") or {}
    alerts: list[dict] = []
    for metric, cfg in TRACKED_METRICS.items():
        cur = _to_float(latest_snap.get(metric))
        prev = _to_float(baseline_snap.get(metric))
        if cur is None or prev is None:
            continue
        delta = cur - prev
        worse = (delta > 0) if cfg["lower_is_better"] else (delta < 0)
        if not worse:
            continue
        drift_pct = cfg.get("drift_pct_alert")
        drift_abs = cfg.get("drift_abs_alert")
        triggered = False
        if drift_pct is not None and abs(delta) >= drift_pct:
            triggered = True
        if drift_abs is not None and abs(delta) >= drift_abs:
            triggered = True
        if not triggered:
            continue
        alerts.append(
            {
                "metric": metric,
                "label": cfg["label"],
                "current": cur,
                "baseline": prev,
                "delta": round(delta, 3),
                "lower_is_better": cfg["lower_is_better"],
                "baseline_ts": baseline.get("ts"),
                "latest_ts": latest.get("ts"),
            }
        )
    alerts.sort(key=lambda a: abs(a["delta"]), reverse=True)
    return alerts


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_snapshot() -> dict[str, float]:
    """Re-uses the same metric sources as subtask_evaluator so the trend
    history matches what /brain/state and goal scaffolds see."""
    snap: dict[str, float] = {}
    try:
        from subtask_evaluator import _metric_snapshot

        snap.update(_metric_snapshot(None, None))
    except Exception as exc:
        log.debug("base metric snapshot failed: %s", exc)
    snap["slo.breached_count"] = _slo_breached_count()
    return snap


def _slo_breached_count() -> float:
    try:
        from slos import evaluate_slos

        result = evaluate_slos(send_alerts=False)
        return float(result.get("breached", 0))
    except Exception:
        return 0.0


def _baseline_entry(history: list[dict]) -> dict | None:
    if not history:
        return None
    try:
        latest_ts = datetime.fromisoformat(str(history[-1]["ts"]).replace("Z", "+00:00"))
    except (TypeError, ValueError, KeyError):
        return None
    target = latest_ts - timedelta(hours=DRIFT_BASELINE_HOURS)
    tol = DRIFT_BASELINE_TOLERANCE_HOURS * 3600
    best: dict | None = None
    best_gap: float | None = None
    for entry in history[:-1]:
        try:
            ts = datetime.fromisoformat(str(entry.get("ts")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        gap = abs((ts - target).total_seconds())
        if gap > tol:
            continue
        if best_gap is None or gap < best_gap:
            best = entry
            best_gap = gap
    return best


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    # 2026-05-15 P2-8: delegate to shared helper.
    from db import now_iso

    return now_iso()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("alerts")
    args = p.parse_args()
    if args.cmd == "snapshot":
        print(json.dumps(snapshot_now(), indent=2, default=str))
    elif args.cmd == "alerts":
        print(json.dumps(compute_trend_alerts(), indent=2, default=str))
