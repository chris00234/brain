"""brain_core/cross_repo_recall.py — find analog edits from other repos.

Looks at past Edit/Write coding events that ended with a positive
outcome (`refined`, `accepted`) and returns ones in other repos whose
text matches the current query. Lets recall answer "you fixed this
exact pattern in repo X three weeks ago — here's how" instead of
treating each repo as a closed world.

Pure SQLite FTS5 + coding_event_outcomes join. No LLM, no embeddings.
Coding-event payloads live in raw/inbox JSON files; only summary fields
(file_path, repo, snippet) are returned so the caller pulls full
context on demand.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("brain.cross_repo_recall")

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
DEFAULT_WINDOW_DAYS = 60
# What counts as a "kept" edit worth surfacing as a cross-repo analog.
# refined  = a later edit built on top of it (highest signal)
# accepted = a git commit confirmed it
# superseded = a later edit replaced it in a different region (mixed —
#              the original work happened and was not actively reverted)
# Only `reverted` is excluded outright: that signal says "this exact
# approach turned out wrong, do not propagate it."
POSITIVE_OUTCOMES = ("refined", "accepted", "superseded")
NEGATIVE_OUTCOMES = ("reverted",)


_HOME_PREFIXES = (
    "/Users/chrischo/server/",
    "/Users/chrischo/",
)


def _repo_from_cwd(cwd: str) -> str:
    """Infer the repo name from a working directory.

    `/Users/chrischo/server/brain/backups` should resolve to `brain`,
    not `backups` — the repo is the first path segment under one of
    the known home prefixes. Falls back to the last segment when the
    path doesn't match any prefix.
    """
    if not cwd:
        return ""
    for prefix in _HOME_PREFIXES:
        if cwd.startswith(prefix):
            tail = cwd[len(prefix) :].split("/", 1)[0]
            if tail and not tail.startswith("."):
                return tail
    return Path(cwd).name or ""


def _tokens(text: str, min_len: int = 3) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", text or "") if len(t) >= min_len]


def _fts_query(tokens: list[str]) -> str:
    # FTS5 OR query, scope to the coding payload tokens
    return " OR ".join(f'"{t}"' for t in tokens[:12])


_TEXT_FIELD_RE = re.compile(
    r"^(?P<tool>Edit|Write|Bash)\s+on\s+(?P<file>\S+)\s+session=(?P<sid>\S+)\s+cwd=(?P<cwd>\S+)\s+status=(?P<status>\w+)\s+old:(?P<old>.*?)\s+new:(?P<new>.*)$",
    re.DOTALL,
)


def _payload_from_row(row: sqlite3.Row) -> dict | None:
    """Best-effort decode of raw_events.content for coding events.

    Two storage shapes coexist:
      - structured JSON (older rows): full payload with cwd/file_path/old/new
      - synthesized human text (newer rows): "Edit on /path session=... cwd=... status=... old:... new:..."
    Try the JSON shape first via json_path → content; fall back to a
    regex on the synthesized text. Either way we want cwd + file_path +
    tool + new/old previews.
    """
    json_path = row["json_path"]
    if json_path:
        try:
            with Path(json_path).open() as f:
                rec = json.load(f)
            return json.loads(rec.get("content", "{}"))
        except (OSError, json.JSONDecodeError):
            pass
    content = row["content"] or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = _TEXT_FIELD_RE.match(content)
    if not m:
        return None
    return {
        "tool": m.group("tool"),
        "file_path": m.group("file"),
        "cwd": m.group("cwd"),
        "new_preview": m.group("new"),
        "old_preview": m.group("old"),
        "success": m.group("status") == "ok",
    }


def find_analogs(
    *,
    query: str,
    current_repo: str = "",
    brain_db_path: Path | str,
    limit: int = DEFAULT_LIMIT,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[dict]:
    """Return up to `limit` analog edits in repos other than `current_repo`.

    Each item: {repo, file_path, tool, outcome, timestamp, snippet}.
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    tokens = _tokens(query)
    if not tokens:
        return []
    db_path = Path(brain_db_path)
    if not db_path.exists():
        return []
    fts_query = _fts_query(tokens)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT re.id, re.timestamp, re.content, re.json_path, co.outcome
                FROM raw_events_fts fts
                JOIN raw_events re ON re.rowid = fts.rowid
                JOIN coding_event_outcomes co ON co.event_id = re.id
                WHERE raw_events_fts MATCH ?
                  AND re.source_type = 'coding_event'
                  AND co.outcome IN ('refined', 'accepted', 'superseded')
                  AND re.timestamp > datetime('now', ?)
                ORDER BY re.timestamp DESC
                LIMIT 200
                """,
                (fts_query, f"-{int(window_days)} days"),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug("cross_repo_recall FTS failed: %s", exc)
        return []

    per_repo: dict[str, dict] = {}
    for row in rows:
        payload = _payload_from_row(row)
        if not payload:
            continue
        repo = _repo_from_cwd(payload.get("cwd", ""))
        if not repo or repo == current_repo:
            continue
        if repo in per_repo:
            continue
        snippet = (payload.get("new_preview") or payload.get("old_preview") or "")[:240]
        per_repo[repo] = {
            "repo": repo,
            "file_path": payload.get("file_path", ""),
            "tool": payload.get("tool", ""),
            "outcome": row["outcome"],
            "timestamp": row["timestamp"],
            "snippet": snippet,
            "event_id": row["id"],
        }
        if len(per_repo) >= limit:
            break
    return list(per_repo.values())


def summarize_for_boot(
    query: str,
    current_repo: str = "",
    brain_db_path: Path | str | None = None,
    limit: int = 3,
) -> list[dict]:
    """Boot-context-shaped wrapper: load BRAIN_DB lazily."""
    if brain_db_path is None:
        from config import BRAIN_DB

        brain_db_path = BRAIN_DB
    return find_analogs(query=query, current_repo=current_repo, brain_db_path=brain_db_path, limit=limit)


__all__ = ["find_analogs", "summarize_for_boot"]
