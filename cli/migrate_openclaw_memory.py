#!/usr/bin/env python3
"""migrate_openclaw_memory.py — one-time absorb OpenClaw per-agent memory
sqlites (~40 MB, 277 chunks across 5 agents) into brain's unified store.

Source schema (uniform across jenna/liz/ellie/sage/main.sqlite):
  files  (path, source, hash, mtime, size)
  chunks (id, path, source, start_line, end_line, hash, model, text, embedding, updated_at)

The existing embeddings are `gemini-embedding-001` which is a different model +
dimensionality than brain's `multilingual-e5-large-instruct`, so we CANNOT
reuse them — each chunk has to be re-embedded via brain's Ollama.

Strategy: for each chunk, POST to /memory with agent=<source_agent>, category='fact',
source='openclaw_memory:<agent>:<path>#L<start>-<end>'. This reuses the entire
/memory ingest pipeline (embedding + Chroma write + atom mirror + supersession).

The source sqlites are preserved at ~/.openclaw/memory/_migrated_<DATE>/ for 30
days before deletion, so rollback is a copy-back.

Usage:
  cli/migrate_openclaw_memory.py                    # dry run, prints what would happen
  cli/migrate_openclaw_memory.py --apply            # actually POST (~10 min with rate limit)
  cli/migrate_openclaw_memory.py --apply --agent liz  # restrict to one agent

Idempotent: /memory classify_operation dedups identical text within the
semantic_memory collection. Re-runs NOOP on chunks already migrated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OPENCLAW_MEMORY_DIR = Path.home() / ".openclaw" / "memory"
BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = Path.home() / ".openclaw" / "credentials" / ".personal_webhook_secret"
MIGRATION_LOG = Path("/Users/chrischo/server/brain/logs/openclaw_memory_migration.json")

RATE_LIMIT_SLEEP_S = 2.2  # stay under /memory 30/minute
MAX_CONTENT = 1900
KNOWN_AGENTS = ["jenna", "liz", "ellie", "sage", "main"]  # main = shared/system


def _load_secret() -> str:
    try:
        return SECRET_FILE.read_text().strip()
    except OSError:
        return ""


def _chunks_for_agent(agent: str) -> list[dict]:
    """Read all chunks from a single agent sqlite. Returns list of dicts."""
    db_path = OPENCLAW_MEMORY_DIR / f"{agent}.sqlite"
    if not db_path.exists():
        return []
    out: list[dict] = []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, path, source, start_line, end_line, hash, text "
            "FROM chunks ORDER BY path, start_line"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"{agent}: sqlite read error: {e}", file=sys.stderr)
        return []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "path": r["path"],
                "source": r["source"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "hash": r["hash"],
                "text": r["text"],
            }
        )
    return out


def _post_memory(content: str, category: str, source: str, agent: str, secret: str) -> dict:
    req = urllib.request.Request(
        f"{BRAIN_URL}/memory",
        data=json.dumps(
            {
                "content": content,
                "category": category,
                "agent": agent,
                "source": source,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "x-agent": agent,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return {"status": "ok", "code": resp.status, "body": json.loads(body)}
    except urllib.error.HTTPError as e:
        return {"status": "http_error", "code": e.code, "body": e.read().decode()[:500]}
    except Exception as e:
        return {"status": "error", "error": str(e)[:500]}


def _truncate(text: str, limit: int = MAX_CONTENT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + " ...(truncated)"


def _compose_content(chunk: dict) -> str:
    """Combine path + line range + chunk text for good embedding signal."""
    path_label = chunk["path"].replace(str(Path.home()), "~")
    header = f"# {path_label} L{chunk['start_line']}-{chunk['end_line']}"
    body = chunk["text"]
    return _truncate(f"{header}\n\n{body}")


def _backup_source_dbs(agents: list[str]) -> Path:
    """Copy source sqlites to _migrated_<DATE>/ for rollback."""
    backup_dir = OPENCLAW_MEMORY_DIR / f"_migrated_{datetime.now(UTC).strftime('%Y_%m_%d')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for agent in agents:
        src = OPENCLAW_MEMORY_DIR / f"{agent}.sqlite"
        if src.exists():
            shutil.copy2(src, backup_dir / f"{agent}.sqlite")
    return backup_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate OpenClaw memory sqlites into brain atoms.")
    parser.add_argument("--apply", action="store_true", help="Actually POST to /memory (default: dry run)")
    parser.add_argument(
        "--agent",
        choices=[*KNOWN_AGENTS, "all"],
        default="all",
        help="Restrict to a single agent (default: all)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max chunks to process per agent (0 = all)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    agents = KNOWN_AGENTS if args.agent == "all" else [args.agent]
    secret = _load_secret() if args.apply else ""
    if args.apply and not secret:
        print("credentials missing; cannot POST", file=sys.stderr)
        return 1

    # Load chunks
    all_chunks: list[tuple[str, dict]] = []  # (agent, chunk)
    for agent in agents:
        chunks = _chunks_for_agent(agent)
        if args.limit:
            chunks = chunks[: args.limit]
        for c in chunks:
            all_chunks.append((agent, c))

    if not all_chunks:
        print(f"no chunks found for {agents}", file=sys.stderr)
        return 1

    total = len(all_chunks)
    est_seconds = total * RATE_LIMIT_SLEEP_S
    print(f"{total} chunks across {len(set(a for a, _ in all_chunks))} agent(s), est {est_seconds:.0f}s")

    # Backup source sqlites before writing anything
    if args.apply:
        backup_dir = _backup_source_dbs(agents)
        print(f"backed up source sqlites to {backup_dir}")

    log_entries: list[dict] = []
    sent = 0
    failed = 0

    for idx, (agent, chunk) in enumerate(all_chunks, 1):
        content = _compose_content(chunk)
        source = f"openclaw_memory:{agent}:{chunk['path']}#L{chunk['start_line']}-{chunk['end_line']}"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        entry = {
            "idx": idx,
            "agent": agent,
            "path": chunk["path"],
            "lines": f"{chunk['start_line']}-{chunk['end_line']}",
            "source": source,
            "content_hash": content_hash,
            "content_len": len(content),
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        }

        if args.verbose or not args.apply:
            print(
                f"{'[DRY]' if not args.apply else '[POST]'} [{idx:3d}/{total}] {agent:6s} "
                f"{chunk['path'][-50:]:50s} L{chunk['start_line']}-{chunk['end_line']} len={len(content)}"
            )

        if args.apply:
            result = _post_memory(content, "fact", source, agent, secret)
            entry["result"] = result
            if result.get("status") == "ok":
                sent += 1
            else:
                failed += 1
                err_preview = str(result.get("body") or result.get("error") or result)[:200]
                print(f"FAIL [{idx}/{total}] {agent}/{chunk['path']}: {err_preview}", file=sys.stderr)
            time.sleep(RATE_LIMIT_SLEEP_S)

        log_entries.append(entry)

    if args.apply:
        MIGRATION_LOG.parent.mkdir(parents=True, exist_ok=True)
        MIGRATION_LOG.write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "agents": agents,
                    "total": total,
                    "sent": sent,
                    "failed": failed,
                    "entries": log_entries,
                },
                indent=2,
            )
        )
        print(f"\nMigration complete: {sent}/{total} sent, {failed} failed")
        print(f"Log: {MIGRATION_LOG}")
    else:
        print(f"\nDry run: {total} chunks would be migrated. Re-run with --apply to commit.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
