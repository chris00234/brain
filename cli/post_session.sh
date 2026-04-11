#!/bin/bash
# Claude Code SessionEnd hook → POST recent user messages to brain API /learn
#
# Wired in ~/.claude/settings.json under hooks.SessionEnd. Receives JSON on stdin
# with at least {"session_id": "...", "transcript_path": "..."}.
# Best-effort: never blocks the user, never errors loud. Logs failures only.
#
# Phase 2a: lives at workspace-jenna/scripts/post_session.sh.
# Phase 1 will move this to /Users/chrischo/server/brain/cli/post_session.sh.

set -uo pipefail

LOG=/Users/chrischo/.openclaw/logs/post-session.log
SECRET_FILE=/Users/chrischo/.openclaw/credentials/.personal_webhook_secret
BRAIN_URL=http://127.0.0.1:8791/learn

mkdir -p "$(dirname "$LOG")"

# Read JSON payload from stdin (Claude Code passes hook context via stdin)
PAYLOAD="$(cat)"

# Best-effort extraction with python (jq may not be available). Pass payload
# via stdin — argv interpolation is unsafe when the hook JSON contains shell
# metacharacters (backticks, $, newlines, etc.).
TRANSCRIPT_PATH=$(printf '%s' "$PAYLOAD" | /opt/homebrew/bin/python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path') or '')
except Exception:
    pass
" 2>/dev/null)

SESSION_ID=$(printf '%s' "$PAYLOAD" | /opt/homebrew/bin/python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('session_id') or '')
except Exception:
    pass
" 2>/dev/null)

# If no transcript path provided, try to find the most recent session file for this dir
if [[ -z "${TRANSCRIPT_PATH:-}" || ! -f "${TRANSCRIPT_PATH:-}" ]]; then
    PROJECT_DIR=/Users/chrischo/.claude/projects/-Users-chrischo
    if [[ -n "${SESSION_ID:-}" && -f "$PROJECT_DIR/${SESSION_ID}.jsonl" ]]; then
        TRANSCRIPT_PATH="$PROJECT_DIR/${SESSION_ID}.jsonl"
    else
        TRANSCRIPT_PATH=$(find "$PROJECT_DIR" -maxdepth 1 -name '*.jsonl' -type f -print0 2>/dev/null \
            | xargs -0 ls -t 2>/dev/null | head -1)
    fi
fi

if [[ -z "${TRANSCRIPT_PATH:-}" || ! -f "${TRANSCRIPT_PATH:-}" ]]; then
    echo "$(date -Iseconds) no transcript found" >> "$LOG"
    exit 0
fi

# Extract last 10 user messages and bot responses, concatenate for /learn
TRANSCRIPT=$(/opt/homebrew/bin/python3 - <<PY 2>/dev/null
import json, sys
path = "$TRANSCRIPT_PATH"
lines = []
try:
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            role = rec.get("type") or rec.get("role")
            msg = rec.get("message") or {}
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    text = " ".join(
                        (c.get("text") or "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                else:
                    text = content or ""
            else:
                text = str(msg)
            if role and text and len(text.strip()) > 5:
                lines.append(f"{role}: {text.strip()[:1500]}")
except Exception as e:
    sys.stderr.write(f"transcript parse error: {e}\n")
    sys.exit(0)
print("\n\n".join(lines[-20:]))
PY
)

if [[ -z "${TRANSCRIPT:-}" || ${#TRANSCRIPT} -lt 50 ]]; then
    echo "$(date -Iseconds) transcript too short, skipping" >> "$LOG"
    exit 0
fi

if [[ ! -f "$SECRET_FILE" ]]; then
    echo "$(date -Iseconds) no secret file" >> "$LOG"
    exit 0
fi

SECRET=$(cat "$SECRET_FILE")

# POST in background — never block the session end. Failure is silent.
# Write TRANSCRIPT to a temp file and pipe it in so there's zero risk of
# here-string heredoc delimiter collision if the transcript happens to
# contain our delimiter on its own line.
TRANSCRIPT_TMP=$(mktemp -t brain_post_transcript_XXXXXX.txt)
printf '%s' "$TRANSCRIPT" > "$TRANSCRIPT_TMP"
PAYLOAD_JSON=$(/opt/homebrew/bin/python3 -c "
import json, sys
print(json.dumps({
    'transcript': sys.stdin.read(),
    'source': 'claude_code',
    'agent': 'claude',
}))
" < "$TRANSCRIPT_TMP")
rm -f "$TRANSCRIPT_TMP"

(
    # Write payload to temp file to avoid argv length limits and shell quoting
    # surprises with special characters in transcripts.
    TMP_PAYLOAD=$(mktemp -t brain_post_session_XXXXXX.json)
    printf '%s' "$PAYLOAD_JSON" > "$TMP_PAYLOAD"
    trap 'rm -f "$TMP_PAYLOAD"' EXIT
    RESPONSE=$(curl -sf -X POST "$BRAIN_URL" \
        -H "Authorization: Bearer $SECRET" \
        -H "Content-Type: application/json" \
        -d @"$TMP_PAYLOAD" \
        --max-time 10)
    if [[ -n "$RESPONSE" ]]; then
        echo "$(date -Iseconds) ok session=$SESSION_ID len=${#TRANSCRIPT} resp=$RESPONSE" >> "$LOG"
    else
        echo "$(date -Iseconds) FAIL session=$SESSION_ID len=${#TRANSCRIPT}" >> "$LOG"
    fi

    # Phase 2: Record session as outcome for accuracy/heuristic learning loop.
    # Only for significant sessions (10+ tool calls = real work, not just chat).
    # Pass TRANSCRIPT_PATH via env so a path containing quotes, backslashes, or
    # $ can't corrupt the Python literal or inject code.
    TOOL_COUNT=$(TRANSCRIPT_PATH="$TRANSCRIPT_PATH" /opt/homebrew/bin/python3 - <<'PYCOUNT' 2>/dev/null
import json, os
path = os.environ["TRANSCRIPT_PATH"]
count = 0
try:
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
                msg = rec.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else []
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            count += 1
            except Exception:
                continue
except Exception:
    pass
print(count)
PYCOUNT
)

    if [[ "${TOOL_COUNT:-0}" -ge 10 ]]; then
        # Extract a 1-line summary from the last assistant message
        SUMMARY=$(TRANSCRIPT_PATH="$TRANSCRIPT_PATH" /opt/homebrew/bin/python3 - <<'PYSUM' 2>/dev/null
import json, os
path = os.environ["TRANSCRIPT_PATH"]
last_text = ""
try:
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("type") == "assistant":
                    msg = rec.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else []
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text" and len(c.get("text","")) > 20:
                                last_text = c["text"][:200]
            except Exception:
                continue
except Exception:
    pass
print(last_text.replace('"','\\"').replace('\n',' ')[:200])
PYSUM
)
        # Create a task + outcome record for the session. Pass shell vars via
        # env (not argv/literal interpolation) so quotes in SUMMARY can't
        # break the Python string.
        TASK_JSON=$(SESSION_ID="${SESSION_ID:-unknown}" SUMMARY="${SUMMARY:-}" TOOL_COUNT="${TOOL_COUNT:-0}" /opt/homebrew/bin/python3 -c "
import json, os
title = f'Claude Code session: {os.environ[\"SESSION_ID\"]}'[:80]
desc = os.environ.get('SUMMARY') or f'Session with {os.environ[\"TOOL_COUNT\"]} tool calls'
print(json.dumps({
    'title': title,
    'description': desc[:200],
    'priority': 5,
    'confidence': 0.8,
    'assigned_agent': 'claude',
}))
" 2>/dev/null)

        TASK_RESP=$(curl -sf -X POST "http://127.0.0.1:8791/brain/tasks" \
            -H "Authorization: Bearer $SECRET" \
            -H "Content-Type: application/json" \
            -d "$TASK_JSON" \
            --max-time 5 2>/dev/null)

        TASK_ID=$(printf '%s' "$TASK_RESP" | /opt/homebrew/bin/python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

        if [[ -n "$TASK_ID" ]]; then
            # Approve → start → complete the task (recording full lifecycle).
            # SUMMARY is untrusted transcript text — pass via env to Python
            # rather than a here-string, matching the TASK_JSON pattern above.
            curl -sf -X POST "http://127.0.0.1:8791/brain/tasks/$TASK_ID/approve" \
                -H "Authorization: Bearer $SECRET" --max-time 3 >/dev/null 2>&1
            curl -sf -X POST "http://127.0.0.1:8791/brain/tasks/$TASK_ID/start" \
                -H "Authorization: Bearer $SECRET" --max-time 3 >/dev/null 2>&1
            COMPLETE_JSON=$(SUMMARY="${SUMMARY:-completed}" /opt/homebrew/bin/python3 -c "
import json, os
print(json.dumps({'result': os.environ.get('SUMMARY','')[:500]}))
" 2>/dev/null)
            curl -sf -X POST "http://127.0.0.1:8791/brain/tasks/$TASK_ID/complete" \
                -H "Authorization: Bearer $SECRET" \
                -H "Content-Type: application/json" \
                -d "$COMPLETE_JSON" \
                --max-time 3 >/dev/null 2>&1
            echo "$(date -Iseconds) outcome recorded task=$TASK_ID tools=$TOOL_COUNT" >> "$LOG"
        fi
    fi
) &

exit 0
