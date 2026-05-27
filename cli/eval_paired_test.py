#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_paired_test.py — test coupled (fanout, rerank_window) combinations.

The chroma_fanout and rerank_window knobs are NOT independent: widening the
Chroma retrieval pool is pointless if the reranker can only see `limit * 2 = 10`
items anyway. They must be tuned together.

This runs a grid of (fanout, rerank_window) combinations and reports which
one maximizes content@5 hit rate against the current rolling baseline state.

The CURRENT rolling baseline (as of knob 4 finalize in the sweep) is:
  - ce_blend_ratio: 0.2/0.8
  - semantic_boost: 0.5/0.65
  - other knobs: default
  - expected baseline: source 80.7% / content 71.6% / 290ms

Protocol per combination:
  1. Patch both files (fanout + rerank_window)
  2. Restart brain
  3. Warm cache (CE + 5 queries)
  4. Run eval_compare --json on train set
  5. Revert BOTH patches
  6. Log to paired_test_log.jsonl

Runs all combinations, then prints summary table + best combo.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
VENV_PY = BRAIN_ROOT / ".venv" / "bin" / "python"
EVAL_COMPARE = BRAIN_ROOT / "cli" / "eval_compare.py"
TRAIN = BRAIN_ROOT / "cli" / "eval_set_train.json"
LOG = BRAIN_ROOT / "logs" / "eval_paired_test.jsonl"
LAUNCHD_LABEL = "gui/501/ai.brain.server"
BRAIN_URL = "http://127.0.0.1:8791"

SEARCH_UNIFIED = BRAIN_ROOT / "brain_core" / "search_unified.py"

# Anchor templates (match existing file)
FANOUT_TPL = (
    "        raw_results = search_rag(query, limit * {v}, where=local_where or None, collections=collections)"
)
RERANK_TPL = "        unique = _rerank(relevance_query, unique, top_k=limit * {v})"

# (fanout_multiplier, rerank_window_multiplier)
GRID = [
    (4, 4),
    (6, 6),
    (10, 10),
    (8, 4),  # wide fanout, narrow rerank
    (4, 10),  # narrow fanout, wide rerank
    (15, 15),
    (6, 10),
    (10, 6),
]


def _bearer() -> str:
    return Path("/Users/chrischo/.brain/credentials/.personal_webhook_secret").read_text().strip()


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(BRAIN_URL + "/healthz", timeout=5) as r:
            return json.loads(r.read().decode()).get("status") == "ok"
    except Exception:
        return False


def _wait_healthy(max_s: float = 40) -> bool:
    t0 = time.time()
    while time.time() - t0 < max_s:
        if _healthy():
            return True
        time.sleep(0.8)
    return False


