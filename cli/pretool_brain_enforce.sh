#!/bin/bash
# Claude Code PreToolUse hook — brain enforcement (deny).
#
# Separate from pretool_brain_nudge.sh (which only hints). This hook returns
# permissionDecision=deny when the agent is about to touch a path that brain
# has been told is dangerous without explicit Chris approval.
#
# Scope (phase 1, narrow):
#   - ~/.brain/credentials/**       — secrets dir
#   - ~/.hermes/profiles/*/.env       — Hermes per-profile secrets
#   - ~/server/brain/models/adapters/lora_active/**  — live LoRA adapter
#
# Escape hatch: set BRAIN_OVERRIDE=1 in the agent's env before the tool call.
# The override goes in the audit log so it's reviewable after the fact.
#
# Tool scope: Edit | Write | NotebookEdit. Read is NOT denied (reading
# secrets is fine; writing them breaks things).

set -uo pipefail

BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:8791}"
SECRET_FILE="$HOME/.brain/credentials/.personal_webhook_secret"
if [ ! -r "$SECRET_FILE" ] && [ -r "$HOME/.openclaw/credentials/.personal_webhook_secret" ]; then
  SECRET_FILE="$HOME/.openclaw/credentials/.personal_webhook_secret"
fi
AGENT_NAME="${BRAIN_AGENT:-claude}"

PAYLOAD=""
if [ ! -t 0 ]; then
  PAYLOAD=$(cat 2>/dev/null || echo "")
fi
[ -z "$PAYLOAD" ] && exit 0

command -v jq >/dev/null 2>&1 || exit 0

TOOL_NAME=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
case "$TOOL_NAME" in
  Edit|Write|NotebookEdit) ;;
  *) exit 0 ;;
esac

FILE_PATH=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.file_path // empty' 2>/dev/null || echo "")
[ -z "$FILE_PATH" ] && exit 0

# Normalize ~ expansion (Claude Code sometimes passes literal ~)
case "$FILE_PATH" in
  "~"*) FILE_PATH="${HOME}${FILE_PATH:1}" ;;
esac

# Dangerous-path matcher. Keep narrow: each pattern is a literal prefix.
DENY_REASON=""
case "$FILE_PATH" in
  "$HOME/.brain/credentials"/*|"$HOME/.brain/credentials"|"$HOME/.openclaw/credentials"/*|"$HOME/.openclaw/credentials")
    DENY_REASON="Modifies OpenClaw credentials dir. Rotate via the documented credential workflow, not by direct Edit/Write."
    ;;
  "$HOME/.hermes/profiles"/*/config.yaml)
    DENY_REASON="Hermes profile configs are managed by \`hermes config set\` / \`hermes -p <name> config set\`. Direct YAML edits bypass validation."
    ;;
  "$HOME/server/brain/models/adapters/lora_active"/*|"$HOME/server/brain/models/adapters/lora_active")
    DENY_REASON="Live LoRA adapter must not be modified in place — that's the serving adapter. Write to lora_v_candidate/ and let lora_ab_gate promote it."
    ;;
  "$HOME/.claude/settings.json")
    # Conditional: only deny if the edit REMOVES brain hooks.
    OLD_STR=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.old_string // empty' 2>/dev/null || echo "")
    NEW_STR=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.new_string // empty' 2>/dev/null || echo "")
    # If old_string contains a brain hook path and new_string no longer does → deny
    if printf '%s' "$OLD_STR" | grep -q "brain/cli/\(claude_boot\|pretool_brain_nudge\|posttool_coding_event\|subagent_log\|pretool_brain_enforce\)\.sh" 2>/dev/null; then
      if ! printf '%s' "$NEW_STR" | grep -q "brain/cli/\(claude_boot\|pretool_brain_nudge\|posttool_coding_event\|subagent_log\|pretool_brain_enforce\)\.sh" 2>/dev/null; then
        DENY_REASON="This edit removes a brain hook from Claude Code settings. Brain hooks are how brain observes + acts; removing them silently breaks agency."
      fi
    fi
    ;;
esac

[ -z "$DENY_REASON" ] && exit 0

# Escape hatch
if [ "${BRAIN_OVERRIDE:-0}" = "1" ]; then
  # Log override to brain audit trail (best-effort). Header goes via file to
  # keep the secret out of the process table.
  if [ -r "$SECRET_FILE" ]; then
    HDR_FILE=$(mktemp -t brain_enforce_hdr_XXXXXX 2>/dev/null) || exit 0
    trap 'rm -f "$HDR_FILE" 2>/dev/null' EXIT HUP INT TERM
    { printf 'Authorization: Bearer '; cat "$SECRET_FILE"; printf '\n'; } > "$HDR_FILE"
    chmod 600 "$HDR_FILE" 2>/dev/null || true
    BODY=$(jq -nc \
      --arg content "BRAIN_OVERRIDE=1 used to bypass deny on $FILE_PATH ($TOOL_NAME). Reason denied: $DENY_REASON" \
      --arg source "brain_enforce_override" \
      --arg agent "$AGENT_NAME" \
      '{content: $content, category: "audit", agent: $agent, source: $source}')
    curl -s -X POST --max-time 1 \
      -H "@$HDR_FILE" \
      -H "x-agent: $AGENT_NAME" \
      -H "Content-Type: application/json" \
      -d "$BODY" \
      "$BRAIN_URL/memory" >/dev/null 2>&1 || true
  fi
  exit 0
fi

# Emit deny decision
jq -nc --arg reason "$DENY_REASON" --arg path "$FILE_PATH" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: ("[brain enforce] " + $reason + " Path: " + $path + " — set BRAIN_OVERRIDE=1 to bypass with audit log.")
  }
}' 2>/dev/null || true

exit 0
