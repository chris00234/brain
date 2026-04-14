#!/usr/bin/env bash
# ralph_m7.sh — outer loop runner for Phase M7.
#
# Usage:
#   ./cli/ralph_m7.sh           # run until stop condition
#   MAX_ITER=5 ./cli/ralph_m7.sh  # short run for smoke testing
#
# For Phase M7, the workstream handlers themselves are executed by the human
# (or Claude Code) operator between script iterations — this shell is a thin
# harness that orchestrates verify + review gates and persists outcomes.
# Expect to be invoked like:
#
#   ./cli/ralph_m7_interactive.sh WS1 "shipped cron shift"  # operator-driven
#
# or in a fully-autonomous ralph context where Claude drives the outer loop,
# in which case this script is documentation of the protocol rather than the
# driver.

set -uo pipefail

BRAIN_ROOT=/Users/chrischo/server/brain
cd "$BRAIN_ROOT"

SECRET=$(cat /Users/chrischo/.openclaw/credentials/.personal_webhook_secret)
export SECRET

MAX_ITER="${MAX_ITER:-30}"
PY=./.venv/bin/python

mkdir -p logs/ralph_m7_reviews

$PY cli/ralph_m7.py --init || true

for i in $(seq 1 "$MAX_ITER"); do
  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "  Ralph M7  •  outer iteration $i / $MAX_ITER"
  echo "════════════════════════════════════════════════════════════"

  NEXT_JSON=$($PY cli/ralph_m7.py --next)
  echo "$NEXT_JSON" | jq .

  STOP=$(echo "$NEXT_JSON" | jq -r '.stop')
  if [ "$STOP" = "true" ]; then
    REASON=$(echo "$NEXT_JSON" | jq -r '.reason')
    echo "[ralph_m7] STOP: $REASON"
    break
  fi

  WS=$(echo "$NEXT_JSON" | jq -r '.workstream')
  echo "[ralph_m7] next workstream: $WS"
  echo ""
  echo "This script is a protocol harness. The actual workstream execution"
  echo "happens in-session (by Claude Code) between script invocations."
  echo "To continue the loop manually, run:"
  echo ""
  echo "  $PY cli/ralph_m7.py --start $WS"
  echo "  # ... do the work, run verify + review ..."
  echo "  $PY cli/ralph_m7.py --done $WS --commit-sha \$(git rev-parse HEAD) \\"
  echo "      --metric key=value --metric key2=value2"
  echo ""
  break
done

echo ""
$PY cli/ralph_m7.py --status
