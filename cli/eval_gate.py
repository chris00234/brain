#!/opt/homebrew/bin/python3
"""eval_gate.py — regression gate wrapping eval_compare.py.

Runs the eval suite and compares the result to a stored baseline
(brain/tests/eval_baseline.json). Exits non-zero + alerts via Telegram
(Jenna) if the mean score drops more than --threshold percent.

Wired via the `eval_run` scheduled job (Sunday 4:30am). First run bootstraps
the baseline so subsequent runs have something to compare against.

Usage:
  eval_gate.py                           # compare against baseline, fail on regression
  eval_gate.py --update-baseline         # overwrite baseline with current run
  eval_gate.py --threshold 7             # allow 7% drop before failing
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
EVAL_COMPARE = BRAIN_ROOT / "cli" / "eval_compare.py"
BASELINE_FILE = BRAIN_ROOT / "cli" / "eval_baseline.json"
OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"


def _persist_eval_report(report: dict) -> None:
    """Write eval-report.json + append eval-history.jsonl so Brain UI stays current."""
    logs = BRAIN_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    v2 = report.get("v2", {})
    total = int(v2.get("total", report.get("cases", 0)))
    content_pct = float(v2.get("hit_content_pct", 0))
    passed = round(total * content_pct / 100) if total else 0
    failed = total - passed
    # eval-report.json (for Brain UI)
    (logs / "eval-report.json").write_text(json.dumps({
        "timestamp": str(datetime.now()),
        "passed": passed, "failed": failed, "accuracy": round(content_pct, 1),
        "slow_count": 0, "v2": v2,
    }, indent=2, ensure_ascii=False))
    # eval-history.jsonl (append-only for regression tracking)
    with (logs / "eval-history.jsonl").open("a") as hf:
        hf.write(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "total": total, "passed": passed, "failed": failed,
            "accuracy": round(content_pct, 1), "slow_count": 0,
        }, ensure_ascii=False) + "\n")


def run_current_eval() -> dict:
    """Run eval_compare.py --json and return the parsed report."""
    result = subprocess.run(
        ["/opt/homebrew/bin/python3", str(EVAL_COMPARE), "--json"],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"eval_compare failed: {result.stderr[:300]}")
    return json.loads(result.stdout)


def load_baseline() -> dict | None:
    if not BASELINE_FILE.exists():
        return None
    return json.loads(BASELINE_FILE.read_text())


def write_baseline(report: dict) -> None:
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    report_with_ts = {**report, "baseline_written_at": datetime.now().isoformat(timespec="seconds")}
    BASELINE_FILE.write_text(json.dumps(report_with_ts, indent=2) + "\n")


def alert_chris(message: str) -> None:
    """Send a regression alert via OpenClaw Jenna (Telegram)."""
    try:
        subprocess.run(
            [
                OPENCLAW_BIN, "agent",
                "--agent", "jenna",
                "--message",
                f"[BRAIN EVAL ALERT] {message}\n\nPlease check brain eval regression ASAP.",
                "--deliver",
                "--json",
                "--thinking", "off",
                "--timeout", "60",
            ],
            capture_output=True, text=True, timeout=90,
        )
    except Exception:
        pass  # alerting must never block the gate


def main() -> int:
    parser = argparse.ArgumentParser(description="eval regression gate")
    parser.add_argument("--update-baseline", action="store_true",
                        help="overwrite baseline with the current run")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="max allowed drop in hit_content_pct (default 5.0 percentage points)")
    parser.add_argument("--max-baseline-age-days", type=int, default=30,
                        help="auto-refresh baseline if older than N days AND current is ≥ baseline (default 30)")
    args = parser.parse_args()

    print(f"[eval_gate] running eval at {datetime.now().isoformat(timespec='seconds')}")
    try:
        report = run_current_eval()
    except Exception as e:
        print(f"[eval_gate] ERROR running eval: {e}", file=sys.stderr)
        return 2

    # Write eval-report.json and eval-history.jsonl so Brain UI stays current
    _persist_eval_report(report)

    current = report.get("v2", {})
    current_content = float(current.get("hit_content_pct", 0))
    current_source = float(current.get("hit_source_pct", 0))
    print(f"[eval_gate] current /recall/v2: hit_content@5={current_content}% hit_source@5={current_source}%")

    if args.update_baseline:
        write_baseline(report)
        print(f"[eval_gate] baseline overwritten at {BASELINE_FILE}")
        return 0

    baseline = load_baseline()
    if baseline is None:
        write_baseline(report)
        print(f"[eval_gate] no baseline existed, bootstrapped at {BASELINE_FILE}")
        return 0

    baseline_current = baseline.get("v2", {})
    baseline_content = float(baseline_current.get("hit_content_pct", 0))
    baseline_source = float(baseline_current.get("hit_source_pct", 0))
    print(f"[eval_gate] baseline /recall/v2: hit_content@5={baseline_content}% hit_source@5={baseline_source}%")

    delta_content = current_content - baseline_content
    delta_source = current_source - baseline_source
    print(f"[eval_gate] delta content@5: {delta_content:+.1f}pts, source@5: {delta_source:+.1f}pts")

    if delta_content < -args.threshold:
        msg = (
            f"REGRESSION: hit_content@5 dropped {-delta_content:.1f}pts "
            f"(baseline={baseline_content}%, current={current_content}%, threshold={args.threshold}pts)"
        )
        print(f"[eval_gate] {msg}", file=sys.stderr)
        alert_chris(msg)

        # Phase A2: auto-remediation via self_heal dispatcher
        try:
            from self_heal import HealingSignal, dispatch as heal_dispatch
            # Only triggers if BRAIN_AUTO_HEAL_ENABLED=true
            heal_dispatch(HealingSignal(
                source="eval_gate",
                signal_type="eval_regression",
                severity="high" if delta_content <= -10 else "medium",
                metric="hit_content_pct",
                value=current_content,
                baseline=baseline_content,
                target="semantic_memory",
                context={"delta": delta_content, "threshold": args.threshold},
            ))
        except Exception as e:
            print(f"[eval_gate] self_heal dispatch failed: {e}", file=sys.stderr)

        return 1

    # Auto-refresh baseline on passing runs if stale. Without this, a steady
    # ~4.9pt drift each month goes undetected because each individual delta
    # is just under the threshold.
    try:
        baseline_ts = baseline.get("baseline_written_at", "")
        if baseline_ts:
            dt_baseline = datetime.fromisoformat(baseline_ts)
            age_days = (datetime.now() - dt_baseline).days
            if age_days > args.max_baseline_age_days and current_content >= baseline_content:
                write_baseline(report)
                print(f"[eval_gate] baseline auto-refreshed (was {age_days}d old, current ≥ baseline)")
    except Exception as e:
        print(f"[eval_gate] baseline age check failed: {e}", file=sys.stderr)

    print(f"[eval_gate] PASS (within {args.threshold}pts threshold)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
