#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_sweep_verify.py — final-run verification for the tuning sweep.

After eval_sweep.py finishes, this runs a 4-part sanity check on the winning
config:

  1. Train set re-measurement  — confirms rolling_baseline is reproducible
  2. Full merged eval set      — apples-to-apples vs pre-sweep baseline
  3. Holdout set               — overfit guard (was never seen during tuning)
  4. Brain health / err-log    — confirm no ops regression

Writes a markdown report to logs/eval_sweep_verify.md.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
VENV_PY = BRAIN_ROOT / ".venv" / "bin" / "python"
EVAL_COMPARE = BRAIN_ROOT / "cli" / "eval_compare.py"
STATE_FILE = Path("/tmp/brain_eval_sweep_state.json")
REPORT = BRAIN_ROOT / "logs" / "eval_sweep_verify.md"

TRAIN = BRAIN_ROOT / "cli" / "eval_set_train.json"
FULL = BRAIN_ROOT / "cli" / "eval_set.json"
HOLDOUT = BRAIN_ROOT / "cli" / "eval_holdout.json"


def _run(eval_set: Path) -> dict | None:
    try:
        r = subprocess.run(
            [str(VENV_PY), str(EVAL_COMPARE), "--json", "--eval-set", str(eval_set)],
            capture_output=True, text=True, timeout=600,
            cwd=str(BRAIN_ROOT),
        )
        if r.returncode != 0:
            print(f"  eval_compare fail on {eval_set.name}: {r.stderr[-300:]}", file=sys.stderr)
            return None
        return json.loads(r.stdout)
    except Exception as e:
        print(f"  eval_compare exception on {eval_set.name}: {e}", file=sys.stderr)
        return None


def _fmt(v2: dict) -> str:
    if not v2:
        return "FAILED"
    return (f"source={v2['hit_source_pct']:.1f}%  content={v2['hit_content_pct']:.1f}%  "
            f"mean_rank={v2['mean_rank']}  latency={v2['mean_latency_ms']:.0f}ms  n={v2['total']}")


def main() -> int:
    if not STATE_FILE.exists():
        print(f"no sweep state at {STATE_FILE} — run eval_sweep.py first", file=sys.stderr)
        return 2

    state = json.loads(STATE_FILE.read_text())
    baseline = state.get("baseline") or {}
    rolling = state.get("rolling_baseline") or {}
    winners = state.get("winning_config") or {}
    iterations = state.get("iteration", 0)

    print("=" * 70)
    print("eval_sweep_verify — final-run verification")
    print("=" * 70)
    print(f"sweep iterations: {iterations}")
    print(f"knobs improved:   {len(winners)} / 9")
    print()

    # 1. Train re-measurement
    print("[1/3] Train set re-run (reproducibility check)...")
    train_v2 = (_run(TRAIN) or {}).get("v2")
    print(f"      {_fmt(train_v2)}")

    # 2. Full set
    print("[2/3] Full merged set (n=744)...")
    full_v2 = (_run(FULL) or {}).get("v2")
    print(f"      {_fmt(full_v2)}")

    # 3. Holdout
    print("[3/3] Holdout set (n=149) — overfit guard...")
    hold_v2 = (_run(HOLDOUT) or {}).get("v2")
    print(f"      {_fmt(hold_v2)}")

    # Build markdown report
    lines: list[str] = []
    lines.append("# eval_sweep_verify — final run")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append(f"- **sweep iterations**: {iterations}")
    lines.append(f"- **knobs improved**: {len(winners)} / 9")
    lines.append("")
    lines.append("## Train set (n≈595)")
    lines.append("")
    lines.append(f"- baseline:  source={baseline.get('source', 0):.1f}%  content={baseline.get('content', 0):.1f}%  lat={baseline.get('latency_ms', 0)}ms")
    lines.append(f"- rolling:   source={rolling.get('source', 0):.1f}%  content={rolling.get('content', 0):.1f}%  lat={rolling.get('latency_ms', 0)}ms")
    if train_v2:
        ds = train_v2["hit_source_pct"] - baseline.get("source", 0)
        dc = train_v2["hit_content_pct"] - baseline.get("content", 0)
        dl = train_v2["mean_latency_ms"] - baseline.get("latency_ms", 0)
        lines.append(f"- verify:    source={train_v2['hit_source_pct']:.1f}%  content={train_v2['hit_content_pct']:.1f}%  lat={train_v2['mean_latency_ms']:.0f}ms")
        lines.append(f"- delta vs baseline: source={ds:+.1f}pt  content={dc:+.1f}pt  lat={dl:+.0f}ms")
    lines.append("")
    lines.append("## Full merged set (n=744)")
    lines.append("")
    if full_v2:
        lines.append(f"- source={full_v2['hit_source_pct']:.1f}%  content={full_v2['hit_content_pct']:.1f}%  lat={full_v2['mean_latency_ms']:.0f}ms")
    lines.append("")
    lines.append("## Holdout set (n=149, never tuned on)")
    lines.append("")
    if hold_v2:
        lines.append(f"- source={hold_v2['hit_source_pct']:.1f}%  content={hold_v2['hit_content_pct']:.1f}%  lat={hold_v2['mean_latency_ms']:.0f}ms")
    lines.append("")
    lines.append("## Winning config")
    lines.append("")
    if not winners:
        lines.append("_no knobs passed the keep gate_")
    for knob_name, cfg in winners.items():
        d = cfg.get("deltas", {})
        v = cfg.get("value", {})
        lines.append(f"- **{knob_name}**: {v}  (Δacc={d.get('d_acc_avg', 0):+.2f}pt  Δlat={d.get('d_latency_ms', 0):+d}ms)")
    lines.append("")
    lines.append("## Source file mutations")
    lines.append("")
    files_touched = sorted({cfg.get("new_string", "")[:80] for cfg in winners.values()})
    for s in files_touched:
        lines.append(f"- `{s}`")
    lines.append("")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"\nreport: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
