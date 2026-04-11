#!/opt/homebrew/bin/python3
"""batch_learn.py — feed past Claude Code session transcripts through POST /learn.

Rebuilds semantic_memory from historical sessions after a data wipe.
Each session gets chunked into ~4000-char blocks (the /learn transcript limit
is 50KB but Jenna distillation works better on focused chunks). Only user
messages are included — assistant tool calls are noise for preference extraction.

Usage:
  batch_learn.py                     # process all sessions, skip already-processed
  batch_learn.py --limit 10          # process only 10 sessions
  batch_learn.py --dry-run           # show what would be processed
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

SESSIONS_DIR = Path("/Users/chrischo/.claude/projects/-Users-chrischo")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/.batch_learn_state.json")
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BRAIN_URL = "http://127.0.0.1:8791/learn"
CHUNK_SIZE = 4000
MIN_TRANSCRIPT_LEN = 200
DELAY_BETWEEN_SESSIONS = 5  # seconds — don't hammer Jenna


def _load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_state(processed: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(processed)))


def _extract_user_messages(session_path: Path) -> str:
    """Extract user messages from a Claude Code session JSONL."""
    lines: list[str] = []
    try:
        for raw_line in session_path.open():
            try:
                rec = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            role = msg.get("role") or rec.get("type") or ""
            if role not in ("user", "human"):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            elif isinstance(content, str):
                text = content
            else:
                continue
            text = text.strip()
            if len(text) > 20:
                lines.append(f"Chris: {text[:2000]}")
    except Exception:
        pass
    return "\n\n".join(lines)


def _post_learn(transcript: str, source: str, token: str) -> dict:
    body = json.dumps({
        "transcript": transcript[:50000],
        "source": source,
        "agent": "claude",
    }).encode()
    req = urllib.request.Request(
        BRAIN_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-feed sessions to /learn")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = SECRET_FILE.read_text().strip()
    processed = _load_state()

    sessions = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    print(f"Found {len(sessions)} sessions, {len(processed)} already processed")

    to_process = [s for s in sessions if s.name not in processed]
    if args.limit > 0:
        to_process = to_process[:args.limit]

    total_queued = 0
    for i, session in enumerate(to_process, 1):
        transcript = _extract_user_messages(session)
        if len(transcript) < MIN_TRANSCRIPT_LEN:
            print(f"  [{i}/{len(to_process)}] {session.name}: too short ({len(transcript)} chars), skip")
            processed.add(session.name)
            continue

        # Chunk large transcripts
        chunks = [transcript[j:j + CHUNK_SIZE] for j in range(0, len(transcript), CHUNK_SIZE)]
        print(f"  [{i}/{len(to_process)}] {session.name}: {len(transcript)} chars, {len(chunks)} chunks", end="")

        if args.dry_run:
            print(" (dry-run)")
            continue

        for ci, chunk in enumerate(chunks):
            result = _post_learn(chunk, f"batch_learn:{session.stem}:{ci}", token)
            candidates = result.get("candidates", 0)
            total_queued += candidates
            print(f" c{ci}={candidates}", end="")
            if ci < len(chunks) - 1:
                time.sleep(1)
        print()

        processed.add(session.name)
        _save_state(processed)
        time.sleep(DELAY_BETWEEN_SESSIONS)

    print(f"\nDone. {len(to_process)} sessions processed, {total_queued} total candidates queued for distillation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
