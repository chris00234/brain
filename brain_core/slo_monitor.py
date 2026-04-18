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
from datetime import UTC, datetime
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

# Content quality baselines — 2026-04-17 recalibrated after the eval path
# swap from eval-report.json (extended track, 71.9%) to eval-report-stable.json
# (stable track, currently 97.8% content / 89.1% source). Prior thresholds
# (67.0 / 76.0) were stale against the new track and effectively disabled the
# content-quality auto-heal path. Set ~4pt below stable-track numbers so
# normal variance doesn't trip the doom loop.
CONTENT_QUALITY_SLOS = {
    "content_hit_pct": 93.0,
    "source_hit_pct": 84.0,
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
    """Check content/source hit rates against the STABLE eval (not extended).

    Previously this read eval-report.json which is the extended 743-query
    eval set — explicitly trend-only per CLAUDE.md ("tracks literal-wording
    queries vs consolidated abstractions — trend-only, not a regression
    gate"). Reading the extended eval caused a 42-hour breach loop on
    source_hit_pct (71.9 vs a threshold miscalibrated against old ~81
    numbers). Now reads eval-report-stable.json (138-query regression gate)
    which legitimately measures health. The stable-track content hit is
    also covered by slos.py recall_v2_content_hit_pct, so this check is
    left only as a redundant observability belt.
    """
    stable_path = EVAL_REPORT_FILE.parent / "eval-report-stable.json"
    report_path = stable_path if stable_path.exists() else EVAL_REPORT_FILE
    if not report_path.exists():
        return []
    try:
        report = json.loads(report_path.read_text())
    except Exception:
        return []

    v2 = report.get("v2", {})
    if not v2:
        return []

    violations = []
    content_pct = float(v2.get("hit_content_pct", 0))
    source_pct = float(v2.get("hit_source_pct", 0))

    if content_pct > 0 and content_pct < CONTENT_QUALITY_SLOS["content_hit_pct"]:
        violations.append(
            {
                "slo": "content_hit_pct",
                "current": content_pct,
                "baseline": CONTENT_QUALITY_SLOS["content_hit_pct"],
                "threshold": CONTENT_QUALITY_SLOS["content_hit_pct"],
            }
        )
    if source_pct > 0 and source_pct < CONTENT_QUALITY_SLOS["source_hit_pct"]:
        violations.append(
            {
                "slo": "source_hit_pct",
                "current": source_pct,
                "baseline": CONTENT_QUALITY_SLOS["source_hit_pct"],
                "threshold": CONTENT_QUALITY_SLOS["source_hit_pct"],
            }
        )
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
        violations.append(
            {
                "slo": "recall_p95_ms",
                "current": recall_p95,
                "baseline": baseline["recall_p95_ms"],
                "threshold": recall_threshold,
            }
        )

    v2_threshold = max(baseline["recall_v2_p95_ms"] * 2, baseline["recall_v2_p95_ms"])
    v2_p95 = current["recall_v2"]["p95"]
    if current["recall_v2"]["samples"] >= 2 and v2_p95 > v2_threshold:
        violations.append(
            {
                "slo": "recall_v2_p95_ms",
                "current": v2_p95,
                "baseline": baseline["recall_v2_p95_ms"],
                "threshold": v2_threshold,
            }
        )

    return {
        "status": "ok" if not violations else "breached",
        "current": current,
        "baseline": baseline,
        "violations": violations,
    }


# Throttle jenna dispatches so hourly ticks can't spam the LLM-backed agent
# once an SLO sits below threshold for hours. 6-hour floor means at most 4
# dispatches per SLO per day even in the worst case. Persists in brain_config
# so restarts don't reset the clock.
_ALERT_DISPATCH_FLOOR_S = 6 * 3600
_ALERT_KEY_PREFIX = "slo_monitor_alert."


def _in_quiet_hours_now() -> bool:
    """Read persisted quiet_hours.* keys directly — the autonomy.QUIET_HOURS
    constant doesn't pick up POST /brain/quiet-hours overrides, so relying
    on it silently ignores Chris's configured window."""
    try:
        from datetime import time as _dtime
        from zoneinfo import ZoneInfo

        import brain_config_store

        # 2026-04-17 fix: match documented 22:30–07:30 PT window (see slos.py).
        start_s = brain_config_store.get("quiet_hours.start") or "22:30"
        end_s = brain_config_store.get("quiet_hours.end") or "07:30"
        tz_s = brain_config_store.get("quiet_hours.tz") or "America/Los_Angeles"
        start = _dtime.fromisoformat(start_s)
        end = _dtime.fromisoformat(end_s)
        t = datetime.now(ZoneInfo(tz_s)).time()
        if start > end:
            return t >= start or t < end
        return start <= t < end
    except Exception:
        return False


def _alert_key(alerts: list[str]) -> str:
    # Key by the set of SLO names in the alert bundle so switching breach sets
    # is treated as a distinct event.
    names = sorted({a.split(" ")[1] for a in alerts if a.startswith("SLO ")})
    return _ALERT_KEY_PREFIX + "|".join(names)


def _should_dispatch_alert(alerts: list[str]) -> bool:
    if _in_quiet_hours_now():
        return False
    try:
        import brain_config_store

        key = _alert_key(alerts)
        last = brain_config_store.get(key)
        if last and (time.time() - float(last)) < _ALERT_DISPATCH_FLOOR_S:
            return False
    except Exception:
        pass
    return True


def _record_alert_dispatched(alerts: list[str]) -> None:
    try:
        import brain_config_store

        brain_config_store.set(_alert_key(alerts), f"{time.time():.0f}", updated_by="slo_monitor")
    except Exception:
        pass


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
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            if _should_dispatch_alert(alerts):
                from cli_llm import dispatch

                dispatch(
                    agent="jenna",
                    message="BRAIN SLO ALERT:\n" + "\n".join(alerts),
                    thinking="off",
                    timeout=30,
                )
                _record_alert_dispatched(alerts)
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
    # 2026-04-16 R-10: gate-blocked is NOT a failure — it's the autonomy
    # system working as designed. Previously this raised RuntimeError,
    # which bubbled up as a scheduler job failure and tripped the
    # /brain/health "1 job failed" degraded status on every slo_monitor
    # tick when quiet hours / kill-switch engaged. Now: log + skip heal.
    try:
        if not _slo_gate_allowed:
            # Record telemetry but do not raise — autonomy said no, we respect it.
            print("[slo_monitor] autonomy gate denied slo.remediate; skipping heal dispatch")
            return {"status": "gate_denied", "violations": len(violations)}
        from self_heal import HealingSignal
        from self_heal import dispatch as heal_dispatch

        # Latency breaches
        for v in violations:
            slo_name = v["slo"]
            count = state["violations"].get(slo_name, 0)
            if count >= 5:
                heal_dispatch(
                    HealingSignal(
                        source="slo_monitor",
                        signal_type="slo_latency_breach",
                        severity="high" if count >= 8 else "medium",
                        metric=slo_name,
                        value=v.get("current", 0),
                        baseline=v.get("baseline", 0),
                        target="recall",
                        context={"breach_count": count},
                    )
                )
        # Content quality breaches — trigger reindex via eval_regression healer
        for v in content_violations:
            slo_name = v["slo"]
            count = state["violations"].get(slo_name, 0)
            if count >= 2:
                heal_dispatch(
                    HealingSignal(
                        source="slo_monitor",
                        signal_type="content_quality_breach",
                        severity="high" if count >= 4 else "medium",
                        metric=slo_name,
                        value=v.get("current", 0),
                        baseline=v.get("baseline", 0),
                        target="recall",
                        context={"breach_count": count},
                    )
                )
    except Exception as e:
        # 2026-04-17: distinguish autonomy-gate-denied (normal behavior)
        # from real self_heal failures. Gate denies when L3 perm missing
        # or quiet hours — it's a policy decision, not an error, and
        # shouldn't pollute the scheduler's error count (was causing
        # /brain/health to flag 14 spurious errors/day).
        msg = str(e)
        if "blocked by autonomy gate" in msg or "autonomy gate" in msg.lower():
            log.info("self_heal gate-denied (policy, not an error): %s", msg)
        else:
            log.warning("self_heal dispatch failed: %s", e)

    state["last_check"] = datetime.now(UTC).isoformat()

    # Append to history (keep last 168 — 1 week of hourly checks)
    state["history"].append(
        {
            "timestamp": state["last_check"],
            "status": result["status"],
            "recall_p95": result["current"]["recall"]["p95"],
            "recall_v2_p95": result["current"]["recall_v2"]["p95"],
        }
    )
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
