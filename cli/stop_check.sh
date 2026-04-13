#!/usr/bin/env bash
# Stop hook: nudge Claude to check for remaining work before stopping.
# stdin: JSON with stop_hook_active flag.
# exit 0 = allow stop (but stdout injects context).
# exit 2 = block stop.

set -euo pipefail

INPUT=$(cat)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')

# If this is a re-fire after blocking, allow stop to prevent infinite loop.
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

# Inject a nudge — Claude will continue if there's actually remaining work.
echo "Before stopping: check your task list. If there is remaining work, keep going."
exit 0
