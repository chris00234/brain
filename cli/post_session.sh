#!/bin/bash
# Claude Code SessionEnd hook (v2) — thin outbox spooler.
#
# Differences from v1 (post_session.sh):
#   - Writes a JSONL envelope to ~/.openclaw/outbox/brain-learn/pending/<sid>.jsonl
#   - Returns in ~50 ms (no blocking HTTP, no transcript parse)
#   - Drainer (cli/outbox_drain.py) handles upload + retries asynchronously
#   - Failures persist across brain outages (no transcript loss)
#
# Cutover: `mv post_session_v2.sh post_session.sh` once verified.

set -uo pipefail

LOG=/Users/chrischo/.openclaw/logs/post-session.log
OUTBOX=/Users/chrischo/.openclaw/outbox/brain-learn/pending
DRAIN_SCRIPT=/Users/chrischo/server/brain/cli/outbox_drain.py
BRAIN_PY="${BRAIN_PYTHON:-/Users/chrischo/server/brain/.venv/bin/python}"

mkdir -p "$(dirname "$LOG")" "$OUTBOX"

PAYLOAD="$(cat)"

# Write envelope to outbox using Python (handles JSON safely, no shell quoting risk).
PAYLOAD_VAR="$PAYLOAD" "$BRAIN_PY" - <<'PY' 2>>"$LOG"
import json
import os
import time
import uuid
from pathlib import Path

OUTBOX = Path("/Users/chrischo/.openclaw/outbox/brain-learn/pending")
OUTBOX.mkdir(parents=True, exist_ok=True)

raw = os.environ.get("PAYLOAD_VAR", "")
try:
    hook = json.loads(raw) if raw else {}
except Exception:
    hook = {}

sid = hook.get("session_id") or f"unknown-{uuid.uuid4().hex[:8]}"
tpath = hook.get("transcript_path") or ""

# Resolve transcript path if missing
if not tpath or not Path(tpath).exists():
    project_dir = Path("/Users/chrischo/.claude/projects/-Users-chrischo")
    candidate = project_dir / f"{sid}.jsonl"
    if candidate.exists():
        tpath = str(candidate)
    elif project_dir.exists():
        # Fall back to most recent jsonl
        try:
            jsonls = sorted(
                project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if jsonls:
                tpath = str(jsonls[0])
        except Exception:
            pass

envelope = {
    "session_id": sid,
    "transcript_path": tpath,
    "enqueued_ts": time.time(),
    "retries": 0,
    "next_attempt_ts": time.time(),
    "schema_version": 1,
}

# Atomic write: <sid>.jsonl.tmp → rename
target = OUTBOX / f"{sid}.jsonl"
tmp = target.with_suffix(".jsonl.tmp")
tmp.write_text(json.dumps(envelope) + "\n")
tmp.rename(target)
print(f"[ok] enqueued {sid}")
PY

echo "$(date -Iseconds) enqueued session" >> "$LOG"

# Kick the drainer best-effort. Drainer is idempotent and safe to spawn concurrently.
( "$BRAIN_PY" "$DRAIN_SCRIPT" >> "$LOG" 2>&1 & ) >/dev/null 2>&1 || true

exit 0
