#!/Users/chrischo/server/brain/.venv/bin/python
"""ralph_m7_review.py — per-iteration code review dispatcher for Phase M7.

Reads the most recent staged-or-unstaged diff (or a specified commit range),
formats it into a code-review prompt, and writes a structured review to
`logs/ralph_m7_reviews/iter_<N>.md`. In this simplified implementation the
driver cannot spawn Claude subagents directly (that happens in-session, not
via subprocess), so the script instead:

  1. Gathers the diff + context + done-criteria from ralph_m7 state
  2. Writes a review-request file at logs/ralph_m7_reviews/iter_<N>_request.md
  3. Waits for a matching logs/ralph_m7_reviews/iter_<N>_review.md authored by
     the reviewer (Claude operator dispatches `feature-dev:code-reviewer` in-session
     and writes the report to that path), OR skips-and-warns if `--no-wait`.
  4. Parses the review for "CRITICAL:" markers. Any present → exit 1 (block).
     Otherwise → exit 0.

Invocation:
  ralph_m7_review.py --workstream WS1 [--since HEAD~1] [--no-wait] [--timeout SECONDS]

The operator (Claude or human) is expected to:
  - Read logs/ralph_m7_reviews/iter_<N>_request.md
  - Dispatch the review (via Task tool or feature-dev:code-reviewer)
  - Write the output to logs/ralph_m7_reviews/iter_<N>_review.md
  - Re-run this script (it will pick up the review file and parse)

In `--no-wait` mode the script exits immediately after writing the request,
allowing bash loops to chain work without blocking on a human gate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
REVIEW_DIR = BRAIN_ROOT / "logs" / "ralph_m7_reviews"
STATE_FILE = Path("/tmp/brain_ralph_m7_state.json")  # noqa: S108
CRITICAL_PATTERN = re.compile(r"\b(CRITICAL|CRIT|SEV:\s*CRITICAL)\b", re.I)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"iteration": 0, "workstreams": {}}
    return json.loads(STATE_FILE.read_text())


def _git_diff(since: str) -> str:
    try:
        r = subprocess.run(
            ["git", "diff", since, "--stat", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(BRAIN_ROOT),
            timeout=10,
        )
        stat = r.stdout or "(no changes)"
    except Exception as e:
        stat = f"(git diff stat error: {e})"

    try:
        r = subprocess.run(
            ["git", "diff", since, "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(BRAIN_ROOT),
            timeout=30,
        )
        full = r.stdout or ""
    except Exception as e:
        full = f"(git diff error: {e})"

    # If there's nothing committed, also include unstaged changes
    if not full.strip():
        try:
            r = subprocess.run(
                ["git", "diff"],
                capture_output=True,
                text=True,
                cwd=str(BRAIN_ROOT),
                timeout=30,
            )
            full = r.stdout
        except Exception as e:
            full = f"(git diff unstaged error: {e})"

    return f"{stat}\n\n{full}".strip()


def _write_request(iteration: int, workstream_id: str, diff: str, state: dict) -> Path:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    req_path = REVIEW_DIR / f"iter_{iteration:02d}_{workstream_id}_request.md"

    w = state.get("workstreams", {}).get(workstream_id, {})
    done = "\n".join(f"- {c}" for c in w.get("done_criteria", []))

    req = f"""# Ralph M7 — Iteration {iteration} Code Review Request

**Workstream**: {workstream_id} — {w.get("title", "(unknown)")}
**Status**: {w.get("status", "?")}
**Attempts**: {w.get("attempts", 0)}
**Generated**: {datetime.now().isoformat(timespec="seconds")}

## Done criteria for this workstream

{done or "(none defined)"}

## Diff under review

```diff
{diff[:200_000]}
```

## Review instructions for the reviewer

Use `feature-dev:code-reviewer` subagent (or equivalent). Return a markdown
report at:

  `logs/ralph_m7_reviews/iter_{iteration:02d}_{workstream_id}_review.md`

The report must use the following section headers:

  ## Summary
  ## Critical issues
  ## High issues
  ## Medium issues
  ## Low / style
  ## Verdict

Any issue severity-tagged `CRITICAL` in the body will block the commit.
All other severities are advisory.

If there are no issues, the Critical/High/Medium sections may contain `(none)`.
"""
    req_path.write_text(req)
    return req_path


def _wait_for_review(iteration: int, workstream_id: str, timeout: int) -> Path | None:
    review_path = REVIEW_DIR / f"iter_{iteration:02d}_{workstream_id}_review.md"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if review_path.exists():
            return review_path
        time.sleep(2)
    return None


def _parse_review(review_path: Path) -> tuple[bool, list[str]]:
    text = review_path.read_text()
    # Extract the Critical issues section
    m = re.search(r"## Critical issues\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL | re.I)
    critical_body = m.group(1).strip() if m else ""
    normalized = critical_body.lower().strip()

    # Empty markers
    if not critical_body or normalized in {"(none)", "none", "n/a", "-"}:
        return False, []

    # Extract bullet items
    items = [
        line.strip().lstrip("-* ").strip()
        for line in critical_body.splitlines()
        if line.strip().startswith(("-", "*", "•"))
    ]
    if not items and critical_body:
        items = [critical_body[:200]]

    # Also catch a body with the literal word CRITICAL but no bullets
    if CRITICAL_PATTERN.search(critical_body) and not items:
        items = [critical_body[:200]]

    return bool(items), items


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workstream", required=True)
    p.add_argument("--since", default="HEAD~1", help="git ref to diff from (default: HEAD~1)")
    p.add_argument(
        "--no-wait", action="store_true", help="exit after writing request; do not wait for review file"
    )
    p.add_argument("--timeout", type=int, default=1200, help="seconds to wait for review file (default 1200)")
    args = p.parse_args()

    state = _load_state()
    iteration = state.get("iteration", 0)

    diff = _git_diff(args.since)
    req_path = _write_request(iteration, args.workstream, diff, state)
    print(f"wrote review request: {req_path}")

    if args.no_wait:
        print("--no-wait: skipping blocking review")
        return 0

    print(f"waiting up to {args.timeout}s for review file...")
    review_path = _wait_for_review(iteration, args.workstream, args.timeout)
    if not review_path:
        print(f"ERROR: no review file after {args.timeout}s — run reviewer manually")
        return 2

    blocked, items = _parse_review(review_path)
    if blocked:
        print(f"BLOCKED by {len(items)} critical issue(s):")
        for it in items:
            print(f"  - {it}")
        return 1

    print(f"review passed — no critical issues ({review_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
