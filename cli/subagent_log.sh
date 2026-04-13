#!/usr/bin/env bash
# SubagentStart/SubagentStop hook: log subagent lifecycle to brain.
# Lightweight — just logs, never blocks.

set -euo pipefail

INPUT=$(cat)
EVENT=$(echo "$INPUT" | jq -r '.hookEventName // "unknown"')
AGENT_NAME=$(echo "$INPUT" | jq -r '.agentName // .name // "unnamed"')
SESSION_ID=$(echo "$INPUT" | jq -r '.sessionId // "unknown"')

# Only log to brain if it's reachable — don't block on failure.
SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret 2>/dev/null) || exit 0

curl -s -X POST \
  -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"$EVENT: agent=$AGENT_NAME session=$SESSION_ID\",\"category\":\"fact\",\"agent\":\"claude\",\"source\":\"subagent_hook\"}" \
  http://127.0.0.1:8791/memory >/dev/null 2>&1 || true

exit 0
