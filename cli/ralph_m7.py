#!/Users/chrischo/server/brain/.venv/bin/python
"""ralph_m7.py — Phase M7 workstream-level state machine.

Distinct from cli/eval_sweep.py (which is knob-level, auto-patching) — this one
tracks nine workstreams that close the brain v2 → commercial-grade gap. The
driver doesn't auto-code anything; it tracks state, prints what's next, and
records outcomes. The actual work per workstream is performed by Claude (the
operator) reading `--next` output and executing file edits, subagent dispatches,
and integration runs in-session.

State file: /tmp/brain_ralph_m7_state.json  (atomic rename on every write)
Review log dir: logs/ralph_m7_reviews/

Invocation modes:
  ralph_m7.py --init              # create state file if missing
  ralph_m7.py --next              # print next pending workstream + done-criteria (JSON)
  ralph_m7.py --start WS          # mark workstream in_progress, record attempt
  ralph_m7.py --done WS --commit-sha SHA --metric KEY=VALUE ...
                                   # mark workstream done with attached evidence
  ralph_m7.py --fail WS --reason TEXT
                                   # record a failed attempt (keeps status=in_progress)
  ralph_m7.py --block WS --reason TEXT
                                   # mark blocked (skip in --next until manually unblocked)
  ralph_m7.py --report            # print a markdown progress report
  ralph_m7.py --status            # print compact one-line status
  ralph_m7.py --check-stop        # exit 0 if loop should stop, 1 if keep going

Stop conditions:
- All 9 workstreams done                      → exit 0 (done=true)
- plateau_streak >= 3                         → exit 0 (done=true, stopped=plateau)
- iteration >= 30                             → exit 0 (done=true, stopped=cap)
- any workstream status == 'aborted'          → exit 0 (done=true, stopped=abort)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_FILE = Path("/tmp/brain_ralph_m7_state.json")  # noqa: S108
BRAIN_ROOT = Path("/Users/chrischo/server/brain")
REVIEW_DIR = BRAIN_ROOT / "logs" / "ralph_m7_reviews"
PLAN_FILE = Path("/Users/chrischo/.claude/plans/eventual-wobbling-seahorse.md")

ITERATION_CAP = 30
PLATEAU_LIMIT = 3

WORKSTREAMS: list[dict[str, Any]] = [
    {
        "id": "WS1",
        "title": "Ellie reboot collision fix",
        "priority": 1,
        "est_iters": 1,
        "risk": "low",
        "done_criteria": [
            "~/.openclaw/cron/jobs.json has Ellie cron at 01:00/01:05/01:20 Sun",
            "ensure_weekly_reboot_reminder.sh stale '04:15' comment replaced with '01:00'",
            "weekly_reboot_postcheck.py has OrbStack health probe",
            "openclaw gateway reloaded (launchctl kickstart)",
        ],
    },
    {
        "id": "WS5",
        "title": "Eval history document",
        "priority": 1,  # pair with WS1 in iter 1 — both trivial
        "est_iters": 1,
        "risk": "low",
        "done_criteria": [
            "brain/EVAL_HISTORY.md exists",
            "has >=4 historical rows (from git log on eval_baseline_*.json)",
            "has current row (2026-04-13: stable 91.3/95.7 extended 78.4/68.2)",
            "has M7 target row (extended content_hit >=80)",
        ],
    },
    {
        "id": "WS8",
        "title": "Brain adoption counter + coverage fix",
        "priority": 2,
        "est_iters": 2,
        "risk": "low",
        "done_criteria": [
            "action_audit table non-empty (>=1 row per tool)",
            "provenance.agent fix at brain_store write path",
            "/brain/usage GET endpoint returns structured JSON",
            "all 6 actors (jenna/liz/ellie/sage/market/claude-code) have >=1 call in last 24h",
        ],
    },
    {
        "id": "WS6",
        "title": "SearXNG wire-up across all agents + Claude Code",
        "priority": 3,
        "est_iters": 2,
        "risk": "low",
        "done_criteria": [
            "brain_search_web in all 5 workspace-*/TOOLS.md Brain Tools tables",
            "brain_search_web usage rule in all 5 workspace-*/AGENTS.md",
            "workspace-claude/TOOLS.md rewritten with real brain table",
            "Claude Code MCP server restarted; 12 brain tools visible",
            "web_source_trust_recompute job logged at least one run",
            "7d SearXNG call count >=20 across >=3 actors (may require waiting)",
        ],
    },
    {
        "id": "WS4",
        "title": "Self-evolution E2E integration test",
        "priority": 4,
        "est_iters": 1,
        "risk": "low",
        "done_criteria": [
            "tests/integration/test_self_evolution_e2e.py passes",
            "5 synthetic corrections flowed end-to-end: feedback -> eval_proposals -> holdout_audit -> lora_ab_gate",
            "brain_eval_holdout_growth_weekly SLO metric surfaced in /brain/slos",
        ],
    },
    {
        "id": "WS7",
        "title": "Fresh 5-agent deep audit + remediation",
        "priority": 5,
        "est_iters": 4,
        "risk": "medium",
        "done_criteria": [
            "audit report at logs/ralph_m7_reviews/audit_m567.md",
            "5 parallel Explore agents dispatched (security/logic/hot-path/quality/db concurrency)",
            "zero unfixed criticals",
            "zero unfixed highs unless deferred with written reason",
        ],
    },
    {
        "id": "WS3",
        "title": "CRAG default-on + HippoRAG2 triple linking",
        "priority": 6,
        "est_iters": 3,
        "risk": "medium",
        "done_criteria": [
            "server.py:_recall_v2 flips CRAG default to on; ?iterative=false opt-out",
            "cli/eval_compare.py has --iterative flag",
            "brain_core/triple_link.py exists; wired into search_unified.py pre-RRF",
            "extended_train content_hit >=72 (from 68.2)",
            "stable eval content_hit stays >=94",
            "p50 latency <=500ms",
        ],
    },
    {
        "id": "WS2",
        "title": "Docling PDF + OpenClaw image captioning",
        "priority": 7,
        "est_iters": 5,
        "risk": "medium",
        "done_criteria": [
            "docling added to requirements.txt and installed in .venv",
            "ingest/pdfs.py exists with Docling-based parser",
            "ingest/images.py exists with OpenClaw vision dispatch + OCR fallback + hash dedupe",
            "pdf_ingest (05:30) + image_ingest (05:45) daily jobs in scheduler.py",
            ">=1 PDF indexed with >=1 captioned image",
            "dedupe works (re-run adds 0 new entries)",
            "IMAGE_INGEST_DAILY_CAP enforced at 20",
            "20 new PDF-source queries in eval_set_extended.json",
            "content_hit on new queries >=70",
            "projected monthly cost <$10",
        ],
    },
    {
        "id": "WS9",
        "title": "$50k commercial gap triage (bounded)",
        "priority": 8,
        "est_iters": 10,  # bounded, fill remaining iters
        "risk": "low",
        "done_criteria": [
            "brain/BENCHMARKS.md with BEIR NQ + HotpotQA dev subset numbers",
            "brain/ARCHITECTURE.md component diagram + narrative",
            "brain/DEPLOY.md + Dockerfile + docker-compose.yml (not actually deployed)",
            "brain/API.md auto-generated from FastAPI OpenAPI",
            "sdk/python/brain_client.py thin wrapper (200 LOC)",
            "API key rotation procedure documented",
            "brain/COMMERCIAL_READINESS.md 8-axis rubric scored before/after",
        ],
    },
]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _init_state() -> dict[str, Any]:
    state = {
        "created_at": _now_iso(),
        "iteration": 0,
        "plateau_streak": 0,
        "stopped": None,
        "done": False,
        "workstreams": {},
    }
    for ws in WORKSTREAMS:
        state["workstreams"][ws["id"]] = {
            "title": ws["title"],
            "status": "pending",
            "priority": ws["priority"],
            "est_iters": ws["est_iters"],
            "risk": ws["risk"],
            "done_criteria": ws["done_criteria"],
            "attempts": 0,
            "commit_shas": [],
            "metrics": {},
            "notes": [],
            "started_at": None,
            "done_at": None,
        }
    return state


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return _init_state()
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        print("state file corrupt, re-initializing", file=sys.stderr)
        return _init_state()


def _save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def _check_stop(state: dict[str, Any]) -> tuple[bool, str | None]:
    all_done = all(w["status"] == "done" for w in state["workstreams"].values())
    if all_done:
        return True, "success"
    if state["iteration"] >= ITERATION_CAP:
        return True, "cap"
    if state["plateau_streak"] >= PLATEAU_LIMIT:
        return True, "plateau"
    if any(w["status"] == "aborted" for w in state["workstreams"].values()):
        return True, "abort"
    return False, None


def _next_workstream(state: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        (wid, w) for wid, w in state["workstreams"].items() if w["status"] in ("pending", "in_progress")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda kv: (kv[1]["priority"], kv[0]))
    wid, w = candidates[0]
    return {"id": wid, **w}


def cmd_init(_args: argparse.Namespace) -> int:
    if STATE_FILE.exists():
        print(f"state file already exists: {STATE_FILE}", file=sys.stderr)
        return 0
    state = _init_state()
    _save_state(state)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"initialized state at {STATE_FILE}")
    print(f"review dir at {REVIEW_DIR}")
    return 0


def cmd_next(_args: argparse.Namespace) -> int:
    state = _load_state()
    stop, reason = _check_stop(state)
    if stop:
        print(json.dumps({"stop": True, "reason": reason, "iteration": state["iteration"]}))
        return 0
    w = _next_workstream(state)
    if not w:
        print(json.dumps({"stop": True, "reason": "no_pending", "iteration": state["iteration"]}))
        return 0
    state["iteration"] += 1
    _save_state(state)
    print(
        json.dumps(
            {
                "stop": False,
                "iteration": state["iteration"],
                "workstream": w["id"],
                "title": w["title"],
                "status": w["status"],
                "done_criteria": w["done_criteria"],
                "attempts": w["attempts"],
                "plan_ref": f"{PLAN_FILE}#{w['id'].lower()}",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    state = _load_state()
    wid = args.start
    if wid not in state["workstreams"]:
        print(f"unknown workstream: {wid}", file=sys.stderr)
        return 2
    w = state["workstreams"][wid]
    w["status"] = "in_progress"
    w["attempts"] += 1
    if w["started_at"] is None:
        w["started_at"] = _now_iso()
    _save_state(state)
    print(f"started {wid} (attempt {w['attempts']})")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    state = _load_state()
    wid = args.done
    if wid not in state["workstreams"]:
        print(f"unknown workstream: {wid}", file=sys.stderr)
        return 2
    w = state["workstreams"][wid]
    w["status"] = "done"
    w["done_at"] = _now_iso()
    if args.commit_sha:
        w["commit_shas"].append(args.commit_sha)
    for m in args.metric or []:
        if "=" in m:
            k, v = m.split("=", 1)
            w["metrics"][k.strip()] = v.strip()
    state["plateau_streak"] = 0  # any completion resets plateau
    _save_state(state)
    stop, reason = _check_stop(state)
    if stop:
        state["done"] = True
        state["stopped"] = reason
        _save_state(state)
    print(f"completed {wid}")
    if stop:
        print(f"loop finished: {reason}")
    return 0


def cmd_fail(args: argparse.Namespace) -> int:
    state = _load_state()
    wid = args.fail
    if wid not in state["workstreams"]:
        print(f"unknown workstream: {wid}", file=sys.stderr)
        return 2
    w = state["workstreams"][wid]
    w["notes"].append(f"[{_now_iso()}] FAIL: {args.reason}")
    state["plateau_streak"] += 1
    _save_state(state)
    print(f"recorded failure on {wid}: {args.reason}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    state = _load_state()
    wid = args.block
    if wid not in state["workstreams"]:
        print(f"unknown workstream: {wid}", file=sys.stderr)
        return 2
    w = state["workstreams"][wid]
    w["status"] = "blocked"
    w["notes"].append(f"[{_now_iso()}] BLOCKED: {args.reason}")
    _save_state(state)
    print(f"blocked {wid}: {args.reason}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    state = _load_state()
    counts = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0, "aborted": 0}
    for w in state["workstreams"].values():
        counts[w["status"]] = counts.get(w["status"], 0) + 1
    print(
        f"iter={state['iteration']}/{ITERATION_CAP} "
        f"done={counts['done']}/9 "
        f"in_progress={counts['in_progress']} "
        f"pending={counts['pending']} "
        f"blocked={counts['blocked']} "
        f"plateau={state['plateau_streak']}/{PLATEAU_LIMIT} "
        f"stopped={state['stopped'] or '-'}"
    )
    return 0


def cmd_report(_args: argparse.Namespace) -> int:
    state = _load_state()
    lines = [
        "# Ralph M7 — Progress Report",
        "",
        f"Generated: {_now_iso()}",
        f"Iteration: {state['iteration']}/{ITERATION_CAP}",
        f"Plateau streak: {state['plateau_streak']}/{PLATEAU_LIMIT}",
        f"Stopped: {state['stopped'] or '(running)'}",
        "",
        "## Workstream table",
        "",
        "| ID | Title | Status | Attempts | Commits | Metrics |",
        "|----|-------|--------|----------|---------|---------|",
    ]
    for wid in ["WS1", "WS2", "WS3", "WS4", "WS5", "WS6", "WS7", "WS8", "WS9"]:
        w = state["workstreams"].get(wid)
        if not w:
            continue
        metrics = ", ".join(f"{k}={v}" for k, v in w["metrics"].items()) or "-"
        commits = ", ".join(w["commit_shas"][:3]) or "-"
        lines.append(f"| {wid} | {w['title']} | {w['status']} | {w['attempts']} | {commits} | {metrics} |")
    lines.append("")
    lines.append("## Notes")
    for wid in ["WS1", "WS2", "WS3", "WS4", "WS5", "WS6", "WS7", "WS8", "WS9"]:
        w = state["workstreams"].get(wid)
        if not w or not w["notes"]:
            continue
        lines.append(f"\n### {wid}")
        for note in w["notes"]:
            lines.append(f"- {note}")
    print("\n".join(lines))
    return 0


def cmd_check_stop(_args: argparse.Namespace) -> int:
    state = _load_state()
    stop, reason = _check_stop(state)
    if stop:
        print(f"STOP: {reason}")
        return 0
    print("CONTINUE")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true")
    group.add_argument("--next", action="store_true")
    group.add_argument("--start", metavar="WS_ID")
    group.add_argument("--done", metavar="WS_ID")
    group.add_argument("--fail", metavar="WS_ID")
    group.add_argument("--block", metavar="WS_ID")
    group.add_argument("--status", action="store_true")
    group.add_argument("--report", action="store_true")
    group.add_argument("--check-stop", action="store_true")
    p.add_argument("--commit-sha", default=None)
    p.add_argument("--metric", action="append", default=None, help="key=value; repeatable")
    p.add_argument("--reason", default="", help="reason text for --fail / --block")
    args = p.parse_args()
    if args.init:
        return cmd_init(args)
    if args.next:
        return cmd_next(args)
    if args.start:
        return cmd_start(args)
    if args.done:
        return cmd_done(args)
    if args.fail:
        return cmd_fail(args)
    if args.block:
        return cmd_block(args)
    if args.status:
        return cmd_status(args)
    if args.report:
        return cmd_report(args)
    if args.check_stop:
        return cmd_check_stop(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
