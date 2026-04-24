#!/bin/bash
# Claude Code PostToolUse hook — captures coding signal mid-session.
#
# Problem this solves: coding-domain rec accuracy was self-reported at 0%.
# Coding failures only reached brain via the SessionEnd distill, which means
# the hot context (what file, what change, what Chris corrected) was lost to
# summarization cold-cache. This hook fires on every Edit/Write, POSTs the
# structured event to /capture/coding_event so brain has the full provenance
# before the session ends.
#
# Budget: fire-and-forget, <100ms. Never blocks the tool call. Exit 0 always.

set -uo pipefail

BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:8791}"
SECRET_FILE="$HOME/.openclaw/credentials/.personal_webhook_secret"

PAYLOAD=""
if [ ! -t 0 ]; then
  PAYLOAD=$(cat 2>/dev/null || echo "")
fi
[ -z "$PAYLOAD" ] && exit 0

command -v jq >/dev/null 2>&1 || exit 0
[ -r "$SECRET_FILE" ] || exit 0

TOOL_NAME=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
case "$TOOL_NAME" in
  Edit|Write|NotebookEdit) ;;
  *) exit 0 ;;
esac

# Extract the event shape. file_path is required.
FILE_PATH=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.file_path // empty' 2>/dev/null || echo "")
[ -z "$FILE_PATH" ] && exit 0

SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || echo "")
CWD=$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null || echo "")

# For Edit: old_string/new_string give us the diff intent. Truncate to keep
# request body small. For Write: just note the full write.
OLD_STR=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.old_string // empty' 2>/dev/null | head -c 400 || echo "")
NEW_STR=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.new_string // empty' 2>/dev/null | head -c 400 || echo "")

# Success signal — if the tool returned an error, mark it as failed.
SUCCESS=$(printf '%s' "$PAYLOAD" | jq -r '
  if .tool_response.error then "false"
  elif .tool_response.success == false then "false"
  else "true"
  end' 2>/dev/null || echo "true")

NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Build capture payload. /capture/{source_type} writes to raw inbox; the
# canonical pipeline pulls from there. Using a dedicated source_type
# 'coding_event' so future ingest logic can filter cleanly.
HEADER_FILE=$(mktemp -t posttool_coding_hdr_XXXXXX 2>/dev/null) || exit 0
trap 'rm -f "$HEADER_FILE" 2>/dev/null' EXIT HUP INT TERM
{ printf 'Authorization: Bearer '; cat "$SECRET_FILE"; printf '\n'; } > "$HEADER_FILE"
chmod 600 "$HEADER_FILE" 2>/dev/null || true

REQ_BODY=$(jq -nc \
  --arg tool "$TOOL_NAME" \
  --arg fp "$FILE_PATH" \
  --arg ss "$SESSION_ID" \
  --arg cwd "$CWD" \
  --arg ts "$NOW_ISO" \
  --arg old "$OLD_STR" \
  --arg new "$NEW_STR" \
  --arg ok "$SUCCESS" \
  '{
    tool: $tool,
    file_path: $fp,
    session_id: $ss,
    cwd: $cwd,
    ts: $ts,
    old_preview: $old,
    new_preview: $new,
    success: ($ok == "true")
  }' 2>/dev/null || echo "")
[ -z "$REQ_BODY" ] && exit 0

# Fire-and-forget. 800ms is plenty — we're just POSTing to localhost. If
# brain is down we just lose this event; the old SessionEnd path still fires.
curl -sS --max-time 0.8 \
  -H "@$HEADER_FILE" \
  -H "Content-Type: application/json" \
  --data "$REQ_BODY" \
  "${BRAIN_URL}/capture/coding_event" >/dev/null 2>&1 || true

exit 0
