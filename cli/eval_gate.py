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
import contextlib
import json
import re
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


def _content_metric_value(v2: dict, metric: str) -> float:
    strict = float(v2.get("hit_content_pct", 0))
    if metric == "loose":
        return float(v2.get("hit_content_loose_pct", strict))
    return strict


def _failure_breakdown(per_test: list[dict]) -> dict:
    """Summarize eval failures into actionable buckets.

    `content_only_failed` usually means the expected source/provenance is
    found, but the eval phrase is stale or too literal. `both_failed` points
    at real retrieval/source misses.
    """
    if not per_test:
        return {}
    content_failed = [r for r in per_test if not r.get("hit_content_loose")]
    source_failed = [r for r in per_test if not r.get("hit_source")]
    both_failed = [r for r in per_test if not r.get("hit_content_loose") and not r.get("hit_source")]
    content_only_failed = [r for r in per_test if not r.get("hit_content_loose") and r.get("hit_source")]
    source_only_failed = [r for r in per_test if r.get("hit_content_loose") and not r.get("hit_source")]

    def sample(rows: list[dict], limit: int = 10) -> list[dict]:
        out = []
        for row in rows[:limit]:
            out.append(
                {
                    "query": row.get("query", ""),
                    "expected_source": row.get("expected_source", ""),
                    "expected_content": row.get("expected_content", ""),
                    "top_sources": row.get("top_sources", [])[:5],
                    "rank": row.get("rank", 0),
                }
            )
        return out

    return {
        "total": len(per_test),
        "content_failed": len(content_failed),
        "source_failed": len(source_failed),
        "both_failed": len(both_failed),
        "content_only_failed": len(content_only_failed),
        "source_only_failed": len(source_only_failed),
        "samples": {
            "both_failed": sample(both_failed),
            "content_only_failed": sample(content_only_failed),
            "source_only_failed": sample(source_only_failed),
        },
    }


def _failure_analysis(per_test: list[dict], *, slow_ms: int = 1000) -> dict:
    """Classify failed eval rows into deterministic, fix-oriented causes."""
    buckets: dict[str, dict] = {}
    secondary_flags: dict[str, int] = {}
    failed_rows = [row for row in per_test if _row_failed(row)]
    for row in failed_rows:
        bucket, reason = _classify_failure_row(row)
        _bucket_add(buckets, bucket, row, reason)
        if _safe_float(row.get("latency_ms"), 0.0) > slow_ms:
            secondary_flags["slow_failure"] = secondary_flags.get("slow_failure", 0) + 1

    return {
        "version": 1,
        "total": len(per_test),
        "failed": len(failed_rows),
        "buckets": buckets,
        "secondary_flags": dict(sorted(secondary_flags.items())),
    }


def _row_failed(row: dict) -> bool:
    return not bool(row.get("hit_content_loose")) or not bool(row.get("hit_source"))


def _classify_failure_row(row: dict) -> tuple[str, str]:
    hit_content = bool(row.get("hit_content_loose"))
    hit_source = bool(row.get("hit_source"))
    expected_source = str(row.get("expected_source") or "")
    top_sources = [str(src) for src in (row.get("top_sources") or []) if src]
    best_source_overlap = max((_source_overlap(expected_source, src) for src in top_sources), default=0.0)
    expected_is_archived = _looks_archived_or_superseded(expected_source)
    top_has_canonical = any("canonical" in src.lower() for src in top_sources)

    if not hit_content and hit_source:
        return (
            "stale_expected_content",
            "expected source was found but expected phrase/alternate did not appear",
        )
    if hit_content and not hit_source:
        return (
            "source_alias_or_successor",
            "expected content was found under a different source/provenance",
        )
    if expected_is_archived and top_has_canonical:
        return (
            "canonical_consolidation_gap",
            "archived expected source now resolves toward canonical/successor material",
        )
    if best_source_overlap >= 0.22:
        return (
            "source_moved_or_archived",
            f"top source slug overlaps expected source ({best_source_overlap:.2f})",
        )
    return (
        "retrieval_miss",
        "neither expected content nor expected source was found in top results",
    )


def _bucket_add(buckets: dict[str, dict], bucket: str, row: dict, reason: str, *, sample_limit: int = 10) -> None:
    item = buckets.setdefault(bucket, {"count": 0, "samples": []})
    item["count"] += 1
    if len(item["samples"]) < sample_limit:
        item["samples"].append(
            {
                "query": row.get("query", ""),
                "expected_source": row.get("expected_source", ""),
                "expected_content": row.get("expected_content", ""),
                "top_sources": list(row.get("top_sources") or [])[:5],
                "rank": row.get("rank", 0),
                "reason": reason,
            }
        )


def _source_overlap(expected_source: str, actual_source: str) -> float:
    expected = _source_tokens(expected_source)
    actual = _source_tokens(actual_source)
    if not expected or not actual:
        return 0.0
    return len(expected & actual) / max(1, len(expected | actual))


def _source_tokens(source: str) -> set[str]:
    tail = source.replace("\\", "/").split("/")[-1]
    tail = re.sub(r"\.[a-z0-9]{1,8}$", "", tail.lower())
    return {tok for tok in re.findall(r"[a-z0-9가-힣]{3,}", tail) if tok not in _SOURCE_STOPWORDS}


def _looks_archived_or_superseded(source: str) -> bool:
    lowered = source.lower()
    return any(marker in lowered for marker in ("archived", "archive", "superseded", "obsolete", "deprecated"))


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_SOURCE_STOPWORDS = {
    "and",
    "for",
    "from",
    "the",
    "with",
    "this",
    "that",
    "chris",
    "canonical",
    "archived",
    "archive",
}


