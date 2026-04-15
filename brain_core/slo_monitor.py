#!/opt/homebrew/bin/python3
"""brain_core/slo_monitor.py — SLO tracking and alerts.

Runs hourly. Probes /recall (+/recall/v2 sparingly) with a fixed query set to
sample current p95 latency, compares against baseline from
tests/slo_baseline.json, and alerts via Jenna on 3 consecutive violations.

Runs as a subprocess dispatched by server.py JOB_REGISTRY. Cannot read the
in-process metrics buffer directly, so it performs a fresh probe each cycle.
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("brain.slo_monitor")

BRAIN_URL = "http://127.0.0.1:8791"
BASELINE_FILE = Path("/Users/chrischo/server/brain/tests/slo_baseline.json")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/slo_state.json")
EVAL_REPORT_FILE = Path("/Users/chrischo/server/brain/logs/eval-report.json")

# Default SLOs if baseline doesn't exist. Values aligned to the production
# SLO thresholds in brain_core/slos.py so the monitor and the SLO gauge don't
# disagree on what a breach means.
DEFAULT_SLOS = {
    "recall_p95_ms": 350,
    "recall_v2_p95_ms": 350,
    "memory_growth_weekly_pct": 20,
}

# Content quality baselines — calibrated to the expanded n=744 eval set
# (2026-04-13). Set ~4pt below the current honest number so normal run-to-run
# variance doesn't trip the auto-heal doom loop. Tighten as the corpus matures.
CONTENT_QUALITY_SLOS = {
    "content_hit_pct": 67.0,  # current ~71.9 on full set
    "source_hit_pct": 76.0,   # current ~81.2 on full set
}

# Fixed probe queries so measurements are comparable across cycles
PROBE_QUERIES = [
    "docker service",
    "python pipeline",
    "openclaw agent",
    "brain memory",
    "chromadb collection",
    "neo4j graph",
    "canonical knowledge",
    "search index",
    "nginx reverse proxy",
    "cloudflare tunnel",
    "apple notes personal",
    "imessage conversation",
    "calendar schedule",
    "reminders tasks",
    "daily synthesis",
    "embedding model ollama",
    "homelab infrastructure",
    "git commit history",
    "profile preferences",
    "weekly reflection",
]


def _token() -> str:
    from config import load_bearer_secret
    return load_bearer_secret()


def _probe_latency(endpoint: str, query: str, token: str, timeout: int = 10) -> float | None:
    """Hit endpoint once, return latency ms or None on failure."""
    url = f"{BRAIN_URL}{endpoint}?q={urllib.parse.quote_plus(query)}&n=5"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return (time.time() - start) * 1000
    except Exception:
        return None


def _p95(latencies: list[float]) -> float:
    if not latencies:
        return 0.0
    s = sorted(latencies)
    return round(s[min(len(s) - 1, int(len(s) * 0.95))], 1)


def load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        return DEFAULT_SLOS.copy()
    try:
        b = json.loads(BASELINE_FILE.read_text())
        return {
            "recall_p95_ms": b.get("recall", {}).get("p95") or DEFAULT_SLOS["recall_p95_ms"],
            "recall_v2_p95_ms": b.get("recall_v2", {}).get("p95") or DEFAULT_SLOS["recall_v2_p95_ms"],
            "memory_growth_weekly_pct": DEFAULT_SLOS["memory_growth_weekly_pct"],
        }
    except Exception:
        return DEFAULT_SLOS.copy()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"violations": {}, "last_check": None, "history": []}
    try:
        data = json.loads(STATE_FILE.read_text())
        data.setdefault("violations", {})
        data.setdefault("history", [])
        return data
    except Exception:
        return {"violations": {}, "last_check": None, "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def probe() -> dict:
    """Run probe queries and return current latency stats."""
    token = _token()
    recall_latencies: list[float] = []
    for q in PROBE_QUERIES:
        lat = _probe_latency("/recall", q, token)
        if lat is not None:
            recall_latencies.append(lat)

    # v2 is expensive — sample 3 queries
    v2_latencies: list[float] = []
    for q in random.sample(PROBE_QUERIES, min(3, len(PROBE_QUERIES))):
        lat = _probe_latency("/recall/v2", q, token, timeout=15)
        if lat is not None:
            v2_latencies.append(lat)

    return {
        "recall": {
            "samples": len(recall_latencies),
            "p95": _p95(recall_latencies),
            "mean": round(sum(recall_latencies) / len(recall_latencies), 1) if recall_latencies else 0,
        },
        "recall_v2": {
            "samples": len(v2_latencies),
            "p95": _p95(v2_latencies),
            "mean": round(sum(v2_latencies) / len(v2_latencies), 1) if v2_latencies else 0,
        },
    }


def check_content_quality() -> list[dict]:
    """Read latest eval report and check content/source hit rates against SLOs."""
    if not EVAL_REPORT_FILE.exists():
        return []
    try:
        report = json.loads(EVAL_REPORT_FILE.read_text())
    except Exception:
        return []

    v2 = report.get("v2", {})
    if not v2:
        return []

    violations = []
    content_pct = float(v2.get("hit_content_pct", 0))
    source_pct = float(v2.get("hit_source_pct", 0))

    if content_pct > 0 and content_pct < CONTENT_QUALITY_SLOS["content_hit_pct"]:
        violations.append({
            "slo": "content_hit_pct",
            "current": content_pct,
            "baseline": CONTENT_QUALITY_SLOS["content_hit_pct"],
            "threshold": CONTENT_QUALITY_SLOS["content_hit_pct"],
        })
    if source_pct > 0 and source_pct < CONTENT_QUALITY_SLOS["source_hit_pct"]:
        violations.append({
            "slo": "source_hit_pct",
            "current": source_pct,
            "baseline": CONTENT_QUALITY_SLOS["source_hit_pct"],
            "threshold": CONTENT_QUALITY_SLOS["source_hit_pct"],
        })
    return violations


def check_slos() -> dict:
    """Probe current latency and compare against SLOs. Returns status dict."""
    current = probe()
    baseline = load_baseline()
    violations = []

    # Threshold = 2x baseline, floor at baseline itself. Baselines default to
    # the slos.py production SLO (350ms) so monitor breaches align with the
    # SLO gauge's notion of a breach.
    recall_threshold = max(baseline["recall_p95_ms"] * 2, baseline["recall_p95_ms"])
    recall_p95 = current["recall"]["p95"]
    if current["recall"]["samples"] >= 5 and recall_p95 > recall_threshold:
        violations.append({
            "slo": "recall_p95_ms",
            "current": recall_p95,
            "baseline": baseline["recall_p95_ms"],
            "threshold": recall_threshold,
        })

    v2_threshold = max(baseline["recall_v2_p95_ms"] * 2, baseline["recall_v2_p95_ms"])
    v2_p95 = current["recall_v2"]["p95"]
    if current["recall_v2"]["samples"] >= 2 and v2_p95 > v2_threshold:
        violations.append({
            "slo": "recall_v2_p95_ms",
            "current": v2_p95,
            "baseline": baseline["recall_v2_p95_ms"],
            "threshold": v2_threshold,
        })

    return {
        "status": "ok" if not violations else "breached",
        "current": current,
        "baseline": baseline,
        "violations": violations,
    }


def monitor_cycle() -> dict:
    """One monitoring cycle. Tracks consecutive violations, alerts at 3."""
    state = load_state()
    result = check_slos()
    violations = result.get("violations", [])

    # Content quality check (from latest eval report)
    content_violations = check_content_quality()
    all_violations = violations + content_violations

    # Track consecutive violations per SLO
    for v in all_violations:
        slo_name = v["slo"]
        state["violations"][slo_name] = state["violations"].get(slo_name, 0) + 1

    # Reset counters for SLOs that are now healthy
    violated_slos = {v["slo"] for v in all_violations}
    for slo_name in list(state["violations"].keys()):
        if slo_name not in violated_slos:
            state["violations"][slo_name] = 0

    # Alert on 3+ consecutive latency violations, 2+ consecutive content violations
    alerts: list[str] = []
    content_slo_names = set(CONTENT_QUALITY_SLOS.keys())
    for slo_name, count in state["violations"].items():
        threshold = 2 if slo_name in content_slo_names else 3
        if count >= threshold:
            alerts.append(f"SLO {slo_name} breached for {count} consecutive checks")

    if alerts:
        try:
            # Lazy import — only when alerting to avoid pulling openclaw on every cycle
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from openclaw_dispatch import dispatch
            dispatch(
                agent="jenna",
                message="BRAIN SLO ALERT:\n" + "\n".join(alerts),
                thinking="off",
                timeout=30,
            )
        except Exception as e:
            log.warning("alert dispatch failed: %s", e)

    # Phase 5: outer autonomy gate before any heal_dispatch.
    # Inner self_heal.dispatch() also gates per-kind, but we short-circuit early
    # so we don't even build healing signals when slo.remediate is blocked.
    _slo_gate_allowed = True
    try:
        from autonomy import authorize as _autonomy_authorize

        gate = _autonomy_authorize("slo.remediate")
        _slo_gate_allowed = gate.allowed
    except Exception:
        pass

    # Phase A3: auto-remediation via self_heal
    try:
        if not _slo_gate_allowed:
            raise RuntimeError("slo.remediate blocked by autonomy gate")
        from self_heal import HealingSignal, dispatch as heal_dispatch
        # Latency breaches
        for v in violations:
            slo_name = v["slo"]
            count = state["violations"].get(slo_name, 0)
            if count >= 5:
                heal_dispatch(HealingSignal(
                    source="slo_monitor",
                    signal_type="slo_latency_breach",
                    severity="high" if count >= 8 else "medium",
                    metric=slo_name,
                    value=v.get("current", 0),
                    baseline=v.get("baseline", 0),
                    target="recall",
                    context={"breach_count": count},
                ))
        # Content quality breaches — trigger reindex via eval_regression healer
        for v in content_violations:
            slo_name = v["slo"]
            count = state["violations"].get(slo_name, 0)
            if count >= 2:
                heal_dispatch(HealingSignal(
                    source="slo_monitor",
                    signal_type="content_quality_breach",
                    severity="high" if count >= 4 else "medium",
                    metric=slo_name,
                    value=v.get("current", 0),
                    baseline=v.get("baseline", 0),
                    target="recall",
                    context={"breach_count": count},
                ))
    except Exception as e:
        log.warning("self_heal dispatch failed: %s", e)

    state["last_check"] = datetime.now(timezone.utc).isoformat()

    # Append to history (keep last 168 — 1 week of hourly checks)
    state["history"].append({
        "timestamp": state["last_check"],
        "status": result["status"],
        "recall_p95": result["current"]["recall"]["p95"],
        "recall_v2_p95": result["current"]["recall_v2"]["p95"],
    })
    state["history"] = state["history"][-168:]

    save_state(state)

    # Merge content quality info into result
    if content_violations:
        result["status"] = "breached"
        result["violations"] = all_violations

    return {
        **result,
        "consecutive_violations": state["violations"],
        "alerts": alerts,
    }


if __name__ == "__main__":
    result = monitor_cycle()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "ok" else 1)
