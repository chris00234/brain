#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_sweep.py — one-step iterative sweep driver for /recall/v2 tuning.

Each invocation does exactly ONE iteration of the sweep:
  1. Read state file (init if missing)
  2. If baseline not captured yet: capture it and exit
  3. Pick next (knob, value) from MATRIX
  4. Capture revert hunk (exact source text)
  5. Patch the source file (literal-anchor replacement)
  6. Restart brain-server via launchctl kickstart -k
  7. Poll /health until ready, warm the CE cache
  8. Run eval_compare.py --json, parse v2 scores
  9. KEEP or REVERT based on accuracy/latency decision matrix
 10. Append iteration record to logs/eval_sweep_iterations.jsonl
 11. Advance state, check stopping criteria
 12. Exit 0 so a bash while-loop or ralph-loop can re-fire

Running it in a loop:
  cd /Users/chrischo/server/brain && for i in $(seq 1 25); do
    ./.venv/bin/python cli/eval_sweep.py
    jq -e '.done == true' /tmp/brain_eval_sweep_state.json >/dev/null 2>&1 && break
  done

State file: /tmp/brain_eval_sweep_state.json   (atomic rename on every write)
Iteration log: logs/eval_sweep_iterations.jsonl (append-only)

Safety:
- Every patch is captured as a revert hunk before it's applied.
- If the anchor isn't uniquely present in the target file, the step errors
  out without modifying anything.
- If the brain fails to come back healthy after a restart, the step reverts
  the patch and tries to restart again. If that also fails, the state file
  is marked `brain_unhealthy = true` and the sweep stops.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# Make cli/ importable so we can load the matrix
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_sweep_matrix import MATRIX, knob_count

# ── Paths ──────────────────────────────────────────────────────────────
BRAIN_ROOT = Path("/Users/chrischo/server/brain")
STATE_FILE = Path("/tmp/brain_eval_sweep_state.json")
ITERATIONS_LOG = BRAIN_ROOT / "logs" / "eval_sweep_iterations.jsonl"
EVAL_COMPARE = BRAIN_ROOT / "cli" / "eval_compare.py"
VENV_PY = BRAIN_ROOT / ".venv" / "bin" / "python"
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
LAUNCHD_LABEL = f"gui/{os.getuid()}/ai.openclaw.brain-server"
BRAIN_URL = "http://127.0.0.1:8791"

# Prefer the train split if it exists so we don't tune on the holdout.
EVAL_SET_TRAIN = BRAIN_ROOT / "cli" / "eval_set_train.json"
EVAL_SET_FULL = BRAIN_ROOT / "cli" / "eval_set.json"


def _eval_set_path() -> Path:
    return EVAL_SET_TRAIN if EVAL_SET_TRAIN.exists() else EVAL_SET_FULL


# ── Decision thresholds ────────────────────────────────────────────────
LATENCY_BUDGET_MS = 300  # warm p50
LATENCY_STRETCH_MS = 400  # allow if big accuracy win
KEEP_ACC_MIN = 0.5  # ≥0.5pt avg delta to keep at normal budget
KEEP_ACC_BIG = 1.5  # ≥1.5pt avg delta to keep at stretch budget
NO_GAIN_STREAK_LIMIT = 9  # consecutive KNOBS with no improvement (relaxed — always finish all knobs)
ITER_CAP = 80  # hard cap on single-value measurements
TARGET_SOURCE = 92.0
TARGET_CONTENT = 85.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log_iteration(record: dict) -> None:
    ITERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ITERATIONS_LOG.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── State management ──────────────────────────────────────────────────
def _init_state() -> dict:
    return {
        "created_at": _now_iso(),
        "current_knob": 0,
        "current_value_idx": 0,
        "iteration": 0,
        "baseline_captured": False,
        "baseline": None,  # initial measurement, never changes
        "rolling_baseline": None,  # promoted after each knob's winning value
        "winning_config": {},  # {knob_name: {value, scores, d_acc_avg}}
        "knob_best": None,  # best passing value seen for current knob
        "knob_no_gain": 0,  # consecutive knobs with no improvement
        "done": False,
        "brain_unhealthy": False,
    }


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print("state file corrupt, re-initializing", file=sys.stderr)
            return _init_state()
    return _init_state()


