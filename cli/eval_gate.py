#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_gate.py — regression gate wrapping eval_compare.py.

Runs the eval suite and compares the result to a stored baseline
(brain/tests/eval_baseline.json). Exits non-zero + alerts via Telegram
(Jenna) if the mean score drops more than --threshold percent.

Wired via the `eval_run` scheduled job (Sunday 4:30am). First run bootstraps
the baseline so subsequent runs have something to compare against.

Two-track design (incident 2026-04-13):
  - --eval-set cli/eval_set_stable.json    → strict gate, heal dispatch on regression
  - --eval-set cli/eval_set_extended.json  → trend tracking, alert only on big drops
  - --eval-set cli/eval_set.json (default) → full set, baseline auto-refreshes
The baseline file is selected via --baseline so each track has its own.

Usage:
  eval_gate.py                                    # full set, default baseline
  eval_gate.py --eval-set cli/eval_set_stable.json --baseline cli/eval_baseline_stable.json
  eval_gate.py --update-baseline                  # overwrite baseline with current run
  eval_gate.py --threshold 7                      # allow 7% drop before failing
  eval_gate.py --no-heal                          # don't dispatch heal on regression (extended track)
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
DEFAULT_EVAL_SET = BRAIN_ROOT / "cli" / "eval_set.json"
DEFAULT_BASELINE = BRAIN_ROOT / "cli" / "eval_baseline.json"
PENDING_HOLDOUT = BRAIN_ROOT / "cli" / "eval_holdout_pending.json"
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BRAIN_URL = "http://127.0.0.1:8791"
OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_ACCOUNT = "jenna-bot"


