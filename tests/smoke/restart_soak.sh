#!/bin/bash
# tests/smoke/restart_soak.sh — Phase H5 brain restart soak test.
#
# Restarts brain-server N times, waits for healthy, runs stable eval,
# asserts content_hit ≥ 96.0% on each iteration.
#
# Usage:
#   tests/smoke/restart_soak.sh             # default 5 iterations
#   tests/smoke/restart_soak.sh 3           # 3 iterations
#
# Verifies:
#   - schema_versions runner picks up brain_db@3 every restart
#   - autonomy gate warm cache rebuilds on each restart
#   - persistent breakers survive
#   - eval baseline doesn't drift

set -u

ITERATIONS="${1:-5}"
THRESHOLD=96.0
SECRET=$(cat ~/.brain/credentials/.personal_webhook_secret 2>/dev/null)
BRAIN_PY=/Users/chrischo/server/brain/.venv/bin/python
BRAIN_DIR=/Users/chrischo/server/brain
EVAL_SCRIPT="$BRAIN_DIR/cli/eval_compare.py"
EVAL_SET="$BRAIN_DIR/cli/eval_set_stable.json"

if [ -z "$SECRET" ]; then
    echo "[FAIL] no webhook secret"
    exit 1
fi

PASS=0
FAIL=0
LAST_CONTENT="0"

for i in $(seq 1 "$ITERATIONS"); do
    echo "=== iteration $i / $ITERATIONS ==="

    if ! launchctl bootout "gui/$(id -u)/ai.brain.server" 2>&1; then
        echo "  bootout returned non-zero (may be expected if not loaded)"
    fi
    sleep 1
    if ! launchctl bootstrap "gui/$(id -u)" /Users/chrischo/Library/LaunchAgents/ai.brain.server.plist; then
        echo "[FAIL #$i] bootstrap failed"
        FAIL=$((FAIL + 1))
        continue
    fi

    # Wait up to 10s for /healthz to return 200
    for j in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        if curl -fs -H "Authorization: Bearer $SECRET" \
            http://127.0.0.1:8791/healthz > /dev/null 2>&1; then
            echo "  brain alive after ${j}s"
            break
        fi
    done

    HEALTH=$(curl -s -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health)
    STATUS=$(echo "$HEALTH" | "$BRAIN_PY" -c "import json,sys; print(json.load(sys.stdin)['status'])")

    if [ "$STATUS" != "healthy" ]; then
        ALERTS=$(echo "$HEALTH" | "$BRAIN_PY" -c "import json,sys; print(json.load(sys.stdin).get('alerts',[]))")
        echo "[FAIL #$i] status=$STATUS alerts=$ALERTS"
        FAIL=$((FAIL + 1))
        continue
    fi

    # Run stable eval
    RESULT=$(cd "$BRAIN_DIR" && "$BRAIN_PY" "$EVAL_SCRIPT" --eval-set "$EVAL_SET" --json --limit 138 2>&1)
    CONTENT=$(echo "$RESULT" | "$BRAIN_PY" -c "import json,sys; d=json.load(sys.stdin); print(d['v2']['hit_content_pct'])" 2>/dev/null)
    LAST_CONTENT="$CONTENT"

    if [ -z "$CONTENT" ]; then
        echo "[FAIL #$i] eval produced no result"
        FAIL=$((FAIL + 1))
        continue
    fi

    # Float comparison via awk
    OK=$(awk "BEGIN{print ($CONTENT >= $THRESHOLD) ? 1 : 0}")
    if [ "$OK" = "1" ]; then
        echo "[OK #$i] status=healthy stable content_hit=$CONTENT% (>= $THRESHOLD)"
        PASS=$((PASS + 1))
    else
        echo "[FAIL #$i] status=healthy but content_hit=$CONTENT% (< $THRESHOLD)"
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "==============================="
echo "  soak: $PASS / $ITERATIONS pass · last content_hit=$LAST_CONTENT%"
echo "==============================="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
