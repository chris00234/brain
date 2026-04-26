#!/bin/bash
# Codex CLI UserPromptSubmit hook — active recall per turn.
#
# Mirrors claude_boot.sh but trimmed: no first-turn heavy boot_context, no
# 5-min payload cache. Codex sessions are often short-lived, and oh-my-codex
# already ships its own orchestration layer; this hook's single job is to
# inject brain's intent-routed recall (canonical + semantic) plus direct
# doorbell delivery before each Codex turn so Codex sees Chris's preferences
# and recent decisions the same way Claude Code does.
#
# stdin: hook JSON envelope with session_id + prompt + cwd.
# stdout: <system-reminder>…</system-reminder> block injected into context.
# Hard 1.5s wall-clock budget on the recall call. Fail-open on any error.

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

SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '(.session_id // .sessionId // "") | tostring | .[0:128]' 2>/dev/null || echo "")
PROMPT=$(printf '%s' "$PAYLOAD" | jq -r '(.prompt // .user_message // "") | tostring | .[0:8000]' 2>/dev/null || echo "")
CWD_RAW=$(printf '%s' "$PAYLOAD" | jq -r '(.cwd // "") | tostring | .[0:512]' 2>/dev/null || echo "")
[ -z "$SESSION_ID" ] && SESSION_ID="codex-$$"

# Turn counter scoped to session id. Codex doesn't guarantee the same session
# id across restarts so this is best-effort.
TURN_FILE="/tmp/.codex_turn_${SESSION_ID}"
TURN_IDX=$(cat "$TURN_FILE" 2>/dev/null || echo 0)
TURN_IDX=$(printf '%s' "$TURN_IDX" | tr -d '[:space:]')
[[ "$TURN_IDX" =~ ^[0-9]+$ ]] || TURN_IDX=0
echo $((TURN_IDX + 1)) > "$TURN_FILE"

HEADER_FILE=$(mktemp -t codex_boot_hdr_XXXXXX)
trap 'rm -f "$HEADER_FILE" 2>/dev/null' EXIT HUP INT TERM
{ printf 'Authorization: Bearer '; cat "$SECRET_FILE"; printf '\n'; } > "$HEADER_FILE"
chmod 600 "$HEADER_FILE"

REQ_BODY=$(jq -nc \
  --arg prompt "$PROMPT" \
  --arg session_id "$SESSION_ID" \
  --argjson turn_idx "$TURN_IDX" \
  --arg agent "codex" \
  --arg cwd "$CWD_RAW" \
  '{prompt: $prompt, session_id: $session_id, turn_idx: $turn_idx, agent: $agent, cwd: $cwd}')

RESP=$(curl -sS --max-time 1.5 \
  -H @"$HEADER_FILE" \
  -H 'Content-Type: application/json' \
  -d "$REQ_BODY" \
  "${BRAIN_URL}/recall/active" 2>/dev/null || echo "")

BLOCK_COUNT=0
if [ -n "$RESP" ]; then
  BLOCK_COUNT=$(printf '%s' "$RESP" | jq '[.blocks[]? | select(((.source // "") | startswith("doorbell")) | not)] | length' 2>/dev/null || echo 0)
fi
[[ "$BLOCK_COUNT" =~ ^[0-9]+$ ]] || BLOCK_COUNT=0

# Format blocks into a <system-reminder> block. Codex treats
# UserPromptSubmit stdout as trailing context on the current turn.
if [ "$BLOCK_COUNT" -gt 0 ]; then
  printf '<system-reminder>\n### Brain Active Recall — prompt-relevant context contract\n'
  printf '%s' "$RESP" | jq -r '.blocks[]? | select(((.source // "") | startswith("doorbell")) | not) | "- **\(.title)** [\(.contract_category // "direct_evidence")/\(.source)] \((.include_reason // "prompt-relevant context")) — \(.content[:280])"' 2>/dev/null
  printf '</system-reminder>\n'
fi

# Doorbell files are consumed by /recall/active, which now applies prompt
# relevance/criticality gates before returning any doorbell block. Do not raw
# render the file here; that bypasses judgment and creates prompt noise.
DOORBELL="/tmp/.brain_doorbell.${SESSION_ID}.jsonl"
if [ -f "$DOORBELL" ]; then
  rm -f "$DOORBELL"
fi

exit 0
