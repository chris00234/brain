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
SECRET_FILE="$HOME/.brain/credentials/.personal_webhook_secret"
if [ ! -r "$SECRET_FILE" ] && [ -r "$HOME/.openclaw/credentials/.personal_webhook_secret" ]; then
  SECRET_FILE="$HOME/.openclaw/credentials/.personal_webhook_secret"
fi

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

# ── Phase 3 (2026-04-27): first-turn heavy boot context for Codex ─────
# Mirror claude_boot.sh — when this is the first turn of a Codex session,
# spawn boot_context.py once to prepend identity/state/focus/recent sessions/
# atoms-due/messages. Subsequent turns rely on the per-turn /recall/active
# call already in this script. Disable via BRAIN_CODEX_HEAVY_BOOT=off if
# Codex sessions are too short-lived to benefit.
BRAIN_PY="${BRAIN_PYTHON:-/Users/chrischo/server/brain/.venv/bin/python}"
HEAVY_BOOT_ENABLED="${BRAIN_CODEX_HEAVY_BOOT:-on}"
if [ "$HEAVY_BOOT_ENABLED" != "off" ] && [ "$TURN_IDX" = "0" ] && [ -x "$BRAIN_PY" ]; then
  PROMPT_ARGS=()
  if [ -n "$PROMPT" ]; then
    PROMPT_ARGS=(--prompt "$PROMPT")
  fi
  HEAVY_RESULT=""
  if command -v timeout >/dev/null 2>&1; then
    HEAVY_RESULT=$(timeout 8 "$BRAIN_PY" /Users/chrischo/server/brain/brain_core/boot_context.py codex --limit 2 "${PROMPT_ARGS[@]}" 2>/dev/null || true)
  else
    HEAVY_RESULT=$("$BRAIN_PY" /Users/chrischo/server/brain/brain_core/boot_context.py codex --limit 2 "${PROMPT_ARGS[@]}" 2>/dev/null &
                   BG=$!
                   ( sleep 8 && kill -9 $BG 2>/dev/null ) &
                   KILLER=$!
                   wait $BG 2>/dev/null
                   kill $KILLER 2>/dev/null
                   true)
  fi
  if [ -n "$HEAVY_RESULT" ] && [ "$HEAVY_RESULT" != "No relevant boot context found. Starting fresh." ]; then
    printf '%s\n' "$HEAVY_RESULT"
  fi
fi

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

# Phase 2 (2026-04-27): surface pending cross-agent lessons. The
# lesson_fanout hook (~/.brain_hooks/lesson_fanout.py) routes new
# correction-kind / high-confidence atoms into agent_messenger; this
# block delivers them to Codex on the next turn and acks each one.
# Disabled if BRAIN_LESSON_FANOUT=off on the server side (hook is a no-op).
LESSON_BUDGET_MS="${BRAIN_LESSON_BUDGET_MS:-600}"
if [ "${BRAIN_LESSON_FANOUT:-on}" != "off" ]; then
  LESSONS=$(curl -sS --max-time "$(awk -v ms="$LESSON_BUDGET_MS" 'BEGIN{printf "%.2f", ms/1000.0}')" \
    -H @"$HEADER_FILE" \
    "${BRAIN_URL}/brain/messages/codex?limit=3" 2>/dev/null || echo "")
  LESSON_COUNT=0
  if [ -n "$LESSONS" ]; then
    LESSON_COUNT=$(printf '%s' "$LESSONS" | jq '[.messages[]? | select(.message_type == "lesson")] | length' 2>/dev/null || echo 0)
  fi
  [[ "$LESSON_COUNT" =~ ^[0-9]+$ ]] || LESSON_COUNT=0
  if [ "$LESSON_COUNT" -gt 0 ]; then
    printf '<system-reminder>\n### Brain — pending cross-agent lessons (deliver-once)\n'
    printf '%s' "$LESSONS" | jq -r '.messages[]? | select(.message_type == "lesson") | "- [\((.metadata | fromjson? | .atom_kind) // "lesson")] \(.content)"' 2>/dev/null
    printf '</system-reminder>\n'
    # Ack each delivered lesson so it does not re-appear next turn.
    for MSG_ID in $(printf '%s' "$LESSONS" | jq -r '.messages[]? | select(.message_type == "lesson") | .id' 2>/dev/null); do
      curl -sS --max-time 0.5 -X POST \
        -H @"$HEADER_FILE" \
        "${BRAIN_URL}/brain/messages/${MSG_ID}/ack" >/dev/null 2>&1 &
    done
    wait 2>/dev/null
  fi
fi

# Doorbell files are consumed by /recall/active, which now applies prompt
# relevance/criticality gates before returning any doorbell block. Do not raw
# render the file here; that bypasses judgment and creates prompt noise.
DOORBELL="/tmp/.brain_doorbell.${SESSION_ID}.jsonl"
if [ -f "$DOORBELL" ]; then
  rm -f "$DOORBELL"
fi

exit 0