def _persist_eval_report(report: dict, track: str = "default") -> None:
    """Write eval-report.json + append eval-history.jsonl so Brain UI stays current.

    `track` lets the two-track gate persist separate per-track histories without
    clobbering the legacy single-file UI integration. The default track keeps
    writing to the original paths; named tracks write to *_<track>.{json,jsonl}.
    """
    logs = BRAIN_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    v2 = report.get("v2", {})
    total = int(v2.get("total", report.get("cases", 0)))
    content_pct = float(v2.get("hit_content_pct", 0))
    passed = round(total * content_pct / 100) if total else 0
    failed = total - passed

    if track == "default":
        report_path = logs / "eval-report.json"
        history_path = logs / "eval-history.jsonl"
    else:
        report_path = logs / f"eval-report-{track}.json"
        history_path = logs / f"eval-history-{track}.jsonl"

    report_path.write_text(
        json.dumps(
            {
                "timestamp": str(datetime.now()),
                "track": track,
                "passed": passed,
                "failed": failed,
                "accuracy": round(content_pct, 1),
                "slow_count": 0,
                "v2": v2,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    with history_path.open("a") as hf:
        hf.write(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "track": track,
                    "total": total,
                    "passed": passed,
                    "failed": failed,
                    "accuracy": round(content_pct, 1),
                    "slow_count": 0,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def run_current_eval(eval_set_path: Path) -> dict:
    """Run eval_compare.py --json --eval-set <path> and return the parsed report.

    Timeout scales with eval set size: ~500ms per case × 2 endpoints + margin.
    """
    try:
        n_cases = len(json.loads(eval_set_path.read_text())) if eval_set_path.exists() else 500
    except Exception:
        n_cases = 500
    timeout_s = max(600, n_cases * 2 + 180)
    # 2026-04-17 Phase 1 enabler: include per_test so eval-report-stable.json
    # carries labeled case-by-case results (hit_content + top_ids), which the
    # bootstrap_feedback_from_eval.py script + future LtR trainer consume.
    result = subprocess.run(
        [
            sys.executable,
            str(EVAL_COMPARE),
            "--json",
            "--include-per-test",
            "--eval-set",
            str(eval_set_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise RuntimeError(f"eval_compare failed: {result.stderr[:300]}")
    return json.loads(result.stdout)


def load_baseline(baseline_path: Path) -> dict | None:
    if not baseline_path.exists():
        return None
    return json.loads(baseline_path.read_text())


def write_baseline(report: dict, baseline_path: Path) -> None:
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    report_with_ts = {
        **report,
        "baseline_written_at": datetime.now().isoformat(timespec="seconds"),
    }
    baseline_path.write_text(json.dumps(report_with_ts, indent=2) + "\n")


def alert_chris(message: str) -> None:
    """Send a regression alert via OpenClaw Telegram direct message.
    Uses `message send` with explicit channel/target (not `agent --deliver`,
    which was failing with 'requires target <chatId>'). Bug fix 2026-04-12.
    """
    try:
        subprocess.run(
            [
                OPENCLAW_BIN,
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                TELEGRAM_CHAT_ID,
                "--account",
                TELEGRAM_ACCOUNT,
                "--message",
                f"[BRAIN EVAL ALERT] {message}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        pass  # alerting must never block the gate


def main() -> int:
    parser = argparse.ArgumentParser(description="eval regression gate")
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=DEFAULT_EVAL_SET,
        help="path to eval set JSON (default: cli/eval_set.json)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="path to baseline JSON (default: cli/eval_baseline.json)",
    )
    parser.add_argument(
        "--track",
        type=str,
        default="default",
        help="track label for persisted reports (e.g. stable | extended). "
        "Default writes legacy eval-report.json; named tracks add suffix.",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="overwrite baseline with the current run",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="max allowed drop in hit_content_pct (default 5.0 percentage points)",
    )
    parser.add_argument(
        "--max-baseline-age-days",
        type=int,
        default=30,
        help="auto-refresh baseline if older than N days AND current is ≥ baseline (default 30)",
    )
    parser.add_argument(
        "--no-heal",
        action="store_true",
        help="suppress self_heal dispatch on regression (use for trend-only tracks)",
    )
    parser.add_argument(
        "--alert-only-above",
        type=float,
        default=0.0,
        help="only alert if current_content drops below this absolute floor "
        "(0 = always alert on relative regression)",
    )
    args = parser.parse_args()

    print(
        f"[eval_gate] running eval at {datetime.now().isoformat(timespec='seconds')} "
        f"track={args.track} set={args.eval_set.name}"
    )
    try:
        report = run_current_eval(args.eval_set)
    except Exception as e:
        print(f"[eval_gate] ERROR running eval: {e}", file=sys.stderr)
        return 2

    _persist_eval_report(report, track=args.track)

    current = report.get("v2", {})
    current_content = float(current.get("hit_content_pct", 0))
    current_source = float(current.get("hit_source_pct", 0))
    print(
        f"[eval_gate] current /recall/v2: " f"hit_content@5={current_content}% hit_source@5={current_source}%"
    )

    if args.update_baseline:
        write_baseline(report, args.baseline)
        print(f"[eval_gate] baseline overwritten at {args.baseline}")
        return 0

    baseline = load_baseline(args.baseline)
    if baseline is None:
        write_baseline(report, args.baseline)
        print(f"[eval_gate] no baseline existed, bootstrapped at {args.baseline}")
        return 0

    baseline_current = baseline.get("v2", {})
    baseline_content = float(baseline_current.get("hit_content_pct", 0))
    baseline_source = float(baseline_current.get("hit_source_pct", 0))
    print(
        f"[eval_gate] baseline /recall/v2: "
        f"hit_content@5={baseline_content}% hit_source@5={baseline_source}%"
    )

    delta_content = current_content - baseline_content
    delta_source = current_source - baseline_source
    print(f"[eval_gate] delta content@5: {delta_content:+.1f}pts, source@5: {delta_source:+.1f}pts")

    regression = delta_content < -args.threshold
    above_floor = current_content >= args.alert_only_above
    if regression and (args.alert_only_above == 0.0 or not above_floor):
        msg = (
            f"REGRESSION[{args.track}]: hit_content@5 dropped {-delta_content:.1f}pts "
            f"(baseline={baseline_content}%, current={current_content}%, "
            f"threshold={args.threshold}pts)"
        )
        print(f"[eval_gate] {msg}", file=sys.stderr)
        alert_chris(msg)

        if not args.no_heal:
            try:
                from self_heal import HealingSignal
                from self_heal import dispatch as heal_dispatch

                heal_dispatch(
                    HealingSignal(
                        source="eval_gate",
                        signal_type="eval_regression",
                        severity="high" if delta_content <= -10 else "medium",
                        metric="hit_content_pct",
                        value=current_content,
                        baseline=baseline_content,
                        target="semantic_memory",
                        context={
                            "delta": delta_content,
                            "threshold": args.threshold,
                            "track": args.track,
                        },
                    )
                )
            except Exception as e:
                print(f"[eval_gate] self_heal dispatch failed: {e}", file=sys.stderr)
        else:
            print("[eval_gate] heal dispatch suppressed (--no-heal)")

        return 1

    # Auto-refresh baseline on passing runs if stale.
    try:
        baseline_ts = baseline.get("baseline_written_at", "")
        if baseline_ts:
            dt_baseline = datetime.fromisoformat(baseline_ts)
            age_days = (datetime.now() - dt_baseline).days
            if age_days > args.max_baseline_age_days and current_content >= baseline_content:
                write_baseline(report, args.baseline)
                print(f"[eval_gate] baseline auto-refreshed (was {age_days}d old, current ≥ baseline)")
    except Exception as e:
        print(f"[eval_gate] baseline age check failed: {e}", file=sys.stderr)

    # Phase N3: also score pending holdout candidates against the live brain
    # and record each outcome into eval_holdout_lifecycle. After 4 passing
    # runs at >=75% ratio, auto_graduate (Sun 7:30) will merge them into the
    # stable set. Best-effort — any failure is logged but does not fail the
    # main eval_gate run.
    try:
        _score_holdout_candidates()
    except Exception as exc:
        print(f"[eval_gate] holdout scoring failed: {exc}", file=sys.stderr)

    print(f"[eval_gate] PASS (within {args.threshold}pts threshold)")
    return 0


def _score_holdout_candidates() -> None:
    """Phase N3: run each pending holdout candidate against /recall/v2 and
    record pass/fail via eval_holdout_promote.record_eval_result().

    Pass rule (mirrors eval_compare's strict content_hit):
      the expected_content substring appears in any of the top-5 retrieved
      contents OR any entry in expected_alternates matches.
    """
    if not PENDING_HOLDOUT.exists():
        return
    try:
        pending = json.loads(PENDING_HOLDOUT.read_text())
    except Exception:
        return
    if not isinstance(pending, list) or not pending:
        return
    if not SECRET_FILE.exists():
        print("[eval_gate] no secret — skipping holdout scoring", file=sys.stderr)
        return
    token = SECRET_FILE.read_text().strip()

    try:
        from eval_holdout_promote import record_eval_result
    except Exception as exc:
        print(f"[eval_gate] record_eval_result unavailable: {exc}", file=sys.stderr)
        return

    import urllib.parse
    import urllib.request

    scored = passed = 0
    for cand in pending:
        if not isinstance(cand, dict):
            continue
        cid = cand.get("id")
        q = cand.get("query") or ""
        expected = (cand.get("expected") or "").strip().lower()
        if not cid or not q or not expected:
            continue
        path = "/recall/v2?" + urllib.parse.urlencode({"q": q, "n": "5"})
        req = urllib.request.Request(
            BRAIN_URL + path,
            headers={"Authorization": f"Bearer {token}", "x-agent": "eval_gate"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode())
        except Exception:
            continue
        results = payload.get("results", [])[:5]
        alternates = [
            (a or "").strip().lower() for a in (cand.get("expected_alternates") or []) if isinstance(a, str)
        ]
        forms = [expected] + [a for a in alternates if a]
        hit = False
        for r in results:
            content = (r.get("content") or "").lower()
            if any(form in content for form in forms):
                hit = True
                break
        record_eval_result(cid, hit)
        scored += 1
        if hit:
            passed += 1
    print(f"[eval_gate] holdout scored: {scored} candidates, {passed} passed")


if __name__ == "__main__":
    sys.exit(main())
