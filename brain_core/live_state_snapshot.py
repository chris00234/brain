"""brain_core/live_state_snapshot.py — periodic live ground-truth capture.

Solves the stale-retrieval problem for queries like "what's running on my
server right now?" or "뭐가 돌아가?". Without this, brain answers from atoms
which are historical snapshots. With this, a 10-minute cron captures the
CURRENT state of docker/launchd/git/goals/sessions and writes it as canonical
markdown files. active_recall's `live_state` intent route then guarantees
these files surface for "now/current/running" queries.

Topics captured:
  docker_services    — docker ps (image, status, name, uptime)
  launchd_services   — launchctl list for ai.openclaw.* jobs
  active_goals       — autonomy.db::goals WHERE status='active' + focus_items
  recent_commits     — git log last 24h across ~/server/* repos
  active_sessions    — action_audit distinct actors in last 1h

Output: ~/server/knowledge/canonical/live_state/{topic}.md
Each file is OVERWRITTEN (not appended) on every run via atomic .tmp → rename,
so the directory always contains the current state with no chain history.

Trigger: scheduler job `live_state_snapshot` at IntervalTrigger(minutes=10).
Consumer: active_recall reads the directory via canonical_paths in intent_routes.yaml.
Effect: "server status" queries return current reality, not yesterday's snapshot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR, KNOWLEDGE_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
LIVE_STATE_DIR = KNOWLEDGE_DIR / "canonical" / "live_state"

log = logging.getLogger("brain.live_state_snapshot")


from db import now_iso as _now_iso  # noqa: E402  — single-source UTC stamp helper


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _atomic_write(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content)
        tmp.rename(path)
        return True
    except OSError as e:
        log.warning("atomic write failed for %s: %s", path, e)
        return False


def _header(title: str) -> str:
    return (
        f"# {title}\n\n"
        f"> **Live state snapshot.** Regenerated every 10 minutes by "
        f"`live_state_snapshot` cron. This is the CURRENT reality of the "
        f"system — do not treat as historical record.\n"
        f">\n"
        f"> Captured: {_now_iso()}\n\n"
    )


# ── Docker services ──────────────────────────────────────────────


def _snapshot_docker() -> str:
    """Capture `docker ps` output as formatted markdown."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _header("Running Docker containers") + f"(docker ps failed: {e})\n"

    if result.returncode != 0:
        return (
            _header("Running Docker containers")
            + f"(docker ps returned {result.returncode}: {result.stderr[:200]})\n"
        )

    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return _header("Running Docker containers") + "_No containers running._\n"

    md = _header("Running Docker containers")
    md += f"**Count:** {len(lines)} containers\n\n"
    md += "| Name | Image | Status | Ports |\n"
    md += "|---|---|---|---|\n"
    for line in lines:
        parts = line.split("\t")
        name = parts[0] if len(parts) > 0 else ""
        image = parts[1] if len(parts) > 1 else ""
        status = parts[2] if len(parts) > 2 else ""
        ports = (parts[3] if len(parts) > 3 else "")[:60]
        md += f"| `{name}` | `{image}` | {status} | {ports} |\n"
    return md


# ── launchd services ─────────────────────────────────────────────