def _persist_eval_report(report: dict, track: str = "default", content_metric: str = "strict") -> None:
    """Write eval-report.json + append eval-history.jsonl so Brain UI stays current.

    `track` lets the two-track gate persist separate per-track histories without
    clobbering the legacy single-file UI integration. The default track keeps
    writing to the original paths; named tracks write to *_<track>.{json,jsonl}.
    """
    logs = BRAIN_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    v2 = report.get("v2", {})
    total = int(v2.get("total", report.get("cases", 0)))
    strict_pct = float(v2.get("hit_content_pct", 0))
    loose_pct = float(v2.get("hit_content_loose_pct", strict_pct))
    content_pct = _content_metric_value(v2, content_metric)
    source_pct = float(v2.get("hit_source_pct", 0))
    passed = round(total * content_pct / 100) if total else 0
    failed = total - passed
    per_test = list(v2.get("per_test") or [])
    failure_breakdown = _failure_breakdown(per_test)
    failure_analysis = _failure_analysis(per_test)

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
                "content_metric": content_metric,
                "source_accuracy": round(source_pct, 1),
                "slow_count": 0,
                "v2": v2,
                "failure_breakdown": failure_breakdown,
                "failure_analysis": failure_analysis,
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
                    "content_metric": content_metric,
                    "hit_content_pct": round(strict_pct, 1),
                    "hit_content_strict_pct": round(strict_pct, 1),
                    "hit_content_loose_pct": round(loose_pct, 1),
                    "selected_content_pct": round(content_pct, 1),
                    "hit_source_pct": round(source_pct, 1),
                    "source_accuracy": round(source_pct, 1),
                    "slow_count": 0,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def run_current_eval(eval_set_path: Path) -> dict:
    """Run eval_compare.py --json --eval-set <path> and return the parsed report.

    Timeout scales with eval set size: ~500ms per case x 2 endpoints + margin.
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
    with contextlib.suppress(Exception):
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
        "--source-threshold",
        type=float,
        default=10.0,
        help="max allowed drop in hit_source_pct (default 10.0 percentage points)",
    )
    parser.add_argument(
        "--content-metric",
        choices=["strict", "loose"],
        default="strict",
        help="content metric used for regression gating and persisted accuracy "
        "(strict = substring, loose = 75%% token overlap). Default: strict.",
    )
    parser.add_argument(
        "--min-source",
        type=float,
        default=0.0,
        help="absolute hit_source_pct floor (0 = disabled)",
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

    _persist_eval_report(report, track=args.track, content_metric=args.content_metric)

    current = report.get("v2", {})
    current_content = _content_metric_value(current, args.content_metric)
    current_source = float(current.get("hit_source_pct", 0))
    metric_label = "hit_content_loose@5" if args.content_metric == "loose" else "hit_content@5"
    print(
        f"[eval_gate] current /recall/v2: "
        f"{metric_label}={current_content}% hit_source@5={current_source}%"
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
    baseline_content = _content_metric_value(baseline_current, args.content_metric)
    baseline_source = float(baseline_current.get("hit_source_pct", 0))
    print(
        f"[eval_gate] baseline /recall/v2: "
        f"{metric_label}={baseline_content}% hit_source@5={baseline_source}%"
    )

    delta_content = current_content - baseline_content
    delta_source = current_source - baseline_source
    print(f"[eval_gate] delta {metric_label}: {delta_content:+.1f}pts, source@5: {delta_source:+.1f}pts")

    content_regression = delta_content < -args.threshold
    content_floor_allows_alert = args.alert_only_above == 0.0 or current_content < args.alert_only_above
    source_regression = baseline_source > 0 and delta_source < -args.source_threshold
    source_floor_breach = args.min_source > 0.0 and current_source < args.min_source
    if (content_regression and content_floor_allows_alert) or source_regression or source_floor_breach:
        failing_metric = (
            "hit_source_pct"
            if (source_regression or source_floor_breach)
            else ("hit_content_loose_pct" if args.content_metric == "loose" else "hit_content_pct")
        )
        current_value = current_source if failing_metric == "hit_source_pct" else current_content
        baseline_value = baseline_source if failing_metric == "hit_source_pct" else baseline_content
        delta_value = delta_source if failing_metric == "hit_source_pct" else delta_content
        threshold = args.source_threshold if failing_metric == "hit_source_pct" else args.threshold
        failing_label = "hit_source@5" if failing_metric == "hit_source_pct" else metric_label
        if source_floor_breach and not source_regression:
            msg = (
                f"REGRESSION[{args.track}]: {failing_label} breached floor "
                f"(current={current_value}%, min_source={args.min_source}%)"
            )
        else:
            msg = (
                f"REGRESSION[{args.track}]: {failing_label} dropped {-delta_value:.1f}pts "
                f"(baseline={baseline_value}%, current={current_value}%, "
                f"threshold={threshold}pts)"
            )
            if source_floor_breach:
                msg += f"; source floor breached (min_source={args.min_source}%)"
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
                        severity="high" if delta_value <= -10 else "medium",
                        metric=failing_metric,
                        value=current_value,
                        baseline=baseline_value,
                        target="semantic_memory",
                        context={
                            "delta": delta_value,
                            "threshold": threshold,
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
        req = urllib.request.Request(  # noqa: S310
            BRAIN_URL + path,
            headers={"Authorization": f"Bearer {token}", "x-agent": "eval_gate"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            print(f"[eval_gate] holdout candidate recall failed: {exc}", file=sys.stderr)
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
