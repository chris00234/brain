#!/usr/bin/env bash
# SubagentStart/SubagentStop hook.
#
# On SubagentStop: log lifecycle to brain (original behavior).
#
# On SubagentStart: in addition to logging, query brain for canonical
# context relevant to the subagent's task description and emit it as a
# <system-reminder> so the subagent's first turn doesn't start blind.
# Prior to this change, subagents got zero proactive brain context — they
# only saw what brain_recall would return IF they chose to call it.
#
# Budget: 3s total (curl 1.5s max). Never blocks the dispatch on failure.

set -uo pipefail

INPUT=$(cat)
EVENT=$(echo "$INPUT" | jq -r '.hookEventName // "unknown"')
AGENT_NAME=$(echo "$INPUT" | jq -r '.agentName // .name // "unnamed"')
SESSION_ID=$(echo "$INPUT" | jq -r '.sessionId // .session_id // "unknown"')

SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret 2>/dev/null) || exit 0
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:8791}"

# Always log lifecycle (existing behavior). Build JSON via jq so untrusted
# hook-supplied strings (agent name, session id) can't break out of the
# quoting context.
LOG_BODY=$(jq -nc \
  --arg content "$EVENT: agent=$AGENT_NAME session=$SESSION_ID" \
  '{content: $content, category: "fact", agent: "claude", source: "subagent_hook"}' 2>/dev/null || echo "")
if [ -n "$LOG_BODY" ]; then
  curl -s -X POST --max-time 2 \
    -H "Authorization: Bearer $SECRET" \
    -H "Content-Type: application/json" \
    -d "$LOG_BODY" \
    "$BRAIN_URL/memory" >/dev/null 2>&1 || true
fi

# Only inject context on Start, not Stop
if [ "$EVENT" != "SubagentStart" ]; then
  exit 0
fi

# Extract the task prompt. Claude Code passes this under varied keys
# depending on tool version — try each in priority order.
TASK=""
for k in '.task' '.description' '.prompt' '.tool_input.prompt' '.tool_input.description' '.input.prompt'; do
  TASK=$(echo "$INPUT" | jq -r "$k // empty" 2>/dev/null || echo "")
  [ -n "$TASK" ] && [ "$TASK" != "null" ] && break
done

# Fall back: if no task, nothing to query on. Emit a minimal identity block.
if [ -z "$TASK" ] || [ "$TASK" = "null" ]; then
  cat <<'EOF'
<system-reminder>
[Brain baseline for subagent] You are a subagent for Chris Cho (Irvine, CA, software engineer). The parent session has active brain context. Use brain_recall MCP tool for anything requiring Chris's history, decisions, or preferences.
</system-reminder>
EOF
  exit 0
fi

# Compact task for query (take first ~300 chars, strip newlines)
QUERY=$(printf '%s' "$TASK" | tr -s '[:space:]' ' ' | cut -c 1-300)
ENC_QUERY=$(printf '%s' "$QUERY" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))" 2>/dev/null || true)
[ -z "$ENC_QUERY" ] && exit 0

# Query canonical for the task topic. 1.5s budget; bail silently on timeout.
HEADER_FILE=$(mktemp -t subagent_hdr_XXXXXX 2>/dev/null) || exit 0
trap 'rm -f "$HEADER_FILE" 2>/dev/null' EXIT HUP INT TERM
{ printf 'Authorization: Bearer '; printf '%s' "$SECRET"; printf '\n'; } > "$HEADER_FILE"
chmod 600 "$HEADER_FILE" 2>/dev/null || true

RESP=$(curl -sS --max-time 1.5 \
  -H "@$HEADER_FILE" \
  "${BRAIN_URL}/recall/v2?q=${ENC_QUERY}&n=3&collection=canonical" 2>/dev/null || echo "")

HINT=""
if [ -n "$RESP" ]; then
  HINT=$(printf '%s' "$RESP" | jq -r '
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
    .results // []
    | map(select(
        (.score // 0) >= 85
        and ((.path // "") | length) >= 4
        and (pure_json_chunk | not)
      ))
    | sort_by([-(.score // 0), (if is_generic_title then 1 else 0 end)])
    | .[0:3]
    | if length == 0 then empty
      else (map("- \(src_label): \((strip_prelude) | gsub("\n"; " ") | .[:220])") | join("\n"))
      end
  ' 2>/dev/null || echo "")
fi

# Emit a system-reminder with identity + task-relevant canonical hits.
if [ -n "$HINT" ]; then
  printf '<system-reminder>\n[Brain baseline for subagent %s]\nCanonical pages relevant to this task:\n%s\n\nFor deeper context use brain_recall MCP tool. Parent session has full boot context.\n</system-reminder>\n' \
    "$AGENT_NAME" "$HINT"
else
  printf '<system-reminder>\n[Brain baseline for subagent %s] No strong canonical match for this task. Use brain_recall MCP tool if you need Chris-specific context (decisions, preferences, past work).\n</system-reminder>\n' \
    "$AGENT_NAME"
fi

exit 0
