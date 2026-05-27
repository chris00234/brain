#!/opt/homebrew/bin/python3
"""batch_learn_openclaw.py — feed historical OpenClaw agent sessions through POST /learn.

Ingests Telegram conversations from all 5 agents (Jenna, Liz, Ellie, Sage, Market)
so the brain learns from months of pre-brain decisions, corrections, and preferences.

Skips:
- .deleted session files
- Sessions with < 200 chars of user content
- Cron-triggered sessions (automated, not conversational)
- Already-processed sessions (tracked in state file)

Usage:
  batch_learn_openclaw.py                  # process all agents
  batch_learn_openclaw.py --agent jenna    # single agent
  batch_learn_openclaw.py --dry-run        # preview only
  batch_learn_openclaw.py --limit 10       # first 10 sessions
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

AGENTS_DIR = Path("/Users/chrischo/.openclaw/agents")
AGENT_NAMES = ["jenna", "liz", "ellie", "sage", "market"]
STATE_FILE = Path("/Users/chrischo/server/brain/logs/.batch_learn_openclaw_state.json")
SECRET_FILE = Path("/Users/chrischo/.brain/credentials/.personal_webhook_secret")
BRAIN_URL = "http://127.0.0.1:8791/learn"
CHUNK_SIZE = 4000
MIN_TRANSCRIPT_LEN = 200
DELAY_BETWEEN_SESSIONS = 5


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
    lines: list[str] = []
    try:
        for raw_line in session_path.open():
            try:
                d = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            # Skip non-message records
            if d.get("type") != "message":
                continue

            msg = d.get("message", {})
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")
            if role != "user":
                continue

            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                )
            elif isinstance(content, str):
                text = content
            else:
                continue

            text = text.strip()
            if len(text) < 20:
                continue

            # Skip cron-triggered messages (automated, not conversational)
            if text.startswith("[cron:"):
                continue

            lines.append(f"Chris: {text[:2000]}")
    except Exception:
        pass
    return "\n\n".join(lines)


def _post_learn(transcript: str, source: str, agent: str, token: str) -> dict:
    body = json.dumps(
        {
            "transcript": transcript[:50000],
            "source": source,
            "agent": agent,
        }
    ).encode()
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
    parser = argparse.ArgumentParser(description="Batch-feed OpenClaw sessions to /learn")
    parser.add_argument("--agent", choices=AGENT_NAMES, help="Single agent only")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = SECRET_FILE.read_text().strip()
    processed = _load_state()
    agents = [args.agent] if args.agent else AGENT_NAMES

    # Collect all session files across agents
    sessions: list[tuple[str, Path]] = []
    for agent in agents:
        session_dir = AGENTS_DIR / agent / "sessions"
        if not session_dir.exists():
            continue
        for f in sorted(session_dir.glob("*.jsonl")):
            if ".deleted" in f.name:
                continue
            key = f"{agent}:{f.name}"
            if key not in processed:
                sessions.append((agent, f))

    if args.limit > 0:
        sessions = sessions[: args.limit]

    print(
        f"Found {len(sessions)} unprocessed sessions across {len(agents)} agents, {len(processed)} already done"
    )

    total_queued = 0
    for i, (agent, session) in enumerate(sessions, 1):
        transcript = _extract_user_messages(session)
        key = f"{agent}:{session.name}"

        if len(transcript) < MIN_TRANSCRIPT_LEN:
            print(
                f"  [{i}/{len(sessions)}] {agent}/{session.name}: too short ({len(transcript)} chars), skip"
            )
            processed.add(key)
            continue

        chunks = [transcript[j : j + CHUNK_SIZE] for j in range(0, len(transcript), CHUNK_SIZE)]
        print(
            f"  [{i}/{len(sessions)}] {agent}/{session.name}: {len(transcript)} chars, {len(chunks)} chunks",
            end="",
        )

        if args.dry_run:
            print(" (dry-run)")
            continue

        for ci, chunk in enumerate(chunks):
            result = _post_learn(chunk, f"openclaw_session:{agent}:{session.stem}:{ci}", agent, token)
            candidates = result.get("candidates", 0)
            total_queued += candidates
            print(f" c{ci}={candidates}", end="")
            if ci < len(chunks) - 1:
                time.sleep(1)
        print()

        processed.add(key)
        _save_state(processed)
        time.sleep(DELAY_BETWEEN_SESSIONS)

    print(f"\nDone. {len(sessions)} sessions processed, {total_queued} total candidates queued.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
