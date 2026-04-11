#!/bin/bash
# Claude Code boot context — runs on first user message per session.
# Skips if already ran within 30 minutes (prevents re-running every message).
# Loaded via Claude Code UserPromptSubmit hook.

STATE_FILE="/tmp/.claude_boot_context_ts"
COOLDOWN=1800  # 30 minutes

# Check cooldown
if [ -f "$STATE_FILE" ]; then
  LAST_RUN=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  ELAPSED=$((NOW - LAST_RUN))
  if [ "$ELAPSED" -lt "$COOLDOWN" ]; then
    exit 0  # Already booted recently, skip silently
  fi
fi

# Run boot context for Claude
RESULT=$(/opt/homebrew/bin/python3 /Users/chrischo/server/brain/brain_core/boot_context.py claude --limit 2 2>/dev/null)

if [ -n "$RESULT" ] && [ "$RESULT" != "No relevant boot context found. Starting fresh." ]; then
  echo "$RESULT"
  date +%s > "$STATE_FILE"
fi

# Phase 5 E3: working-directory-relevant canonical notes
CWD=$(pwd)
CWD_NAME=$(basename "$CWD")

if [ -n "$CWD_NAME" ] && [ "$CWD_NAME" != "~" ] && [ "$CWD_NAME" != "/" ] && [ "$CWD_NAME" != "chrischo" ]; then
  SECRET=$(cat "$HOME/.openclaw/credentials/.personal_webhook_secret" 2>/dev/null || echo "")
  if [ -n "$SECRET" ]; then
    # Pass RESULT (main boot context) via env var for dedup
    export BRAIN_BOOT_CONTEXT="$RESULT"
    # Full URL-encode via Python — sed 's/ /+/g' only handled spaces and
    # would corrupt CWD names containing &, #, ?, or other reserved chars.
    ENCODED_CWD=$(printf '%s' "$CWD_NAME" | /opt/homebrew/bin/python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))" 2>/dev/null)
    [ -z "$ENCODED_CWD" ] && ENCODED_CWD="$CWD_NAME"
    curl -s --max-time 3 \
      -H "Authorization: Bearer $SECRET" \
      "http://127.0.0.1:8791/recall?q=${ENCODED_CWD}&collection=canonical&n=5" 2>/dev/null | \
      /opt/homebrew/bin/python3 -c "
import sys, json, os
try:
    d = json.load(sys.stdin)
    results = d.get('results', [])
    existing_context = os.environ.get('BRAIN_BOOT_CONTEXT', '')
    # Dedup: skip notes whose title already appears in the existing boot context
    novel = []
    for r in results:
        title = (r.get('title') or '?')[:60]
        if title and title not in existing_context:
            novel.append((title, r))
        if len(novel) >= 3:
            break
    if novel:
        print('### Working Directory Context (canonical notes matching cwd)')
        for title, r in novel:
            content = (r.get('content') or '')[:300]
            print(f'- **{title}**: {content}')
        print('')
except Exception:
    pass
" 2>/dev/null || true
  fi
fi
