#!/bin/bash
# Codex CLI UserPromptSubmit hook — active recall per turn.
#
# Mirrors claude_boot.sh but trimmed: no first-turn heavy boot_context, no
# 5-min payload cache. Codex sessions are often short-lived, and oh-my-codex
# already ships its own orchestration layer; this hook's single job is to
# inject brain's intent-routed recall (canonical + semantic + doorbell)
# before each Codex turn so Codex sees Chris's preferences and recent
# decisions the same way Claude Code does.
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

SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || echo "")
PROMPT=$(printf '%s' "$PAYLOAD" | jq -r '.prompt // .user_message // empty' 2>/dev/null || echo "")
CWD_RAW=$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null || echo "")
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

REQ_BODY=$(jq -n \
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

[ -z "$RESP" ] && exit 0

# Format blocks into a <system-reminder> block. Codex treats
# UserPromptSubmit stdout as trailing context on the current turn.
printf '<system-reminder>\n### Brain Active Recall — per-turn injection\n'
printf '%s' "$RESP" | jq -r '.blocks[]? | "- **\(.title)** [\(.source)] \(.content[:280])"' 2>/dev/null
printf '</system-reminder>\n'

# Doorbell: urgent brain_loop messages for this session.
DOORBELL="/tmp/.brain_doorbell.${SESSION_ID}.jsonl"
if [ -f "$DOORBELL" ]; then
  echo
  echo '<system-reminder>'
  echo '### ⚠ Brain Doorbell — brain_speak_urgent'
  cat "$DOORBELL" 2>/dev/null | jq -r '"\(.title // "urgent"): \(.content // "")"' 2>/dev/null | head -20
  echo '</system-reminder>'
  rm -f "$DOORBELL"
fi

exit 0
