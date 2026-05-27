#!/bin/bash
# Stop hook: detect passive brain use this turn. If the assistant made
# decision-class tool_uses (Edit/Write to config/code, Bash with config-set
# or infra commands, brain.db queries, etc.) WITHOUT any matching brain_*
# MCP calls, write a warn-only flag for the next UserPromptSubmit to surface.
#
# Warn-only by design (Chris asked for warn, not block). Never blocks the
# stop. Exit 0 always.
#
# Pattern: same file-flag approach as the existing doorbell, but a separate
# file so we don't clash with brain_loop's urgent content channel.

set -uo pipefail

INPUT=$(cat 2>/dev/null || echo "")
[ -z "$INPUT" ] && exit 0

command -v jq >/dev/null 2>&1 || exit 0

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // "unknown"' 2>/dev/null || echo "unknown")
TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || echo "")
[ -z "$TRANSCRIPT" ] && exit 0
[ ! -r "$TRANSCRIPT" ] && exit 0

FLAG_FILE="/tmp/.brain_density_warning.${SESSION_ID}.txt"

# Count tool_uses from the assistant's LAST turn (since the last user message).
# The transcript JSONL contains records like {"type":"assistant","message":{"content":[{"type":"tool_use","name":"..."}]}}
# Filter to last turn: lines after the most recent {"type":"user"} record.
#
# Decision-class tool names:
#   - Edit, Write, NotebookEdit (file modifications)
#   - Bash with config-set / install / disable / brain.db / openclaw / docker-compose patterns
#   - TodoWrite marking tasks complete (signals decisions made)
# Brain calls:
#   - mcp__brain__brain_recall, mcp__brain__brain_store, mcp__brain__brain_decide,
#     mcp__brain__brain_correct, mcp__brain__brain_reason, mcp__brain__brain_outcome
#
# This is heuristic — false positives are fine (warn-only).

LAST_TURN=$(awk '/"type":"user"/{out=""} {out=out"\n"$0} END{print out}' "$TRANSCRIPT" 2>/dev/null || echo "")
[ -z "$LAST_TURN" ] && exit 0

# Count tool_use names in this turn.
TOOL_NAMES=$(printf '%s' "$LAST_TURN" | jq -rs '
  [.[]
   | select(.type == "assistant")
   | (.message.content // [])
   | .[]?
   | select(.type == "tool_use")
   | .name
  ] | .[]
' 2>/dev/null || echo "")
[ -z "$TOOL_NAMES" ] && exit 0

DECISION_COUNT=0
BRAIN_COUNT=0
DECISION_SAMPLES=""

while IFS= read -r name; do
  case "$name" in
    Edit|Write|NotebookEdit)
      DECISION_COUNT=$((DECISION_COUNT + 1))
      [ ${#DECISION_SAMPLES} -lt 100 ] && DECISION_SAMPLES="${DECISION_SAMPLES}${name} "
      ;;
    mcp__brain__brain_recall|mcp__brain__brain_store|mcp__brain__brain_decide|mcp__brain__brain_correct|mcp__brain__brain_reason|mcp__brain__brain_outcome|mcp__brain__brain_doubt)
      BRAIN_COUNT=$((BRAIN_COUNT + 1))
      ;;
  esac
done <<< "$TOOL_NAMES"

# Also count "decision-class" Bash commands by scanning the command field.
BASH_DECISIONS=$(printf '%s' "$LAST_TURN" | jq -rs '
  [.[]
   | select(.type == "assistant")
   | (.message.content // [])
   | .[]?
   | select(.type == "tool_use" and .name == "Bash")
   | (.input.command // "")
  ] | .[]
' 2>/dev/null | grep -cE 'config set|--install|--disable|--enable|brain\.db|autonomy\.db|openclaw config|launchctl (load|unload|bootstrap)|systemctl (enable|disable|start|stop)|docker-compose|orb config' || echo 0)
DECISION_COUNT=$((DECISION_COUNT + BASH_DECISIONS))

# Also count direct HTTP brain calls (curl to /memory or /recall) — these count
# as active brain use even though they're not MCP.
HTTP_BRAIN=$(printf '%s' "$LAST_TURN" | jq -rs '
  [.[]
   | select(.type == "assistant")
   | (.message.content // [])
   | .[]?
   | select(.type == "tool_use" and .name == "Bash")
   | (.input.command // "")
  ] | .[]
' 2>/dev/null | grep -cE '127\.0\.0\.1:8791/(memory|recall|brain/decisions|brain/correct)' || echo 0)
BRAIN_COUNT=$((BRAIN_COUNT + HTTP_BRAIN))

# Density rule: if >= 3 decisions and 0 brain calls, warn.
# Tunable — keep generous to avoid false-positive nag.
THRESHOLD=3

if [ "$DECISION_COUNT" -ge "$THRESHOLD" ] && [ "$BRAIN_COUNT" -eq 0 ]; then
  cat > "$FLAG_FILE" <<EOF
[Brain density warning — previous turn]
You made $DECISION_COUNT decision-class tool_uses (Edit/Write/config-set) with **zero** brain_* calls. CLAUDE.md's "Active vs passive use" rule mandates:
  - brain_recall BEFORE architectural decisions
  - brain_store at sharp inflections (corrections, discoveries, config changes)
  - brain_decide for close architectural choices
  - brain_correct on Chris's explicit corrections

If MCP brain_store times out (5s), POST /memory via HTTP fallback. Don't drop the store.

(This warning auto-clears on next turn.)
EOF
fi

exit 0
