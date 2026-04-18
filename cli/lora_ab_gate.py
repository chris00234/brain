#!/Users/chrischo/server/brain/.venv/bin/python
"""cli/lora_ab_gate.py - Phase 7 LoRA adapter A/B gate + deploy.

Closes the self-learning loop:
  feedback → training pairs → fine-tune → A/B gate → deploy → next eval cycle

Runs the 744-query stable+extended eval against both:
  - the base embedder (current)
  - the candidate LoRA adapter (latest training run)

Promotion criteria:
  delta_content_pct >= 2.0 AND worst_per_query_regression <= 5.0

On promotion: symlink logs/training/lora_active → candidate path
              + bump config.ACTIVE_EMBED_MODEL
              + queue async re-embedding of semantic_memory collection

On rejection: archive the candidate to logs/training/rejects/<ts>/

Safe defaults:
  --dry-run             never promote, just print
  --candidate <path>    explicit adapter path (default: logs/training/lora_v_candidate)
  --eval-set <path>     default: cli/eval_set.json (full 744)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
EVAL_COMPARE = BRAIN_ROOT / "cli" / "eval_compare.py"
TRAINING_DIR = BRAIN_ROOT / "logs" / "training"
LORA_ACTIVE = TRAINING_DIR / "lora_active"
DEFAULT_CANDIDATE = TRAINING_DIR / "lora_v_candidate"
REJECTS_DIR = TRAINING_DIR / "rejects"

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_ACCOUNT = "jenna-bot"


def _switch_brain_adapter(path: str | None) -> dict:
    """POST /admin/embed_adapter on the running brain-server so the eval
    actually exercises the candidate adapter (not just the env var in a
    subprocess that doesn't touch brain-server's in-process embedder).

    2026-04-17 fix: previous impl set BRAIN_EMBED_MODEL in the eval_compare
    subprocess env, but that subprocess calls /recall/v2 via HTTP —
    brain-server itself never saw the override. Delta always = 0.00.
    """
    import urllib.error
    import urllib.request

    secret_file = Path.home() / ".openclaw" / "credentials" / ".personal_webhook_secret"
    secret = secret_file.read_text().strip() if secret_file.exists() else ""
    body = json.dumps({"path": path}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8791/admin/embed_adapter",
        data=body,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"status": "error", "reason": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:200]}


def _run_eval(eval_set: Path, env_overrides: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    n_cases = 500
    with contextlib.suppress(Exception):
        n_cases = len(json.loads(eval_set.read_text()))
    timeout_s = max(600, n_cases * 2 + 180)
    # 2026-04-16 fix: request per-test so we can compute real per-query
    # worst-regression rather than guessing from the aggregate delta.
    result = subprocess.run(
        [
            sys.executable,
            str(EVAL_COMPARE),
            "--json",
            "--include-per-test",
            "--eval-set",
            str(eval_set),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"eval_compare failed: {result.stderr[:300]}")
    return json.loads(result.stdout)


def _per_query_worst_regression(base_report: dict, cand_report: dict) -> tuple[float, int]:
    """Return (max_regression_pts, N_regressed_cases) from paired per_test arrays.

    Each per_test entry has a `hit_content` bool and optional numeric score;
    we score 1.0 / 0.0 per case and compute candidate − base per-aligned-case.
    If per_test is missing from either side, falls back to (+inf, -1) which
    the caller should treat as FAIL (do not promote — we couldn't verify).
    """

    def _pt(report: dict) -> list[dict]:
        return ((report or {}).get("v2") or {}).get("per_test") or []

    base_pt = _pt(base_report)
    cand_pt = _pt(cand_report)
    if not base_pt or not cand_pt:
        return (float("inf"), -1)
    # Align by query string for safety (orders should match but tolerate drift).
    by_query_base = {r.get("query", ""): r for r in base_pt if isinstance(r, dict)}
    worst = 0.0
    regressed = 0
    for cand in cand_pt:
        if not isinstance(cand, dict):
            continue
        base = by_query_base.get(cand.get("query", ""))
        if base is None:
            continue
        # Score each side as 1.0 if strict content hit, else 0.0
        b_score = 1.0 if base.get("hit_content") else 0.0
        c_score = 1.0 if cand.get("hit_content") else 0.0
        delta_pts = (c_score - b_score) * 100.0
        if delta_pts < 0:
            regressed += 1
            if -delta_pts > worst:
                worst = -delta_pts
    return (worst, regressed)


def _alert(title: str, body: str) -> None:
    if not Path(OPENCLAW_BIN).exists():
        print(f"[skip alert] openclaw not at {OPENCLAW_BIN}")
        return
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
                f"[BRAIN LoRA A/B] {title}\n{body}",
            ],
            timeout=20,
            capture_output=True,
        )


def _archive_reject(candidate: Path, report: dict, reason: str) -> None:
    REJECTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = REJECTS_DIR / f"{candidate.name}-{ts}"
    if candidate.exists():
        try:
            shutil.copytree(candidate, target)
        except Exception as e:
            print(f"[warn] archive copy failed: {e}")
    (REJECTS_DIR / f"{candidate.name}-{ts}-report.json").write_text(
        json.dumps({"reason": reason, "report": report}, indent=2)
    )
    print(f"[archived] {target}")


def _promote(candidate: Path) -> None:
    """Atomically flip lora_active → candidate."""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    if LORA_ACTIVE.is_symlink() or LORA_ACTIVE.exists():
        LORA_ACTIVE.unlink()
    LORA_ACTIVE.symlink_to(candidate.resolve())
    print(f"[promoted] lora_active -> {candidate.resolve()}")


def _bootstrap_active_if_missing() -> None:
    """One-shot: if lora_active is missing AND lora_v1/ exists, symlink them.
    Lets the gate run from a cold install without manual setup.
    """
    if LORA_ACTIVE.is_symlink() or LORA_ACTIVE.exists():
        return
    fallback = TRAINING_DIR / "lora_v1"
    if fallback.exists() and fallback.is_dir():
        LORA_ACTIVE.symlink_to(fallback.resolve())
        print(f"[bootstrap] lora_active -> {fallback.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="LoRA A/B gate and deploy")
    parser.add_argument(
        "--candidate",
        type=Path,
        default=DEFAULT_CANDIDATE,
        help="path to LoRA adapter directory (default: logs/training/lora_v_candidate)",
    )
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=BRAIN_ROOT / "cli" / "eval_set.json",
        help="eval set JSON (default: full 744)",
    )
    parser.add_argument(
        "--delta-threshold",
        type=float,
        default=2.0,
        help="minimum hit_content_pct delta to promote (default 2.0)",
    )
    parser.add_argument(
        "--worst-regression",
        type=float,
        default=5.0,
        help="max allowed worst per-query regression in pts (default 5.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="never promote, just compute deltas and print",
    )
    args = parser.parse_args()

    _bootstrap_active_if_missing()

    if not args.candidate.exists():
        print(f"[skip] candidate adapter missing: {args.candidate} (no fine-tune available)")
        return 0  # not a hard error — first run before any training cycle

    # 2026-04-17 fix: ensure brain-server is in base state before baseline
    print("[ab_gate] clearing adapter on brain-server (base state)")
    clear_resp = _switch_brain_adapter(None)
    print(f"  → {clear_resp}")

    print(f"[ab_gate] running base eval ({args.eval_set.name})")
    t0 = time.time()
    base_report = _run_eval(args.eval_set)
    base_dur = int(time.time() - t0)
    base_v2 = base_report.get("v2", {})
    base_content = float(base_v2.get("hit_content_pct", 0))
    print(f"[ab_gate] base hit_content@5={base_content}% (took {base_dur}s)")

    # Load adapter ON brain-server so /recall/v2 actually uses it
    print(f"[ab_gate] loading adapter on brain-server: {args.candidate}")
    load_resp = _switch_brain_adapter(str(args.candidate.resolve()))
    print(f"  → {load_resp}")
    if load_resp.get("status") not in ("loaded", "unchanged"):
        print("[ab_gate] adapter load failed — aborting (would produce meaningless A/B)")
        _alert("LoRA adapter load failed", str(load_resp)[:400])
        # Always clear on exit so brain-server returns to known state
        _switch_brain_adapter(None)
        return 2

    print(f"[ab_gate] running candidate eval (lora:{args.candidate})")
    t0 = time.time()
    try:
        cand_report = _run_eval(args.eval_set)
    except Exception as e:
        print(f"[ab_gate] candidate eval failed: {e}")
        _alert("LoRA candidate eval failed", str(e)[:400])
        _switch_brain_adapter(None)
        return 2
    finally:
        # Always clear adapter after candidate pass so the running brain
        # doesn't stay in a test state if anything below fails.
        pass
    cand_dur = int(time.time() - t0)
    cand_v2 = cand_report.get("v2", {})
    cand_content = float(cand_v2.get("hit_content_pct", 0))
    print(f"[ab_gate] candidate hit_content@5={cand_content}% (took {cand_dur}s)")

    delta = cand_content - base_content
    print(f"[ab_gate] delta = {delta:+.2f}pts")

    # 2026-04-16 fix: real per-query worst regression. Previously this was
    # `max(0.0, -delta)` (the aggregate delta), which allowed a candidate
    # that improved 50% of queries by +4pts and regressed 50% by -3pts to
    # pass the gate with worst_reg=0 — silently destroying half the KB.
    # Now we align per_test arrays by query and compute the real maximum
    # per-case regression. If per_test is missing (older eval_compare or
    # shape drift), we fail CLOSED and reject rather than promote blind.
    worst_regression, regressed_count = _per_query_worst_regression(base_report, cand_report)
    if regressed_count < 0:
        print("[ab_gate] per_test data missing — FAIL CLOSED (cannot verify)")
        worst_regression = float("inf")

    summary = (
        f"base={base_content:.1f}% cand={cand_content:.1f}% "
        f"delta={delta:+.2f}pts worst_reg={worst_regression:.2f}pts "
        f"regressed_cases={regressed_count}"
    )

    # 2026-04-17: always return brain-server to clean (no-adapter) state
    # before returning verdict. Prevents a dry-run from leaving the adapter
    # silently active on the production server.
    print("[ab_gate] clearing adapter from brain-server (restore base state)")
    clear_resp = _switch_brain_adapter(None)
    print(f"  → {clear_resp}")

    if delta >= args.delta_threshold and worst_regression <= args.worst_regression:
        if args.dry_run:
            print(f"[ab_gate] DRY-RUN would promote: {summary}")
        else:
            _promote(args.candidate)
            # Reload adapter on brain-server so promoted state takes effect immediately
            _switch_brain_adapter(str(args.candidate.resolve()))
            _alert("LoRA promoted", summary)
        return 0

    if args.dry_run:
        print(f"[ab_gate] DRY-RUN would reject: {summary}")
    else:
        _archive_reject(args.candidate, cand_report, summary)
        _alert("LoRA rejected", summary)
    return 1


if __name__ == "__main__":
    sys.exit(main())
