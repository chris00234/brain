#!/opt/homebrew/bin/python3
"""Git activity ingest — Ellie distills technical decisions from commit history.

Scans configured git repos, groups commits by (repo, day), dispatches
batches to Ellie for distillation, writes kept summaries to raw/inbox/.

Pipeline: git log → group by day → Ellie distillation → raw/inbox → canonical pipeline

Usage:
  git_activity.py [--dry-run] [--max-days 90]
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/git-activity-state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/git-activity-failures.jsonl")

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
DISPATCH_AGENT = "ellie"
DISPATCH_TIMEOUT = 240
BATCH_SIZE = 5  # daily logs per dispatch

# Repos to track
REPOS = [
    Path.home() / "server/chrischodev",
    Path.home() / "server/claw3d",
    Path.home() / "server/knowledge",
    Path.home() / "LibreUIUX-Claude-Code",
    Path.home() / "jenna_teacher",
    Path.home() / "oc-lifehub",
    Path.home() / "ui-ux-pro-max-skill",
]

# Commit messages to skip
SKIP_PATTERNS = [
    re.compile(r"^merge\s", re.I),
    re.compile(r"^initial commit$", re.I),
    re.compile(r"^wip$", re.I),
    re.compile(r"^fix$", re.I),
    re.compile(r"^update$", re.I),
]
MIN_MSG_LEN = 15


# ── State ───────────────────────────────────────────────
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load
    from safe_state import save_state as _safe_save

    def load_state():
        return _safe_load(STATE_FILE)

    def save_state(state):
        _safe_save(STATE_FILE, state)
except ImportError:

    def load_state():
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}

    def save_state(state):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


# ── Git Log Parsing ─────────────────────────────────────
def get_commits(repo: Path, since_sha: str | None = None) -> list[dict]:
    """Get commits from a git repo, optionally since a specific SHA."""
    if not (repo / ".git").exists():
        return []

    cmd = ["git", "-C", str(repo), "log", "--format=%H|%ai|%s", "--stat"]
    if since_sha:
        cmd.append(f"{since_sha}..HEAD")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        log_failure(f"git log failed for {repo.name}: {e}")
        return []

    if r.returncode != 0:
        return []

    commits = []
    current = None
    stat_lines = []

    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and len(parts[0]) == 40:
            # Save previous commit
            if current:
                current["stat"] = "\n".join(stat_lines[-3:])  # last 3 stat lines (summary)
                commits.append(current)
                stat_lines = []
            sha, date_str, msg = parts
            current = {
                "sha": sha,
                "date": date_str[:10],
                "message": msg.strip(),
                "repo": repo.name,
            }
        elif current and line.strip():
            stat_lines.append(line.strip())

    if current:
        current["stat"] = "\n".join(stat_lines[-3:])
        commits.append(current)

    return commits


def filter_commits(commits: list[dict]) -> list[dict]:
    """Remove noise commits."""
    out = []
    for c in commits:
        msg = c["message"]
        if len(msg) < MIN_MSG_LEN:
            continue
        if any(pat.match(msg) for pat in SKIP_PATTERNS):
            continue
        out.append(c)
    return out


def group_by_day(commits: list[dict]) -> dict[str, list[dict]]:
    """Group commits by (repo, date) key."""
    groups: dict[str, list[dict]] = {}
    for c in commits:
        key = f"{c['repo']}:{c['date']}"
        groups.setdefault(key, []).append(c)
    return groups


# ── Agent Dispatch ──────────────────────────────────────
def build_distillation_prompt(daily_logs: list[dict]) -> str:
    lines = [
        f"You are Ellie. Review these {len(daily_logs)} daily git activity logs.",
        "Extract: what did Chris build, what technical decisions were made, what bugs were fixed.",
        'Focus on the "why" behind changes, not the "what".',
        "",
        "Skip: routine maintenance, dependency bumps, trivial one-line fixes.",
        "",
    ]
    for i, log in enumerate(daily_logs, 1):
        lines.append(f'[{i}] {log["header"]}')
        for commit_line in log["commits"]:
            lines.append(f"    - {commit_line}")
        lines.append("")

    lines.append("OUTPUT FORMAT (return ONLY valid JSON):")
    lines.append(
        '{"keep": [{"index": <int>, "summary": "<1-3 sentences>", "signal_type": "feature|bugfix|architecture|refactor|infra", "signal_score": <1-10>}], "skip_reason": "<why others were skipped>"}'
    )
    lines.append("")
    lines.append("STRICT: only the JSON object. Empty keep list is fine.")
    return "\n".join(lines)


def dispatch_distillation(prompt: str) -> dict | None:
    cmd = [
        OPENCLAW_BIN,
        "agent",
        "--agent",
        DISPATCH_AGENT,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(DISPATCH_TIMEOUT),
        "--thinking",
        "off",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DISPATCH_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        log_failure("ellie dispatch timed out")
        return None
    if r.returncode != 0:
        log_failure(f"ellie dispatch failed: {r.stderr[:300]}")
        return None
    try:
        response = json.loads(r.stdout)
        text = response.get("result", {}).get("payloads", [])[0].get("text", "")
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log_failure(f"could not parse Ellie reply: {e}")
        return None


# ── Record Writing ──────────────────────────────────────
def write_record(log: dict, item: dict) -> Path | None:
    summary = item.get("summary", "")
    signal_type = item.get("signal_type", "unknown")
    score = item.get("signal_score", 0)

    if score < 6 or not summary:
        return None

    repo = log["repo"]
    date = log["date"]
    content = (
        f"Git activity in {repo} on {date}\n"
        f"Signal: {signal_type} (score {score}/10)\n\n"
        f"{summary}\n\n"
        f"Commits:\n" + "\n".join(f"  - {c}" for c in log["commits"][:10])
    )

    digest = hashlib.sha256(content.encode()).hexdigest()
    date_part = date.replace("-", "_")
    rec_id = f"raw_git_{repo}_{date_part}_{digest[:8]}"

    record = {
        "id": rec_id,
        "timestamp": f"{date}T00:00:00Z",
        "source_type": "git_activity",
        "source_ref": f"git:{repo}:{date}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content,
        "attachments": [],
        "entities": ["Chris", repo, signal_type],
        "hash": f"sha256:{digest}",
    }

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f"{rec_id}.json"
    if out.exists():
        return None
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


# ── Main ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Git activity ingest via Ellie distillation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-days", type=int, default=90)
    args = parser.parse_args()

    state = load_state()
    total_written = 0
    all_daily_logs = []
    failed_repos = set()
    repo_latest_sha = {}  # cache newest SHA per repo from first scan

    print(f"Git activity ingest — {len(REPOS)} repos")

    for repo in REPOS:
        if not repo.exists():
            continue
        repo_name = repo.name
        since_sha = state.get(repo_name, {}).get("last_sha")

        commits = get_commits(repo, since_sha)
        commits = filter_commits(commits)
        if commits:
            repo_latest_sha[repo_name] = commits[0]["sha"]

        if not commits:
            print(f"  [{repo_name}] no new commits")
            continue

        print(f"  [{repo_name}] {len(commits)} new commits")

        # Group by day
        groups = group_by_day(commits)
        for key, day_commits in sorted(groups.items()):
            repo_name_k, date = key.split(":", 1)
            commit_lines = [f'{c["message"]}' for c in day_commits]
            daily_log = {
                "repo": repo_name_k,
                "date": date,
                "header": f"{repo_name_k} on {date} ({len(day_commits)} commits)",
                "commits": commit_lines,
            }
            all_daily_logs.append(daily_log)

    print(f"\nTotal: {len(all_daily_logs)} daily logs to process")

    if args.dry_run:
        for log in all_daily_logs[:10]:
            print(f'  {log["header"]}')
            for c in log["commits"][:3]:
                print(f"    - {c[:80]}")
        print(f"\nDone — {len(all_daily_logs)} daily logs, 0 records written (dry run)")
        return

    # Dispatch in batches
    for i in range(0, len(all_daily_logs), BATCH_SIZE):
        batch = all_daily_logs[i : i + BATCH_SIZE]
        prompt = build_distillation_prompt(batch)
        result = dispatch_distillation(prompt)
        if result is None:
            import time

            time.sleep(10)
            result = dispatch_distillation(prompt)

        if not result:
            print(f"  Batch {i // BATCH_SIZE + 1}: DISPATCH FAILED")
            for log in batch:
                failed_repos.add(log["repo"])
            continue

        kept = result.get("keep", [])
        print(f"  Batch {i // BATCH_SIZE + 1}: {len(kept)}/{len(batch)} kept")

        for item in kept:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(batch):
                path = write_record(batch[idx], item)
                if path:
                    total_written += 1

    # Only advance watermark for repos where dispatch succeeded
    for repo_name, latest_sha in repo_latest_sha.items():
        if repo_name not in failed_repos:
            state.setdefault(repo_name, {})["last_sha"] = latest_sha

    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    print(f"\nDone — {len(all_daily_logs)} daily logs processed, {total_written} records written")


if __name__ == "__main__":
    main()
