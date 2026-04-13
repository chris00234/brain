#!/bin/bash
# Claude Code boot context (v2) — TTL payload cache + sentinel degraded mode.
#
# Runs on first user message per session via Claude Code UserPromptSubmit hook.
# Differences from v1 (claude_boot.sh):
#   - TTL payload cache (5 min) instead of throttle stamp
#   - Visible sentinel <system-reminder> on degraded fetches
#   - Hard 2-second budget on every brain call
#   - Last-known-good fallback when brain is unreachable
#
# Cutover: `mv claude_boot_v2.sh claude_boot.sh` once verified.

set -uo pipefail

CACHE_FILE="/tmp/.claude_boot_context.cache"
CACHE_TS="/tmp/.claude_boot_context.ts"
CACHE_TTL=300  # 5 minutes
BRAIN_PY="${BRAIN_PYTHON:-/Users/chrischo/server/brain/.venv/bin/python}"
NOW=$(date +%s)

emit_sentinel() {
  local reason="$1"
  local age_s="${2:-}"
  if [ -n "$age_s" ]; then
    local age_min=$(( age_s / 60 ))
    printf '<system-reminder>\n[brain DEGRADED: serving cached boot context from %d minute(s) ago — %s]\n</system-reminder>\n' \
      "$age_min" "$reason"
  else
    printf '<system-reminder>\n[brain DEGRADED: brain unreachable, operating without context (%s)]\n</system-reminder>\n' \
      "$reason"
  fi
}

# Try fresh fetch with hard 2-second wall-clock budget.
RESULT=""
if command -v timeout >/dev/null 2>&1; then
  RESULT=$(timeout 2 "$BRAIN_PY" /Users/chrischo/server/brain/brain_core/boot_context.py claude --limit 2 2>/dev/null || true)
else
  # macOS lacks GNU timeout by default. Run in subshell and kill via background trap.
  RESULT=$("$BRAIN_PY" /Users/chrischo/server/brain/brain_core/boot_context.py claude --limit 2 2>/dev/null &
           BG=$!
           ( sleep 2 && kill -9 $BG 2>/dev/null ) &
           KILLER=$!
           wait $BG 2>/dev/null
           kill $KILLER 2>/dev/null
           true)
fi

if [ -n "$RESULT" ] && [ "$RESULT" != "No relevant boot context found. Starting fresh." ]; then
  printf '%s\n' "$RESULT"
  printf '%s' "$RESULT" > "$CACHE_FILE"
  echo "$NOW" > "$CACHE_TS"
else
  # Fresh fetch failed or returned the empty-context marker. Check cache.
  if [ -f "$CACHE_FILE" ] && [ -f "$CACHE_TS" ]; then
    LAST=$(cat "$CACHE_TS" 2>/dev/null || echo 0)
    AGE=$(( NOW - LAST ))
    if [ "$AGE" -lt "$CACHE_TTL" ]; then
      cat "$CACHE_FILE"
      emit_sentinel "brain timeout — cached payload" "$AGE"
    else
      emit_sentinel "cache expired (${AGE}s > ${CACHE_TTL}s)"
    fi
  else
    emit_sentinel "no cache available"
  fi
fi

# Working-directory-relevant canonical notes (preserved from v1, with budget check).
CWD=$(pwd)
CWD_NAME=$(basename "$CWD")

if [ -n "$CWD_NAME" ] && [ "$CWD_NAME" != "~" ] && [ "$CWD_NAME" != "/" ] && [ "$CWD_NAME" != "chrischo" ]; then
  SECRET=$(cat "$HOME/.openclaw/credentials/.personal_webhook_secret" 2>/dev/null || echo "")
  if [ -n "$SECRET" ]; then
    export BRAIN_BOOT_CONTEXT="$RESULT"
    ENCODED_CWD=$(printf '%s' "$CWD_NAME" | "$BRAIN_PY" -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))" 2>/dev/null)
    [ -z "$ENCODED_CWD" ] && ENCODED_CWD="$CWD_NAME"
    curl -s --max-time 2 \
      -H "Authorization: Bearer $SECRET" \
      "http://127.0.0.1:8791/recall?q=${ENCODED_CWD}&collection=canonical&n=5" 2>/dev/null | \
      "$BRAIN_PY" -c "
import sys, json, os
try:
    d = json.load(sys.stdin)
    results = d.get('results', [])
    existing_context = os.environ.get('BRAIN_BOOT_CONTEXT', '')
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
