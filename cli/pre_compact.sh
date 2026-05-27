#!/usr/bin/env bash
# PreCompact hook: notify brain that context compaction is about to happen.
# Sends a learning to brain so the session context isn't silently lost.

set -euo pipefail

SECRET=$(cat ~/.brain/credentials/.personal_webhook_secret)
SESSION_ID=$(cat | jq -r '.sessionId // "unknown"')

curl -s -X POST \
  -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"Context compaction triggered mid-session (session: $SESSION_ID). Pre-compaction context may be lost.\",\"category\":\"fact\",\"agent\":\"claude\",\"source\":\"pre_compact_hook\"}" \
  http://127.0.0.1:8791/memory >/dev/null 2>&1 || true

exit 0
