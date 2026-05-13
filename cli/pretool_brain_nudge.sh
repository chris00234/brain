#!/bin/bash
# Claude Code PreToolUse hook — brain-first nudge on Bash/Grep/Glob/Read/Edit.
#
# Problem: agents default to grep-first / raw-read-first because those tools
# return in 200ms while brain_recall takes 1-2s. This hook closes the loop —
# queries /recall/v2 for canonical context matching the tool input AND pulls
# recent coding_event outcomes for the target file; injects any strong
# matches as additionalContext so the agent sees brain's view before acting.
#
# Output contract: JSON with hookSpecificOutput.additionalContext. Must be
# fast (<450ms total) and fail open — any error returns empty stdout.

set -uo pipefail

BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:8791}"
SECRET_FILE="$HOME/.openclaw/credentials/.personal_webhook_secret"
AGENT_NAME="${BRAIN_AGENT:-claude}"

CURL_TIMEOUT="0.3"
# Scoring landscape: noise atoms 60-80, weak file-relevant canonical 85-120,
# real strong hits 120+. Threshold 85 + path/prelude filters below handle
# noise. Canonical chunks often open with atom metadata JSON before the
# real prose, separated by "---" — we strip the prelude before display.
MIN_SCORE_THRESHOLD=85

# Read hook payload from stdin
PAYLOAD=""
if [ ! -t 0 ]; then
  PAYLOAD=$(cat 2>/dev/null || echo "")
fi
[ -z "$PAYLOAD" ] && exit 0

command -v jq >/dev/null 2>&1 || exit 0
[ -r "$SECRET_FILE" ] || exit 0

TOOL_NAME=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
[ -z "$TOOL_NAME" ] && exit 0
SESSION_ID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || echo "")
CWD_RAW=$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null || echo "")
CWD_NAME=""
[ -n "$CWD_RAW" ] && CWD_NAME=$(basename "$CWD_RAW" 2>/dev/null || echo "")

