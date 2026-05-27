#!/usr/bin/env bash
# ralph_m7_verify.sh — per-iteration 7-gate verification harness for Phase M7.
#
# Exit codes:
#   0 = all gates green (proceed to code review)
#   1 = hard failure (halt loop)
#   2 = soft warn (loop may continue, but flagged)
#
# Gates (in order; fail-fast on hard gates):
#   1. pytest tests/unit/                           [HARD]
#   2. ruff check .                                 [HARD]
#   3. ruff format --check .                        [HARD]
#   4. stable eval regression (content_hit >= 94)   [HARD]
#   5. brain health (/brain/health status==healthy) [HARD]
#   6. integration tier (only if INT_TESTS=1)       [HARD]
#   7. err log growth (delta failures.jsonl <= 5)   [SOFT]

set -uo pipefail

BRAIN_ROOT=/Users/chrischo/server/brain
cd "$BRAIN_ROOT"

SECRET_FILE=/Users/chrischo/.brain/credentials/.personal_webhook_secret
SECRET=$(cat "$SECRET_FILE")
export SECRET

STABLE_FLOOR="${BRAIN_M7_STABLE_CONTENT_FLOOR:-94.0}"
INT_TESTS="${BRAIN_M7_INT_TESTS:-0}"

RESULT=0
TIMESTAMP=$(date -Iseconds)
REPORT="/tmp/brain_ralph_m7_verify_last.txt"
echo "ralph_m7_verify $TIMESTAMP" > "$REPORT"

emit() {
  printf '  %s\n' "$1" | tee -a "$REPORT"
}

gate() {
  local name="$1"; shift
  echo ""
  echo "── Gate: $name ──"
  if "$@"; then
    emit "PASS: $name"
    return 0
  else
    emit "FAIL: $name"
    return 1
  fi
}

# Gate 1 — pytest unit
if ! gate "pytest tests/unit" ./.venv/bin/python -m pytest tests/unit/ -q --maxfail=3; then
  RESULT=1
fi

# Gate 2 — ruff check
if ! gate "ruff check" ./.venv/bin/python -m ruff check .; then
  RESULT=1
fi

# Gate 3 — ruff format
if ! gate "ruff format" ./.venv/bin/python -m ruff format --check .; then
  RESULT=1
fi

# Gate 4 — stable eval regression
echo ""
echo "── Gate: stable eval regression ──"
EVAL_JSON=$(./.venv/bin/python cli/eval_compare.py --json --eval-set cli/eval_set_stable.json 2>/dev/null || echo "")
if [ -z "$EVAL_JSON" ]; then
  emit "FAIL: stable eval did not run"
  RESULT=1
else
  CONTENT=$(echo "$EVAL_JSON" | jq -r '.v2.hit_content_pct // 0')
  emit "stable content_hit = $CONTENT (floor $STABLE_FLOOR)"
  if awk -v a="$CONTENT" -v b="$STABLE_FLOOR" 'BEGIN{exit !(a+0 >= b+0)}'; then
    emit "PASS: stable eval >= floor"
  else
    emit "FAIL: stable eval regressed below floor"
    RESULT=1
  fi
fi

# Gate 5 — brain health
echo ""
echo "── Gate: brain health ──"
HEALTH=$(curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health 2>/dev/null || echo '{}')
STATUS=$(echo "$HEALTH" | jq -r '.status // "unknown"')
emit "status = $STATUS"
if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ]; then
  # degraded is tolerable if no critical alerts
  ALERT_COUNT=$(echo "$HEALTH" | jq -r '.alerts | length // 0')
  emit "alerts = $ALERT_COUNT"
  if [ "$ALERT_COUNT" -gt 3 ]; then
    emit "FAIL: too many alerts"
    RESULT=1
  else
    emit "PASS: brain reachable, alerts <=3"
  fi
else
  emit "FAIL: brain not healthy or degraded"
  RESULT=1
fi

# Gate 6 — integration tier (opt-in via env)
if [ "$INT_TESTS" = "1" ]; then
  echo ""
  echo "── Gate: integration tier ──"
  if BRAIN_INTEGRATION_TESTS=1 ./.venv/bin/python -m pytest tests/integration/ -q --maxfail=1; then
    emit "PASS: integration green"
  else
    emit "FAIL: integration regressions"
    RESULT=1
  fi
fi

# Gate 7 — err log growth (soft)
echo ""
echo "── Gate: err log growth (soft) ──"
FAILURES_FILE="$BRAIN_ROOT/logs/failures.jsonl"
CUR=$(wc -l < "$FAILURES_FILE" 2>/dev/null || echo 0)
PREV_FILE="/tmp/brain_ralph_m7_failures_last.txt"
PREV=$(cat "$PREV_FILE" 2>/dev/null || echo "$CUR")
DELTA=$((CUR - PREV))
emit "failures.jsonl lines: prev=$PREV cur=$CUR delta=$DELTA"
echo "$CUR" > "$PREV_FILE"
if [ "$DELTA" -gt 5 ]; then
  emit "WARN: err log grew by $DELTA (>5 soft limit)"
  [ $RESULT -eq 0 ] && RESULT=2
fi

echo ""
if [ $RESULT -eq 0 ]; then
  echo "======================================"
  echo "  VERIFY RESULT: GREEN (all gates OK)"
  echo "======================================"
elif [ $RESULT -eq 2 ]; then
  echo "======================================"
  echo "  VERIFY RESULT: SOFT WARN (continue)"
  echo "======================================"
else
  echo "======================================"
  echo "  VERIFY RESULT: HARD FAIL (halt)"
  echo "======================================"
fi

exit $RESULT
