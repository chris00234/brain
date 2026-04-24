"""brain_core/claude_memory_regen.py — regenerate MEMORY.md from brain atoms.

After cli/migrate_claude_memory.py moves the auto-memory .md files into
brain.db::atoms + Chroma semantic_memory, the local files at
~/.claude/projects/-Users-chrischo/memory/ stop being the source of truth.
Brain is. But CLAUDE.md can only auto-load local files, not HTTP, so we keep
MEMORY.md and a set of derived stub files on disk as a regenerated cache.

This module does the regeneration:
  1. Query brain.db::atoms for rows with provenance.source starting with
     "claude_auto_memory:".
  2. Reconstruct each source file by joining chunks (if a file was split
     at migration time, chunks are suffixed `#1`, `#2`, etc. in the source
     field — reassemble in order).
  3. Render MEMORY.md as an index linking every known source file with its
     one-line description.
  4. Atomic rename (.tmp → final) so CLAUDE.md auto-load never sees torn
     content.

Trigger: throttled from cli/post_session.sh (SessionEnd hook) at 60 s wall
clock. Also callable from brain_loop or a nightly scheduler job.

60 s throttle: post_session.sh touches /tmp/.claude_memory_regen.ts each run.
If the existing timestamp is less than 60 s old, the current regen is a no-op.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-chrischo" / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
THROTTLE_TS = Path("/tmp/.claude_memory_regen.ts")
THROTTLE_SECONDS = 60

log = logging.getLogger("brain.claude_memory_regen")

SOURCE_PREFIX = "claude_auto_memory:"


def _throttled() -> bool:
    """Return True if a regen happened within THROTTLE_SECONDS."""
    try:
        last = float(THROTTLE_TS.read_text().strip() or 0)
    except (OSError, ValueError):
        return False
    return (time.time() - last) < THROTTLE_SECONDS


def _mark_regen() -> None:
    with contextlib.suppress(OSError):
        THROTTLE_TS.write_text(f"{time.time():.0f}")


def _query_atoms() -> dict[str, list[dict]]:
    """Return {source_file: [chunks ordered by index]} for every claude_auto_memory atom.

    The `provenance_json` field stores {"source": "claude_auto_memory:<name>"} or
    "#<idx>" suffixes for split chunks. We parse and reassemble here.
    """
    files: dict[str, list[dict]] = {}
    if not BRAIN_DB.exists():
        return files
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, text, provenance_json, created_at, updated_at, "
            "       confidence, tier, superseded_by "
            "FROM atoms "
            'WHERE provenance_json LIKE \'%"source":"claude_auto_memory:%\' '
            "   OR provenance_json LIKE '%claude_auto_memory:%' "
            "ORDER BY created_at ASC"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        log.warning("query_atoms failed: %s", e)
        return files

    for row in rows:
        try:
            prov = json.loads(row["provenance_json"] or "{}")
        except json.JSONDecodeError:
            prov = {}
        source = prov.get("source") or ""
        if not source.startswith(SOURCE_PREFIX):
            # Some atoms may only have source_ref or other fields
            for field_name in ("source_ref", "path"):
                val = prov.get(field_name) or ""
                if SOURCE_PREFIX in val:
                    source = val
                    break
            if not source.startswith(SOURCE_PREFIX):
                continue

        # Extract filename and optional chunk index (format: claude_auto_memory:<file>#<n>)
        tail = source[len(SOURCE_PREFIX) :]
        if "#" in tail:
            filename, _, idx = tail.partition("#")
            try:
                chunk_idx = int(idx)
            except ValueError:
                chunk_idx = 1
        else:
            filename, chunk_idx = tail, 1

        # NOTE: We intentionally do NOT filter by superseded_by here.
        # The /memory classify_operation aggressively marks chunks of the
        # SAME file as updates of each other (similar content), so the
        # supersession chain would hide most of each file. Auto-memory
        # chunks are complementary — we want the full set.
        files.setdefault(filename, []).append(
            {
                "atom_id": row["id"],
                "chunk_idx": chunk_idx,
                "text": row["text"] or "",
                "created_at": row["created_at"] or "",
                "updated_at": row["updated_at"] or "",
                "confidence": row["confidence"] or 0.0,
            }
        )

    # Sort chunks within each file. If the same chunk_idx appears multiple
    # times (re-runs of migration → multiple atoms for same #N), keep the
    # most recent one by updated_at.
    for fname, chunks in files.items():
        by_idx: dict[int, dict] = {}
        for c in chunks:
            idx = c["chunk_idx"]
            existing = by_idx.get(idx)
            if existing is None or (c.get("updated_at", "") > existing.get("updated_at", "")):
                by_idx[idx] = c
        files[fname] = [by_idx[i] for i in sorted(by_idx.keys())]
    return files


def _reassemble(chunks: list[dict]) -> tuple[str, str]:
    """Given ordered chunks for a file, produce (name, body).

    Each chunk starts with "# <name>\n\n<body>..." — we take the name from
    chunk 1 and concatenate bodies from all chunks. The frontmatter is
    re-derived from name + description parsed out of the first chunk.
    """
    if not chunks:
        return "", ""
    first = chunks[0]["text"]
    # Extract first-line name if "# " header
    name = ""
    body_parts: list[str] = []
    lines = first.split("\n")
    if lines and lines[0].startswith("# "):
        name = lines[0][2:].strip()
        body_parts.append("\n".join(lines[1:]).strip())
    else:
        body_parts.append(first.strip())

    # Append remaining chunks
    for c in chunks[1:]:
        text = c["text"]
        # Strip repeated header from subsequent chunks
        for pref in (f"# {name}\n\n", f"# {name}\n"):
            if pref and text.startswith(pref):
                text = text[len(pref) :]
                break
        # Strip trailing "(part N/M)" marker
        text = re.sub(r"\s*\(part \d+/\d+\)\s*$", "", text).strip()
        body_parts.append(text)

    # Strip trailing part marker from first chunk too
    body_parts[0] = re.sub(r"\s*\(part \d+/\d+\)\s*$", "", body_parts[0]).strip()

    body = "\n\n".join(p for p in body_parts if p)
    return name, body


def _render_stub(name: str, body: str) -> str:
    """Render a single stub .md file with frontmatter + body."""
    safe_name = name or "untitled"
    # Pull a description from the first non-empty non-header line
    desc = ""
    for line in body.split("\n"):
        ln = line.strip()
        if ln and not ln.startswith("#"):
            desc = ln[:200]
            break
    return (
        "---\n"
        f"name: {safe_name}\n"
        f"description: {desc}\n"
        "source: brain_atoms (regenerated)\n"
        f"generated_at: {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        "---\n\n"
        f"# {safe_name}\n\n"
        f"{body}\n"
    )


def _render_index(files: dict[str, tuple[str, str]]) -> str:
    """Render MEMORY.md as a pointer index + verified preferences sections.

    Groups files by semantic prefix (feedback/project/user/openclaw).
    """
    out: list[str] = ["# Claude Code Auto-Memory", ""]
    out.append("> Regenerated from brain atoms. Brain is the source of truth — ")
    out.append("> edits here get re-imported on next session with " "`provenance.reason=manual_edit`.")
    out.append("")
    out.append("## Index")

    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for fname, (name, body) in sorted(files.items()):
        # Pick group by filename prefix
        if fname.startswith("feedback_") or fname.startswith("feedback-"):
            group = "Corrections & Learnings"
        elif fname.startswith("project_"):
            group = "Project Context"
        elif fname.startswith("user_"):
            group = "User Profile"
        elif fname.startswith("openclaw"):
            group = "OpenClaw"
        else:
            group = "Other"
        desc = ""
        for line in body.split("\n"):
            if line.strip() and not line.startswith("#"):
                desc = line.strip()[:140]
                break
        grouped.setdefault(group, []).append((fname, name or fname, desc))

    group_order = ["User Profile", "Corrections & Learnings", "Project Context", "OpenClaw", "Other"]
    for group in group_order:
        entries = grouped.get(group, [])
        if not entries:
            continue
        out.append("")
        out.append(f"## {group}")
        for fname, display, desc in entries:
            out.append(f"- [{display}]({fname}) — {desc}")

    out.append("")
    out.append("---")
    out.append(f"*Regenerated {datetime.now(UTC).isoformat(timespec='seconds')} from brain.db atoms*")
    return "\n".join(out) + "\n"


def run(force: bool = False) -> dict:
    """Public entry point — regenerate MEMORY.md and derived stubs.

    Returns {"status": "ok|throttled|error", "files_written": N, "duration_ms": ms}.
    """
    t0 = time.time()
    if not force and _throttled():
        return {"status": "throttled", "files_written": 0, "duration_ms": 0}

    files = _query_atoms()
    if not files:
        return {
            "status": "no_atoms",
            "files_written": 0,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    assembled: dict[str, tuple[str, str]] = {}
    for fname, chunks in files.items():
        try:
            name, body = _reassemble(chunks)
            if not body:
                continue
            assembled[fname] = (name, body)

            stub = _render_stub(name, body)
            target = MEMORY_DIR / fname
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(stub)
            tmp.rename(target)
            written += 1
        except Exception as e:
            log.warning("regen %s failed: %s", fname, e)
            continue

    # Render index last
    try:
        index = _render_index(assembled)
        tmp = MEMORY_INDEX.with_suffix(".md.tmp")
        tmp.write_text(index)
        tmp.rename(MEMORY_INDEX)
        written += 1
    except OSError as e:
        log.warning("regen MEMORY.md failed: %s", e)

    _mark_regen()
    return {
        "status": "ok",
        "files_written": written,
        "atoms_seen": sum(len(v) for v in files.values()),
        "files_assembled": len(assembled),
        "duration_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Regenerate MEMORY.md from brain atoms.")
    parser.add_argument("--force", action="store_true", help="Skip 60s throttle")
    args = parser.parse_args()
    result = run(force=args.force)
    print(json.dumps(result, indent=2))
