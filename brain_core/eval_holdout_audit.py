"""brain_core/eval_holdout_audit.py - weekly Telegram digest of pending eval candidates (Phase C2).

Reads cli/eval_holdout_pending.json, builds a Telegram digest via openclaw_dispatch
to Jenna with approve/reject URLs (POST /brain/eval-proposals/{id}/approve|reject),
and waits for Chris to act via the existing API endpoints. Approved items are
NOT auto-appended to eval_set.json — that's a separate manual step the API does.

Schedule: Sun 9:15am (after eval_holdout_promote at 8:45).

Manual gate preserved: Chris must approve via Telegram tap or Brain UI.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.eval_holdout_audit")

try:
    from config import BRAIN_DIR
except ImportError:
    BRAIN_DIR = Path("/Users/chrischo/server/brain")


PENDING_PATH = BRAIN_DIR / "cli" / "eval_holdout_pending.json"

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_ACCOUNT = "jenna-bot"
BRAIN_URL = "http://127.0.0.1:8791"


def _build_digest(items: list[dict]) -> str:
    if not items:
        return "[BRAIN EVAL] No pending eval holdout candidates this week."

    lines = [f"[BRAIN EVAL] {len(items)} pending eval candidate(s) — review:"]
    for i, item in enumerate(items, start=1):
        novelty = item.get("novelty", 0)
        query = (item.get("query") or "")[:120]
        expected = (item.get("expected") or "")[:80]
        pid = item.get("id")
        lines.append("")
        lines.append(f"#{i} (novelty {novelty:.2f}) id={pid}")
        lines.append(f"Q: {query}")
        lines.append(f"A: {expected}")
        lines.append(f"approve: {BRAIN_URL}/brain/eval-proposals/{pid}/approve")
        lines.append(f"reject:  {BRAIN_URL}/brain/eval-proposals/{pid}/reject")
    return "\n".join(lines)


def _send_telegram(message: str) -> bool:
    if not Path(OPENCLAW_BIN).exists():
        log.warning("openclaw binary missing at %s — skipping telegram", OPENCLAW_BIN)
        return False
    try:
        result = subprocess.run(
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
                message,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("telegram dispatch failed: %s", exc)
        return False


def run() -> dict:
    if not PENDING_PATH.exists():
        return {"sent": False, "items": 0, "reason": "no pending file"}
    try:
        items = json.loads(PENDING_PATH.read_text())
    except Exception as exc:
        return {"sent": False, "items": 0, "error": str(exc)[:200]}

    digest = _build_digest(items)
    sent = _send_telegram(digest)
    return {"sent": sent, "items": len(items), "digest_length": len(digest)}


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(), indent=2) + "\n")
