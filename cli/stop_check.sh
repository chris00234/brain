#!/usr/bin/env bash
# Stop hook: nudge Claude to check for remaining work before stopping.
# v3 addition: also check the brain doorbell file. If brain_loop has queued
# urgent context for this session, inject it into the Stop output so Claude
# sees it before terminating the turn.
#
# stdin: JSON with stop_hook_active flag + session_id.
# exit 0 = allow stop (but stdout injects context).
# exit 2 = block stop (Claude continues).

set -euo pipefail

INPUT=$(cat)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // .sessionId // "unknown"')

# Background cleanup of stale doorbell + last-stop sentinel files older than
# 24h. Sessions don't garbage-collect their own /tmp footprint, so the dir
# accumulates 30+ stale files over a few days. Backgrounded so the hook never
# blocks the stop on disk I/O.
( find /tmp -maxdepth 1 -name '.brain_doorbell.*.jsonl' -mtime +1 -delete 2>/dev/null
  find /tmp -maxdepth 1 -name '.claude_last_stop.*' -mtime +1 -delete 2>/dev/null
) >/dev/null 2>&1 &
disown 2>/dev/null || true

# If this is a re-fire after blocking, allow stop to prevent infinite loop.
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

# v3: Brain doorbell check. If brain_loop has queued urgent content for this
# session, print it and block the stop so Claude sees it before terminating.
# Gated by a per-session "last stop ts" sentinel to prevent infinite loops if
# the doorbell content can't be resolved.
DOORBELL="/tmp/.brain_doorbell.${SESSION_ID}.jsonl"
LAST_STOP="/tmp/.claude_last_stop.${SESSION_ID}"
NOW=$(date +%s)

if [ -f "$DOORBELL" ]; then
  DOORBELL_MTIME=$(stat -f %m "$DOORBELL" 2>/dev/null || echo 0)
  LAST_STOP_TS=$(cat "$LAST_STOP" 2>/dev/null || echo 0)

  # Only block if the doorbell was written AFTER the last stop attempt on this session.
  # Otherwise we'd loop forever on stale doorbell content.
  if [ "$DOORBELL_MTIME" -gt "$LAST_STOP_TS" ]; then
    echo "$NOW" > "$LAST_STOP"
    echo "⚠ Brain has urgent context queued for this session — do not stop yet."
    if command -v jq >/dev/null 2>&1; then
      jq -r '
        "",
        "### 🔔 \(.title // "Brain Doorbell") [\(.priority // "medium")] — \(.source // "brain_loop")",
        (.content // ""),
        ""
      ' "$DOORBELL" 2>/dev/null || cat "$DOORBELL"
    else
      echo
      cat "$DOORBELL"
      echo
    fi
    echo "(Doorbell will clear on next UserPromptSubmit turn.)"
    # exit 2 = block stop, Claude continues and will see the doorbell on the
    # next turn via claude_boot.sh which also clears it.
    exit 2
  fi
fi

# No doorbell → emit JSON with suppressOutput=true so the Claude Code TUI
# doesn't render a "No stderr output" feedback line between turns (which
# otherwise overwrites short final assistant messages on the terminal).
# The prior silent `exit 0` should have been invisible per docs, but the
# harness surfaces a debug annotation on empty hooks; explicit
# suppressOutput keeps the happy path truly silent.
printf '{"suppressOutput": true}\n'
exit 0