def _snapshot_launchd() -> str:
    """List ai.openclaw.* launchd services and their running state."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _header("Launchd services (native)") + f"(launchctl failed: {e})\n"

    md = _header("Launchd services (native)")
    md += "| Service | PID | Last Exit |\n"
    md += "|---|---|---|\n"
    rows = 0
    for line in (result.stdout or "").splitlines():
        if "ai.openclaw" not in line and "ai.brain" not in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        pid = parts[0]
        status = parts[1]
        label = parts[2]
        pid_display = pid if pid != "-" else "(not running)"
        md += f"| `{label}` | {pid_display} | {status} |\n"
        rows += 1
    if rows == 0:
        md += "| _(no ai.openclaw.* services found)_ | | |\n"
    return md


# ── Active goals + focus ─────────────────────────────────────────


def _snapshot_goals() -> str:
    """Read autonomy.db::goals WHERE status='active' + focus_items."""
    md = _header("Active goals and focus")
    try:
        with sqlite3.connect(str(AUTONOMY_DB), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            goals = conn.execute(
                "SELECT id, title, description, updated_at, owner_agent, brain_notes "
                "FROM goals WHERE status='active' ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
            focus = conn.execute(
                "SELECT content, category, agent, created_at "
                "FROM focus_items "
                "WHERE (expires_at IS NULL OR expires_at >= ?) "
                "ORDER BY created_at DESC LIMIT 10",
                (_now_iso(),),
            ).fetchall()
    except sqlite3.Error as e:
        return md + f"(autonomy.db read failed: {e})\n"

    md += f"## Active goals ({len(goals)})\n\n"
    if not goals:
        md += "_No active goals right now._\n\n"
    else:
        for g in goals:
            owner = g["owner_agent"] or "chris"
            md += f"- **{g['title']}** (owner: {owner}, last updated {(g['updated_at'] or '')[:19]})\n"
            if g["description"]:
                md += f"  {g['description'][:200]}\n"
            if g["brain_notes"]:
                md += f"  _brain notes:_ {g['brain_notes'][:200]}\n"

    md += f"\n## Manual focus items ({len(focus)})\n\n"
    if not focus:
        md += "_No manual focus set._\n"
    else:
        for f in focus:
            md += f"- [{f['category']}] {f['content'][:200]} _(by {f['agent'] or 'system'}, {(f['created_at'] or '')[:19]})_\n"
    return md


# ── Recent commits across repos ──────────────────────────────────


def _snapshot_commits() -> str:
    """Git log across the top-level server repos for the last 24 hours."""
    md = _header("Recent commits (last 24h)")
    repos = [
        Path.home() / "server" / "brain",
        Path.home() / "server" / "brain-ui",
        Path.home() / "server" / "knowledge",
    ]
    any_commits = False
    for repo in repos:
        if not (repo / ".git").exists():
            continue
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    "--since=24 hours ago",
                    "--pretty=format:%h %cd %s",
                    "--date=iso-strict",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            continue
        any_commits = True
        md += f"\n## `{repo.name}` ({len(lines)} commits)\n\n"
        for ln in lines[:15]:
            md += f"- {ln}\n"
    if not any_commits:
        md += "\n_No commits in any tracked repo in the last 24 hours._\n"
    return md


# ── Active sessions (Claude + OpenClaw agents) ──────────────────


def _snapshot_sessions() -> str:
    """Read action_audit for distinct session_id/actor in last hour."""
    md = _header("Active sessions (last 1h)")
    cutoff = (_utcnow() - timedelta(hours=1)).isoformat()
    try:
        with sqlite3.connect(str(BRAIN_DB), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT actor, COUNT(*) as turns, MAX(created_at) as last_activity "
                "FROM action_audit "
                "WHERE created_at >= ? AND route LIKE '/recall%' "
                "GROUP BY actor ORDER BY last_activity DESC",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error as e:
        return md + f"(action_audit read failed: {e})\n"

    if not rows:
        md += "_No agent activity in the last hour._\n"
        return md
    md += "| Actor | Turns | Last activity |\n"
    md += "|---|---|---|\n"
    for r in rows:
        md += f"| `{r['actor']}` | {r['turns']} | {(r['last_activity'] or '')[:19]} |\n"
    return md


# ── Main entry point ─────────────────────────────────────────────

TOPICS = {
    "docker_services.md": _snapshot_docker,
    "launchd_services.md": _snapshot_launchd,
    "active_goals.md": _snapshot_goals,
    "recent_commits.md": _snapshot_commits,
    "active_sessions.md": _snapshot_sessions,
}


def run() -> dict:
    """Run every snapshot function, write to canonical/live_state/.

    Returns {"status": "ok", "written": N, "failed": N, "duration_ms": ms}.
    """
    t0 = time.time()
    LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    failed = 0
    details: list[str] = []
    for filename, fn in TOPICS.items():
        try:
            content = fn()
        except Exception as e:
            log.warning("snapshot %s failed: %s", filename, e)
            content = _header(filename) + f"(snapshot failed: {e})\n"
            failed += 1
        target = LIVE_STATE_DIR / filename
        if _atomic_write(target, content):
            written += 1
            details.append(filename)
        else:
            failed += 1

    # Write a top-level index so the canonical_paths lookup also finds it.
    index_content = _header("Live state index")
    index_content += "## Files\n\n"
    for filename in TOPICS:
        index_content += f"- [{filename[:-3]}]({filename})\n"
    index_content += (
        f"\n---\nSnapshot captured: {_now_iso()}\n"
        "Next refresh: +10 minutes (cron: `live_state_snapshot`).\n"
    )
    _atomic_write(LIVE_STATE_DIR / "INDEX.md", index_content)

    return {
        "status": "ok",
        "written": written,
        "failed": failed,
        "files": details,
        "duration_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