def _save_state(state: dict) -> None:
    _atomic_write_json(STATE_FILE, state)


# ── HTTP helpers ──────────────────────────────────────────────────────
def _bearer() -> str:
    return SECRET_FILE.read_text().strip()


def _http_get(path: str, timeout: float = 10) -> tuple[int, dict]:
    req = urllib.request.Request(
        BRAIN_URL + path,
        headers={"Authorization": f"Bearer {_bearer()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def _brain_healthy() -> bool:
    # /healthz is unauth + uses GET; bypass the bearer header
    try:
        req = urllib.request.Request(BRAIN_URL + "/healthz")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
            return body.get("status") == "ok"
    except Exception:
        return False


def _wait_for_brain(max_wait_s: float = 40) -> bool:
    t0 = time.time()
    while time.time() - t0 < max_wait_s:
        if _brain_healthy():
            return True
        time.sleep(0.8)
    return False


def _warm_cache(n_queries: int = 5) -> None:
    """Warm the brain so measurements are reproducible.

    The CE model (BGE-reranker-base) loads asynchronously at startup via a
    daemon thread. If we measure before the load finishes, the first ~100
    queries race against the warmup and produce noisy results. This helper:

      1. Directly imports cross_encoder_model.warmup() and calls it (blocks
         until the model is loaded into MPS memory).
      2. Sends n_queries /recall/v2 requests so Chroma + Ollama pipelines
         are also warm.

    Both paths are idempotent.
    """
    # Stage 1 — force-load the CE model in this process via the brain's
    # warmup endpoint. We don't have a dedicated HTTP route for it, so we
    # do an extra-long first /recall query with a trivial prompt which
    # guarantees CE is resolved through the full rerank path.
    try:
        req = urllib.request.Request(
            BRAIN_URL + "/recall/v2?" + urllib.parse.urlencode({"q": "warmup probe", "n": "5"}),
            headers={"Authorization": f"Bearer {_bearer()}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # allow model load time
            resp.read()
    except Exception:
        pass

    # Stage 2 — prime Chroma/Ollama on real eval queries
    try:
        cases = json.loads(_eval_set_path().read_text())[:n_queries]
    except Exception:
        return
    for c in cases:
        q = (c.get("query") or "").strip()
        if not q:
            continue
        params = urllib.parse.urlencode({"q": q, "n": "5"})
        _http_get(f"/recall/v2?{params}", timeout=20)


# ── Brain lifecycle ──────────────────────────────────────────────────
def _restart_brain() -> bool:
    try:
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", LAUNCHD_LABEL],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            print(f"launchctl kickstart failed: {r.stderr.strip()}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"launchctl kickstart error: {e}", file=sys.stderr)
        return False
    if not _wait_for_brain(max_wait_s=40):
        print("brain did not return to /health within 40s", file=sys.stderr)
        return False
    _warm_cache(n_queries=5)
    return True


# ── Patch application ────────────────────────────────────────────────
def _apply_patch(file_path: str, old_string: str, new_string: str) -> tuple[bool, str]:
    """Read → verify unique anchor → replace → write. Return (ok, error)."""
    p = Path(file_path)
    if not p.exists():
        return False, f"file not found: {file_path}"
    content = p.read_text()
    count = content.count(old_string)
    if count == 0:
        return False, f"anchor not found in {p.name}"
    if count > 1:
        return False, f"anchor matches {count}x in {p.name} (must be unique)"
    new_content = content.replace(old_string, new_string, 1)
    p.write_text(new_content)
    return True, ""


# ── Eval measurement ─────────────────────────────────────────────────
def _run_eval_compare() -> dict | None:
    """Run eval_compare.py --json on the active eval set (train split if it
    exists). Returns the parsed v2 dict or None on failure."""
    try:
        r = subprocess.run(
            [str(VENV_PY), str(EVAL_COMPARE), "--json", "--eval-set", str(_eval_set_path())],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(BRAIN_ROOT),
        )
        if r.returncode != 0:
            print(f"eval_compare failed: {r.stderr[-500:]}", file=sys.stderr)
            return None
        data = json.loads(r.stdout)
        return data.get("v2")
    except Exception as e:
        print(f"eval_compare exception: {e}", file=sys.stderr)
        return None


def _score_of(v2: dict) -> dict:
    return {
        "source": float(v2.get("hit_source_pct", 0)),
        "content": float(v2.get("hit_content_pct", 0)),
        "latency_ms": int(v2.get("mean_latency_ms", 0)),
        "mean_rank": float(v2.get("mean_rank", 0)),
        "total": int(v2.get("total", 0)),
    }


# ── Decision matrix ─────────────────────────────────────────────────
def _decide(baseline: dict, current: dict) -> tuple[str, dict]:
    """Return (decision, deltas). decision ∈ {'KEEP', 'REVERT'}."""
    ds = current["source"] - baseline["source"]
    dc = current["content"] - baseline["content"]
    dl = current["latency_ms"] - baseline["latency_ms"]
    d_acc = (ds + dc) / 2
    deltas = {
        "d_source": round(ds, 2),
        "d_content": round(dc, 2),
        "d_latency_ms": dl,
        "d_acc_avg": round(d_acc, 2),
    }
    # Gate 1 — normal budget, moderate win
    if d_acc >= KEEP_ACC_MIN and current["latency_ms"] <= LATENCY_BUDGET_MS:
        return "KEEP", deltas
    # Gate 2 — stretch budget, big win
    if d_acc >= KEEP_ACC_BIG and current["latency_ms"] <= LATENCY_STRETCH_MS:
        return "KEEP", deltas
    return "REVERT", deltas


# ── Main step ────────────────────────────────────────────────────────
def run_one_step() -> int:
    state = _load_state()

    if state.get("done"):
        print("EVAL_SWEEP_DONE (state already finalized)")
        return 0

    if state.get("brain_unhealthy"):
        print("ABORTED: brain is marked unhealthy. Fix manually before continuing.", file=sys.stderr)
        return 2

    # Step 0 — capture baseline before anything else
    if not state["baseline_captured"]:
        print(f"[iter 0] capturing baseline on n={_count_eval_set()} set...")
        if not _brain_healthy():
            print("brain not healthy at start; aborting", file=sys.stderr)
            state["brain_unhealthy"] = True
            _save_state(state)
            return 2
        _warm_cache(5)
        v2 = _run_eval_compare()
        if not v2:
            print("baseline eval_compare failed", file=sys.stderr)
            return 2
        baseline = _score_of(v2)
        state["baseline"] = baseline
        state["rolling_baseline"] = dict(baseline)
        state["baseline_captured"] = True
        _save_state(state)
        print(
            f"[iter 0] baseline: source={baseline['source']:.1f}% "
            f"content={baseline['content']:.1f}% "
            f"latency={baseline['latency_ms']}ms "
            f"(n={baseline['total']})"
        )
        return 0

    # Step N — either finalize current knob or test the next value
    knob_idx = state["current_knob"]

    if knob_idx >= knob_count():
        state["done"] = True
        _save_state(state)
        _print_final_summary(state)
        return 0

    knob = MATRIX[knob_idx]
    val_idx = state["current_value_idx"]

    # ── If all values in this knob have been tested, finalize it ──
    if val_idx >= len(knob["values"]):
        return _finalize_knob(state, knob, knob_idx)

    # ── Otherwise, test one value ──
    state["iteration"] += 1
    iter_no = state["iteration"]
    baseline = state["rolling_baseline"]
    value = knob["values"][val_idx]

    old_string = knob["anchor_tpl"].format(**knob["baseline"])
    new_string = knob["anchor_tpl"].format(**value)

    print(f"\n{'=' * 70}")
    print(f"[iter {iter_no}] knob={knob['knob']} value={val_idx} {value}")
    print(f"  file: {Path(knob['file']).name}")
    print(f"  hypothesis: {knob['hypothesis']}")
    print(
        f"  rolling baseline: source={baseline['source']:.1f}% content={baseline['content']:.1f}% lat={baseline['latency_ms']}ms"
    )
    print(f"{'=' * 70}")

    pre_sha = _sha256(Path(knob["file"]))

    ok, err = _apply_patch(knob["file"], old_string, new_string)
    if not ok:
        print(f"  PATCH FAILED: {err}", file=sys.stderr)
        _log_iteration(
            {
                "ts": _now_iso(),
                "iter": iter_no,
                "knob": knob["knob"],
                "value_idx": val_idx,
                "value": value,
                "decision": "PATCH_FAILED",
                "error": err,
            }
        )
        state["current_value_idx"] += 1
        _save_state(state)
        return 0

    print("  restarting brain...")
    if not _restart_brain():
        print("  brain failed to come back; reverting + retry", file=sys.stderr)
        _apply_patch(knob["file"], new_string, old_string)
        if not _restart_brain():
            state["brain_unhealthy"] = True
            _save_state(state)
            _log_iteration(
                {
                    "ts": _now_iso(),
                    "iter": iter_no,
                    "knob": knob["knob"],
                    "value_idx": val_idx,
                    "decision": "BRAIN_UNHEALTHY",
                }
            )
            return 2
        _log_iteration(
            {
                "ts": _now_iso(),
                "iter": iter_no,
                "knob": knob["knob"],
                "value_idx": val_idx,
                "value": value,
                "decision": "REVERTED_AFTER_RESTART_FAIL",
            }
        )
        state["current_value_idx"] += 1
        _save_state(state)
        return 0

    print("  running eval_compare...")
    v2 = _run_eval_compare()

    # Always revert file after the measurement; we'll re-apply the best
    # value at knob-finalize. The brain still has the patched code loaded,
    # but that's fine — the file just needs to be in the right state for
    # the next patch to anchor correctly.
    _apply_patch(knob["file"], new_string, old_string)

    if not v2:
        print("  eval_compare failed", file=sys.stderr)
        _log_iteration(
            {
                "ts": _now_iso(),
                "iter": iter_no,
                "knob": knob["knob"],
                "value_idx": val_idx,
                "value": value,
                "decision": "EVAL_FAILED",
            }
        )
        state["current_value_idx"] += 1
        _save_state(state)
        return 0

    current = _score_of(v2)
    decision, deltas = _decide(baseline, current)
    passed = decision == "KEEP"
    print(
        f"  result: source={current['source']:.1f}% content={current['content']:.1f}% "
        f"lat={current['latency_ms']}ms  Δacc={deltas['d_acc_avg']:+.2f}pt  Δlat={deltas['d_latency_ms']:+d}ms"
    )
    print(f"  gate: {'PASS' if passed else 'FAIL'}")

    # Track the best passing value for this knob
    candidate = {
        "value": value,
        "value_idx": val_idx,
        "scores": current,
        "deltas": deltas,
        "new_string": new_string,
        "old_string": old_string,
    }
    prev_best = state.get("knob_best")
    if passed:
        if prev_best is None or deltas["d_acc_avg"] > prev_best["deltas"]["d_acc_avg"]:
            state["knob_best"] = candidate
            marker = "NEW KNOB BEST"
        else:
            marker = "pass, but not best"
    else:
        marker = "fail"
    print(f"  → {marker}")

    _log_iteration(
        {
            "ts": _now_iso(),
            "iter": iter_no,
            "knob": knob["knob"],
            "value_idx": val_idx,
            "value": value,
            "baseline": baseline,
            "current": current,
            "deltas": deltas,
            "decision": decision,
            "note": marker,
            "pre_sha": pre_sha,
        }
    )

    state["current_value_idx"] += 1
    if state["iteration"] >= ITER_CAP:
        print(f"\nITERATION CAP ({ITER_CAP}) HIT — finalizing and stopping")
        state["done"] = True
    _save_state(state)

    if state["done"]:
        _finalize_knob(state, knob, knob_idx)
        state["done"] = True
        _save_state(state)
        _print_final_summary(state)
    return 0


def _finalize_knob(state: dict, knob: dict, knob_idx: int) -> int:
    """Apply the best passing value (if any) for this knob, then advance."""
    best = state.get("knob_best")
    print(f"\n── finalizing knob {knob_idx}: {knob['knob']} ──")
    if best is None:
        print("  no passing value — knob skipped, no changes")
        state["knob_no_gain"] += 1
    else:
        print(
            f"  applying best: value[{best['value_idx']}] = {best['value']} "
            f"(Δacc={best['deltas']['d_acc_avg']:+.2f}pt)"
        )
        old_string = best["old_string"]
        new_string = best["new_string"]
        ok, err = _apply_patch(knob["file"], old_string, new_string)
        if not ok:
            print(f"  FINALIZE PATCH FAILED: {err}", file=sys.stderr)
            state["brain_unhealthy"] = True
            _save_state(state)
            return 2
        if not _restart_brain():
            print("  brain failed to come back after finalize", file=sys.stderr)
            state["brain_unhealthy"] = True
            _save_state(state)
            return 2
        state["rolling_baseline"] = dict(best["scores"])
        state["winning_config"][knob["knob"]] = {
            "value": best["value"],
            "value_idx": best["value_idx"],
            "scores": best["scores"],
            "deltas": best["deltas"],
            "new_string": new_string,
        }
        state["knob_no_gain"] = 0
        print(
            f"  new rolling baseline: source={best['scores']['source']:.1f}% "
            f"content={best['scores']['content']:.1f}% "
            f"lat={best['scores']['latency_ms']}ms"
        )

    # Advance to next knob
    state["current_knob"] += 1
    state["current_value_idx"] = 0
    state["knob_best"] = None

    # Stopping criteria
    rb = state["rolling_baseline"]
    if rb["source"] >= TARGET_SOURCE and rb["content"] >= TARGET_CONTENT:
        print(f"\nSCORE TARGET HIT: source={rb['source']:.1f}% content={rb['content']:.1f}%")
        state["done"] = True
    elif state["knob_no_gain"] >= NO_GAIN_STREAK_LIMIT:
        print(f"\nKNOB NO-GAIN STREAK LIMIT ({NO_GAIN_STREAK_LIMIT}) — stopping")
        state["done"] = True
    elif state["current_knob"] >= knob_count():
        state["done"] = True

    _save_state(state)
    if state["done"]:
        _print_final_summary(state)
    return 0


def _count_eval_set() -> int:
    try:
        cases = json.loads(_eval_set_path().read_text())
        return len(cases)
    except Exception:
        return 0


def _print_final_summary(state: dict) -> None:
    print("\n" + "=" * 70)
    print("EVAL_SWEEP_DONE")
    print("=" * 70)
    b = state.get("baseline") or {}
    r = state.get("rolling_baseline") or {}
    print(
        f"baseline    : source={b.get('source', 0):.1f}%  content={b.get('content', 0):.1f}%  latency={b.get('latency_ms', 0)}ms"
    )
    print(
        f"final       : source={r.get('source', 0):.1f}%  content={r.get('content', 0):.1f}%  latency={r.get('latency_ms', 0)}ms"
    )
    ds = r.get("source", 0) - b.get("source", 0)
    dc = r.get("content", 0) - b.get("content", 0)
    print(f"delta       : source={ds:+.1f}pt  content={dc:+.1f}pt")
    print(f"iterations  : {state.get('iteration', 0)}")
    print(f"winning_cfg : {len(state.get('winning_config', {}))} knob(s) improved")
    for knob_name, cfg in state.get("winning_config", {}).items():
        print(f"  - {knob_name}: {cfg['value']}")
    print(f"\niteration log: {ITERATIONS_LOG}")
    print(f"state file:    {STATE_FILE}")


if __name__ == "__main__":
    sys.exit(run_one_step())
