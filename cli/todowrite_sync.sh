#!/bin/bash
# todowrite_sync.sh — Claude Code hook that syncs TodoWrite state to brain
# Triggered on PostToolUse for TodoWrite tool
# Reads the tool result JSON from stdin

set -euo pipefail

SECRET_FILE="$HOME/.brain/credentials/.personal_webhook_secret"
BRAIN_URL="http://127.0.0.1:8791"

if [ ! -f "$SECRET_FILE" ]; then
    exit 0  # silently skip if secret missing
fi

SECRET=$(cat "$SECRET_FILE")

# Read hook input from stdin
INPUT=$(cat)

# Check if this is a TodoWrite tool use
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('tool_name',''))" 2>/dev/null || echo "")

if [ "$TOOL" != "TodoWrite" ]; then
    exit 0  # not our tool, skip
fi

# Extract todos from tool_input
TODOS=$(echo "$INPUT" | python3 -c "
import sys,json
d = json.load(sys.stdin)
todos = d.get('tool_input', {}).get('todos', [])
print(json.dumps(todos))
" 2>/dev/null || echo "[]")

if [ "$TODOS" = "[]" ] || [ -z "$TODOS" ]; then
    exit 0
fi

# POST to brain
curl -s -X POST \
    -H "Authorization: Bearer $SECRET" \
    -H "Content-Type: application/json" \
    -d "{\"todos\": $TODOS}" \
    "$BRAIN_URL/brain/todos" > /dev/null 2>&1 || true

exit 0