FILE_PATH=""
QUERY=""
case "$TOOL_NAME" in
  Bash)
    QUERY=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")
    FIRST_TOK=$(printf '%s' "$QUERY" | awk '{print $1}')
    # Skip commands that are obviously observability / plumbing, not knowledge-lookup.
    case "$FIRST_TOK" in
      ls|pwd|cd|echo|printf|cat|head|tail|sed|awk|grep|rg|wc|find|xargs|rm|mv|cp|mkdir|touch|chmod|chown|sleep|date|whoami|which|env|true|false|:|test|[)
        exit 0 ;;
      curl|wget|nc|ping|dig|host|nslookup|ssh|scp|rsync)
        exit 0 ;;
      ps|top|htop|kill|pkill|lsof|launchctl|systemctl|docker|brew|port|netstat|ifconfig|ip)
        exit 0 ;;
      sqlite3|psql|mysql|redis-cli|mongo|jq|yq|python|python3|node|npm|pip|uv|cargo|go|make|bash|sh|zsh|perl|ruby)
        exit 0 ;;
      git)
        case "$QUERY" in
          git\ status*|git\ diff*|git\ log*|git\ add*|git\ commit*|git\ push*|git\ pull*|git\ checkout*|git\ branch*|git\ stash*|git\ show*|git\ blame*|git\ tag*|git\ fetch*|git\ rebase*|git\ merge*|git\ reset*|git\ clean*|git\ rm*|git\ mv*|git\ remote*|git\ config*|git\ worktree*)
            exit 0 ;;
        esac ;;
    esac
    case "$QUERY" in
      ps*|lsof*|launchctl*|docker*|curl*|sqlite3*)
        exit 0 ;;
    esac
    ;;
  Grep)
    PATTERN=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.pattern // empty' 2>/dev/null || echo "")
    PATH_HINT=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.path // empty' 2>/dev/null || echo "")
    QUERY="$PATTERN $PATH_HINT"
    ;;
  Glob)
    QUERY=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.pattern // empty' 2>/dev/null || echo "")
    ;;
  Read|Edit)
    FILE_PATH=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.file_path // empty' 2>/dev/null || echo "")
    [ -z "$FILE_PATH" ] && exit 0
    case "$FILE_PATH" in
      /tmp/*|/private/tmp/*|/var/*) exit 0 ;;
    esac
    FILE_BASE=$(basename "$FILE_PATH")
    QUERY="$FILE_BASE $FILE_PATH"
    ;;
  *)
    exit 0 ;;
esac

QUERY=$(printf '%s' "$QUERY" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')
QLEN=${#QUERY}
[ "$QLEN" -lt 8 ] && exit 0
[ "$QLEN" -gt 300 ] && QUERY="${QUERY:0:300}"

# The same tool input can mean different things under Claude vs Codex and in
# different projects. Keep the original signal first, then add compact routing
# context so brain can prefer agent/project-specific canonical notes.
RECALL_QUERY="$QUERY"
[ -n "$CWD_NAME" ] && RECALL_QUERY="$RECALL_QUERY cwd:$CWD_NAME"
RECALL_QUERY="$RECALL_QUERY ai:$AGENT_NAME tool:$TOOL_NAME"
[ "${#RECALL_QUERY}" -gt 360 ] && RECALL_QUERY="${RECALL_QUERY:0:360}"

HEADER_FILE=$(mktemp -t pretool_nudge_hdr_XXXXXX 2>/dev/null) || exit 0
trap 'rm -f "$HEADER_FILE" 2>/dev/null' EXIT HUP INT TERM
{ printf 'Authorization: Bearer '; cat "$SECRET_FILE"; printf '\n'; } > "$HEADER_FILE"
chmod 600 "$HEADER_FILE" 2>/dev/null || true

ENC_QUERY=$(printf '%s' "$RECALL_QUERY" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))" 2>/dev/null || true)
[ -z "$ENC_QUERY" ] && exit 0

CURL_HEADERS=(-H "@$HEADER_FILE" -H "x-agent: $AGENT_NAME")
[ -n "$SESSION_ID" ] && CURL_HEADERS+=(-H "x-session-id: $SESSION_ID")

RESP=$(curl -sS --max-time "$CURL_TIMEOUT" \
  "${CURL_HEADERS[@]}" \
  "${BRAIN_URL}/recall/v2?q=${ENC_QUERY}&n=3&collection=canonical&actor=${AGENT_NAME}" 2>/dev/null || echo "")

HINT=""
if [ -n "$RESP" ]; then
  HINT=$(printf '%s' "$RESP" | jq -r --argjson min "$MIN_SCORE_THRESHOLD" '
    def src_label:
      (.path // "" | capture("(?<n>[^/]+)$") | .n // "") as $fn |
      if ($fn | length) >= 4 then $fn else (.title // "untitled") end;
    def is_generic_title:
      (.title // "") | test("^(Summary|Details|Metadata|Sources|Did this week|untitled)( *\\(part *[0-9]+\\))? *$");
    def strip_prelude:
      (.content // "") as $c |
      if ($c | test("\n---\n"))
      then ($c | split("\n---\n")[1:] | join("\n---\n"))
      else $c
      end;
    def pure_json_chunk:
      (strip_prelude | .[:120]) as $head |
      ($head | test("^\\s*[\"{\\[]")) or
      ($head | test("\"(confidence|status|visibility|subtype|review_state)\"\\s*:"));
    # Auto-generated distillation snapshots from event streams (file changes,
    # commits, raw shell). These crowd recall results because they verbatim
    # contain code paths / commit text, but they are stale by definition —
    # the current file/git state is the truth. Drop at the hook layer.
    def is_dist_received_snapshot:
      ((.path // "") | test("dist_received_at_|/dist_author_chris_cho_body_|/dist_raw_shell_")) or
      ((.title // "") | test("^\\{\"(_received_at|author|cwd|file_path)\"")) or
      ((strip_prelude | .[:200]) | test("\\{\"(author|_received_at|cwd|file_path)\":\\s*\""));
    .results // []
    | map(select(
        (.score // 0) >= $min
        and ((.path // "") | length) >= 4
        and (pure_json_chunk | not)
        and (is_dist_received_snapshot | not)
      ))
    | sort_by([-(.score // 0), (if is_generic_title then 1 else 0 end)])
    | .[0:2]
    | if length == 0 then empty
      else
        "Brain canonical matches relevant here:\n" +
        (map("- **\(src_label)** (score \(.score // 0 | floor)): \((strip_prelude) | gsub("\n"; " ") | .[:280])") | join("\n"))
      end
  ' 2>/dev/null || echo "")
fi

# For Read/Edit, also surface recent coding_event outcomes for this file.
if { [ "$TOOL_NAME" = "Read" ] || [ "$TOOL_NAME" = "Edit" ]; } && [ -n "${FILE_PATH:-}" ]; then
  ENC_FP=$(printf '%s' "$FILE_PATH" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))" 2>/dev/null || true)
  if [ -n "$ENC_FP" ]; then
    CE_RESP=$(curl -sS --max-time 0.25 \
      "${CURL_HEADERS[@]}" \
      "${BRAIN_URL}/brain/coding_events?file_path=${ENC_FP}&limit=4" 2>/dev/null || echo "")
    if [ -n "$CE_RESP" ]; then
      CE_HINT=$(printf '%s' "$CE_RESP" | jq -r '
        .events // []
        | map(select(.outcome != null and .outcome != "pending"))
        | .[0:3]
        | if length == 0 then empty
          else "\nRecent outcome history on this file:\n" +
               (map("- [\(.outcome)] \(.timestamp // "?") \(.tool // "?")") | join("\n"))
          end
      ' 2>/dev/null || echo "")
      [ -n "$CE_HINT" ] && HINT="${HINT}${CE_HINT}"
    fi
  fi
fi

[ -z "$HINT" ] && exit 0

jq -nc --arg hint "$HINT" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    additionalContext: $hint
  }
}' 2>/dev/null || true

exit 0
