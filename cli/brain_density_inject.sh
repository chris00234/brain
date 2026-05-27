#!/bin/bash
# UserPromptSubmit hook: consume the brain-density warning flag from the
# previous Stop hook (if any) and inject it as additionalContext for this
# turn. One-shot: file is deleted after read.
#
# Designed to run AFTER claude_boot.sh in the same UserPromptSubmit chain.
# Independent of claude_boot.sh — doesn't share state, doesn't compete.
#
# Output contract: empty stdout on no flag. JSON with hookSpecificOutput
# when a flag is present.

set -uo pipefail

INPUT=""
if [ ! -t 0 ]; then
  INPUT=$(cat 2>/dev/null || echo "")
fi
[ -z "$INPUT" ] && exit 0

command -v jq >/dev/null 2>&1 || exit 0

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // "unknown"' 2>/dev/null || echo "unknown")
FLAG_FILE="/tmp/.brain_density_warning.${SESSION_ID}.txt"

[ ! -r "$FLAG_FILE" ] && exit 0

WARNING=$(cat "$FLAG_FILE" 2>/dev/null || echo "")
[ -z "$WARNING" ] && exit 0

# Consume-once: delete the flag after read.
rm -f "$FLAG_FILE" 2>/dev/null || true

jq -nc --arg w "$WARNING" '{
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext: $w
  }
}' 2>/dev/null || true

exit 0