def _warm() -> None:
    try:
        req = urllib.request.Request(
            BRAIN_URL + "/recall/v2?" + urllib.parse.urlencode({"q": "warmup probe", "n": "5"}),
            headers={"Authorization": f"Bearer {_bearer()}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except Exception:
        pass
    try:
        cases = json.loads(TRAIN.read_text())[:5]
    except Exception:
        return
    for c in cases:
        q = (c.get("query") or "").strip()
        if not q:
            continue
        try:
            req = urllib.request.Request(
                BRAIN_URL + "/recall/v2?" + urllib.parse.urlencode({"q": q, "n": "5"}),
                headers={"Authorization": f"Bearer {_bearer()}"},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except Exception:
            pass


def _restart() -> bool:
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", LAUNCHD_LABEL], capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        print(f"  restart failed: {e}", file=sys.stderr)
        return False
    if not _wait_healthy(40):
        return False
    _warm()
    return True


def _patch(old: str, new: str) -> bool:
    content = SEARCH_UNIFIED.read_text()
    if content.count(old) != 1:
        print(f"  anchor not unique ({content.count(old)}x): {old[:60]}", file=sys.stderr)
        return False
    SEARCH_UNIFIED.write_text(content.replace(old, new, 1))
    return True


def _run_eval() -> dict | None:
    r = subprocess.run(
        [str(VENV_PY), str(EVAL_COMPARE), "--json", "--eval-set", str(TRAIN)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(BRAIN_ROOT),
    )
    if r.returncode != 0:
        print(f"  eval failed: {r.stderr[-300:]}", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout).get("v2")
    except Exception:
        return None


def main() -> int:
    print(f"eval_paired_test — {len(GRID)} combinations on n=595 train set")
    print("current state: ce_blend 0.2/0.8 + semantic_boost 0.5/0.65 + (fanout, rerank_window) grid")
    print()

    baseline_line_fan = FANOUT_TPL.format(v="2")
    baseline_line_rer = RERANK_TPL.format(v="2")

    # Capture rolling baseline first (current state)
    print("[baseline] measuring current rolling state (fanout=2, rerank=2)...")
    _warm()
    baseline = _run_eval()
    if not baseline:
        print("baseline measurement failed", file=sys.stderr)
        return 2
    print(
        f"  baseline: source={baseline['hit_source_pct']}% content={baseline['hit_content_pct']}% "
        f"loose={baseline.get('hit_content_loose_pct', 0)}% lat={baseline['mean_latency_ms']}ms"
    )

    LOG.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, (fan, rer) in enumerate(GRID, 1):
        new_fan = FANOUT_TPL.format(v=str(fan))
        new_rer = RERANK_TPL.format(v=str(rer))

        print(f"\n[{i}/{len(GRID)}] fanout={fan}  rerank_window={rer}")

        # Patch both
        if not _patch(baseline_line_fan, new_fan):
            continue
        if not _patch(baseline_line_rer, new_rer):
            _patch(new_fan, baseline_line_fan)  # revert fan first
            continue

        if not _restart():
            _patch(new_fan, baseline_line_fan)
            _patch(new_rer, baseline_line_rer)
            continue

        v2 = _run_eval()

        # Revert
        _patch(new_fan, baseline_line_fan)
        _patch(new_rer, baseline_line_rer)

        if not v2:
            continue

        ds = v2["hit_source_pct"] - baseline["hit_source_pct"]
        dc = v2["hit_content_pct"] - baseline["hit_content_pct"]
        dl = v2["mean_latency_ms"] - baseline["mean_latency_ms"]
        d_acc = (ds + dc) / 2
        print(
            f"  result: source={v2['hit_source_pct']}% content={v2['hit_content_pct']}% "
            f"loose={v2.get('hit_content_loose_pct', 0)}% lat={v2['mean_latency_ms']}ms"
        )
        print(f"  delta:  Δsrc={ds:+.1f}pt Δcon={dc:+.1f}pt Δlat={dl:+.0f}ms Δacc={d_acc:+.2f}pt")

        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "fanout": fan,
            "rerank_window": rer,
            "baseline": baseline,
            "current": v2,
            "d_source": ds,
            "d_content": dc,
            "d_latency": dl,
            "d_acc_avg": d_acc,
        }
        results.append(record)
        with LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")

    # Restart once more to return to baseline (ensures brain has baseline code loaded)
    _restart()

    # Summary
    print("\n" + "=" * 80)
    print("PAIRED TEST SUMMARY")
    print("=" * 80)
    print(f"{'fanout':>7} {'rerank':>7} {'source':>8} {'content':>8} {'loose':>7} {'lat':>6} {'Δacc':>7}")
    for r in sorted(results, key=lambda x: x["d_acc_avg"], reverse=True):
        c = r["current"]
        print(
            f"{r['fanout']:>7} {r['rerank_window']:>7} "
            f"{c['hit_source_pct']:>7}% {c['hit_content_pct']:>7}% {c.get('hit_content_loose_pct', 0):>6}% "
            f"{c['mean_latency_ms']:>5}ms {r['d_acc_avg']:>+6.2f}pt"
        )
    if results:
        best = max(results, key=lambda x: x["d_acc_avg"])
        print(
            f"\nbest: fanout={best['fanout']} rerank_window={best['rerank_window']} → Δacc={best['d_acc_avg']:+.2f}pt"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
