#!/bin/bash
set -euo pipefail

# OrbStack Watchdog — Auto-Recovery + Alert
# Detects TWO failure modes:
#   1. Docker socket dead/unresponsive (auto-restarts OrbStack)
#   2. OrbStack Helper stuck in high-CPU degraded state (alerts via Telegram)
# Runs every 5 minutes via launchd.
# https://github.com/orbstack/orbstack/issues/1842

LOG_TAG="[orbstack-watchdog]"
STATE_FILE="/Users/chrischo/server/brain/logs/.orbstack_watchdog_state"
RESTART_STATE="/Users/chrischo/server/brain/logs/.orbstack_restart_state"
MEM_STATE_FILE="/Users/chrischo/server/brain/logs/.orbstack_mem_watchdog_state"
MEM_ALERT_STATE="/Users/chrischo/server/brain/logs/.orbstack_mem_alert_state"
PORT_STATE_FILE="/Users/chrischo/server/brain/logs/.orbstack_port_watchdog_state"
CPU_THRESHOLD=80
MEM_RSS_THRESHOLD_MB=10240
# Memory alerts should be actionable, not hourly RSS noise. OrbStack Helper
# RSS includes VM/cache/accounting overhead on macOS, so only alert when high
# RSS coincides with real pressure/swap stress.
MEM_ALERT_COOLDOWN=21600
MEM_SWAP_THRESHOLD_MB=1024
MEM_FREE_PCT_THRESHOLD=10
CONSECUTIVE_THRESHOLD=3
RESTART_COOLDOWN=600  # Don't restart more than once per 10 minutes
CHAT_ID="8484060831"

# Pin to Chris's real OrbStack instance. Hermes profile HOME can point at
# ~/.hermes/profiles/<profile>/home, which makes docker/orbctl inspect the
# wrong .orbstack tree.
export HOME=/Users/chrischo
export DOCKER_HOST=unix:///Users/chrischo/.orbstack/run/docker.sock

source /Users/chrischo/.brain/.env 2>/dev/null || true
BOT_TOKEN="${TELEGRAM_ELLIE_TOKEN:-}"

send_telegram() {
  local msg="$1"
  if [ -n "$BOT_TOKEN" ]; then
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      -d chat_id="$CHAT_ID" \
      -d text="$msg" \
      -d parse_mode="Markdown" > /dev/null 2>&1 || true
  fi
}

now_epoch() {
  date +%s
}

restart_orbstack() {
  local reason="$1"
  local telegram_msg="$2"

  LAST_RESTART=0
  if [ -f "$RESTART_STATE" ]; then
    LAST_RESTART=$(cat "$RESTART_STATE" 2>/dev/null || echo 0)
  fi
  NOW=$(now_epoch)
  ELAPSED=$((NOW - LAST_RESTART))

  if [ "$ELAPSED" -lt "$RESTART_COOLDOWN" ]; then
    echo "$LOG_TAG Cooldown active (${ELAPSED}s since last restart). Skipping auto-restart for ${reason}."
    return 0
  fi

  echo "$LOG_TAG Auto-restarting OrbStack: ${reason}"
  echo "$NOW" > "$RESTART_STATE"
  killall OrbStack 2>/dev/null || true
  killall "OrbStack Helper" 2>/dev/null || true
  sleep 5
  open -a OrbStack

  local require_port_forward=false
  if [[ "$reason" == *"localhost published-port forwarding"* ]]; then
    require_port_forward=true
  fi

  for i in $(seq 1 60); do
    sleep 5
    local orb_status=""
    local running_count="0"
    local port_forward_ok=true
    orb_status=$(orbctl status 2>/dev/null || true)
    running_count=$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l | tr -d '[:space:]' || echo 0)
    if [ "$require_port_forward" = true ] && ! localhost_port_forward_healthy; then
      port_forward_ok=false
    fi
    if [ "$orb_status" = "Running" ] && docker info > /dev/null 2>&1 && [ "${running_count:-0}" -ge 15 ] && [ "$port_forward_ok" = true ]; then
      echo "$LOG_TAG OrbStack recovered after ${i}x5s; containers_running=${running_count}; port_forward_ok=${port_forward_ok}."
      send_telegram "$telegram_msg
복구됨 (${i}x5초, containers=${running_count}, port_forward=${port_forward_ok})"
      return 0
    fi
  done

  echo "$LOG_TAG FAILED: OrbStack did not recover after 300s."
  send_telegram "🚨 *OrbStack 복구 실패*
${reason} → 자동 재시작 시도 → 300초 후에도 복구 안 됨
수동 확인 필요"
  return 1
}

localhost_port_forward_healthy() {
  local failures=0
  local checked=0
  local spec name port path code
  # These are host-published ports backed by containers that should answer
  # quickly when OrbStack's localhost forwarding layer is healthy. Use paths
  # that are unauthenticated and cheap; public/docker-network probes remain
  # the service-health source of truth.
  for spec in \
    "beszel:8090:/api/health" \
    "loki:3100:/ready" \
    "uptime-kuma:3001:/dashboard" \
    "open-webui:8080:/health"; do
    IFS=: read -r name port path <<<"$spec"
    if ! docker inspect "$name" >/dev/null 2>&1; then
      continue
    fi
    checked=$((checked + 1))
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${port}${path}" 2>/dev/null || echo 000)
    case "$code" in
      2*|3*) ;;
      *)
        failures=$((failures + 1))
        echo "$LOG_TAG localhost forward probe failed: ${name} 127.0.0.1:${port}${path} code=${code}"
        ;;
    esac
  done

  # Treat two or more simultaneous published-port failures as an OrbStack
  # forwarding-layer fault, not an individual service failure.
  [ "$checked" -gt 0 ] && [ "$failures" -lt 2 ]
}

# ── Check 1: Docker Socket Health ────────────────────────
# This catches the silent-death scenario where OrbStack process exists
# but the Docker socket returns EOF or connection refused.
# IMPORTANT: `docker info` itself hangs when the socket is stuck, so we
# run it in a background subshell with a hard kill timeout. Without this,
# the watchdog gets stuck and can never trigger recovery.

docker_healthy=true
DOCKER_PID=""
# Trap ensures orphaned docker info is killed if watchdog itself is killed
_cleanup_docker_check() { [ -n "$DOCKER_PID" ] && kill -9 "$DOCKER_PID" 2>/dev/null; }
trap '_cleanup_docker_check' INT TERM

docker info > /dev/null 2>&1 &
DOCKER_PID=$!
# 2026-04-22: probe timeout 10s → 20s. Transient I/O slowness under
# memory pressure was tripping the 10s bar and killing a live OrbStack.
for i in $(seq 1 20); do
  if ! kill -0 "$DOCKER_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
if kill -0 "$DOCKER_PID" 2>/dev/null; then
  # docker info hung for 20 seconds — socket is truly stuck
  kill -9 "$DOCKER_PID" 2>/dev/null
  wait "$DOCKER_PID" 2>/dev/null || true
  docker_healthy=false
  echo "$LOG_TAG docker info hung for 20s — socket stuck"
else
  docker_exit=0
  wait "$DOCKER_PID" 2>/dev/null || docker_exit=$?
  if [ "$docker_exit" -ne 0 ]; then
    docker_healthy=false
    echo "$LOG_TAG docker info exited ${docker_exit} — socket unavailable"
  fi
fi
DOCKER_PID=""
trap - INT TERM

if [ "$docker_healthy" = false ]; then
  echo "$LOG_TAG CRITICAL: Docker socket unresponsive."
  restart_orbstack "Docker socket unresponsive" "🔄 *OrbStack 자동 복구 완료*
Docker 소켓 응답 없음 감지 → 자동 재시작"
  exit $?
fi

# ── Check 2: localhost port forwarding ────────────────────
# Docker can be healthy while OrbStack's host-published localhost forwarding
# is wedged. This breaks local agents/probes even though service-to-service
# Docker networking and public Cloudflare/nginx routes still work.
if ! localhost_port_forward_healthy; then
  PREV_PORT_COUNT=0
  if [ -f "$PORT_STATE_FILE" ]; then
    PREV_PORT_COUNT=$(cat "$PORT_STATE_FILE" 2>/dev/null || echo 0)
  fi
  NEW_PORT_COUNT=$((PREV_PORT_COUNT + 1))
  echo "$NEW_PORT_COUNT" > "$PORT_STATE_FILE"

  if [ "$NEW_PORT_COUNT" -ge "$CONSECUTIVE_THRESHOLD" ]; then
    echo "$LOG_TAG CRITICAL: localhost published-port forwarding failed for ${NEW_PORT_COUNT} consecutive checks."
    echo "0" > "$PORT_STATE_FILE"
    restart_orbstack "localhost published-port forwarding reset" "🔄 *OrbStack 자동 복구 완료*
localhost published-port forwarding reset 감지 → 자동 재시작"
    exit $?
  fi
  echo "$LOG_TAG WARNING: localhost published-port forwarding failed (check ${NEW_PORT_COUNT}/${CONSECUTIVE_THRESHOLD})"
else
  if [ -f "$PORT_STATE_FILE" ]; then
    echo "0" > "$PORT_STATE_FILE"
  fi
fi

# ── Check 3: High CPU (existing logic) ──────────────────
# Sum all OrbStack Helper processes. `ps aux | awk ...` can return multiple
# rows; using a single row caused noisy integer parsing when helpers fork.
read -r CPU MEM_RSS_MB <<EOF
$(ps -axo rss,%cpu,comm | awk '
  /OrbStack Helper/ { rss += $1; cpu += $2 }
  END { printf "%d %d", cpu, rss / 1024 }
')
EOF
CPU=${CPU:-0}
MEM_RSS_MB=${MEM_RSS_MB:-0}

if [ "$CPU" -gt "$CPU_THRESHOLD" ]; then
  PREV_COUNT=0
  if [ -f "$STATE_FILE" ]; then
    PREV_COUNT=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  fi
  NEW_COUNT=$((PREV_COUNT + 1))
  echo "$NEW_COUNT" > "$STATE_FILE"

  if [ "$NEW_COUNT" -ge "$CONSECUTIVE_THRESHOLD" ]; then
    echo "$LOG_TAG ALERT: OrbStack Helper at ${CPU}% CPU for ${NEW_COUNT} consecutive checks."

    # Auto-restart on sustained high CPU too (with cooldown)
    LAST_RESTART=0
    if [ -f "$RESTART_STATE" ]; then
      LAST_RESTART=$(cat "$RESTART_STATE" 2>/dev/null || echo 0)
    fi
    NOW=$(now_epoch)
    ELAPSED=$((NOW - LAST_RESTART))

    if [ "$ELAPSED" -ge "$RESTART_COOLDOWN" ]; then
      restart_orbstack "sustained high CPU (${CPU}%)" "🔄 *OrbStack 자동 재시작*
CPU: ${CPU}% (${NEW_COUNT}회 연속 초과) → 자동 재시작 실행"
    else
      send_telegram "⚠️ *OrbStack Helper 고CPU 경고*
CPU: ${CPU}% (${NEW_COUNT}회 연속 초과)
최근 재시작됨 (${ELAPSED}초 전) — 쿨다운 중"
    fi

    echo "0" > "$STATE_FILE"
  else
    echo "$LOG_TAG WARNING: OrbStack Helper at ${CPU}% CPU (check ${NEW_COUNT}/${CONSECUTIVE_THRESHOLD})"
  fi
else
  if [ -f "$STATE_FILE" ]; then
    echo "0" > "$STATE_FILE"
  fi
fi

# ── Check 3: High RSS alert only ─────────────────────────
# Conservative by design: no auto-restart for memory. macOS RSS can include
# VM/cache/accounting overhead; alert only when sustained high RSS coincides
# with actual pressure/swap stress so Telegram does not get hourly noise.
PRESSURE_FREE_PCT=100
PRESSURE_OUT=$(memory_pressure 2>/dev/null || true)
if echo "$PRESSURE_OUT" | grep -q 'System-wide memory free percentage:'; then
  PRESSURE_FREE_PCT=$(echo "$PRESSURE_OUT" | awk -F': ' '/System-wide memory free percentage:/ { gsub(/%/, "", $2); print int($2); exit }')
fi
SWAP_USED_MB=0
SWAP_OUT=$(sysctl vm.swapusage 2>/dev/null || true)
if echo "$SWAP_OUT" | grep -q 'used ='; then
  SWAP_USED_MB=$(echo "$SWAP_OUT" | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p' | awk '{ printf "%d", $1 }')
fi
MEM_PRESSURE_ACTIVE=false
if [ "$PRESSURE_FREE_PCT" -le "$MEM_FREE_PCT_THRESHOLD" ] || [ "$SWAP_USED_MB" -ge "$MEM_SWAP_THRESHOLD_MB" ]; then
  MEM_PRESSURE_ACTIVE=true
fi

if [ "$MEM_RSS_MB" -gt "$MEM_RSS_THRESHOLD_MB" ] && [ "$MEM_PRESSURE_ACTIVE" = true ]; then
  PREV_MEM_COUNT=0
  if [ -f "$MEM_STATE_FILE" ]; then
    PREV_MEM_COUNT=$(cat "$MEM_STATE_FILE" 2>/dev/null || echo 0)
  fi
  NEW_MEM_COUNT=$((PREV_MEM_COUNT + 1))
  echo "$NEW_MEM_COUNT" > "$MEM_STATE_FILE"

  if [ "$NEW_MEM_COUNT" -ge "$CONSECUTIVE_THRESHOLD" ]; then
    echo "$LOG_TAG ALERT: OrbStack Helper RSS ${MEM_RSS_MB}MB for ${NEW_MEM_COUNT} consecutive checks."
    LAST_MEM_ALERT=0
    if [ -f "$MEM_ALERT_STATE" ]; then
      LAST_MEM_ALERT=$(cat "$MEM_ALERT_STATE" 2>/dev/null || echo 0)
    fi
    NOW=$(now_epoch)
    ELAPSED=$((NOW - LAST_MEM_ALERT))
    if [ "$ELAPSED" -ge "$MEM_ALERT_COOLDOWN" ]; then
      echo "$NOW" > "$MEM_ALERT_STATE"
      send_telegram "⚠️ *OrbStack 메모리 경고*
Helper RSS: ${MEM_RSS_MB}MB (${NEW_MEM_COUNT}회 연속, 임계값 ${MEM_RSS_THRESHOLD_MB}MB)
pressure_free=${PRESSURE_FREE_PCT}%, swap_used=${SWAP_USED_MB}MB
자동 재시작은 하지 않음 — safe window에서 수동 판단 권장"
    else
      echo "$LOG_TAG Memory alert cooldown active (${ELAPSED}s since last alert)."
    fi
    echo "0" > "$MEM_STATE_FILE"
  else
    echo "$LOG_TAG WARNING: OrbStack Helper RSS ${MEM_RSS_MB}MB (check ${NEW_MEM_COUNT}/${CONSECUTIVE_THRESHOLD})"
  fi
else
  if [ -f "$MEM_STATE_FILE" ]; then
    echo "0" > "$MEM_STATE_FILE"
  fi
  echo "$LOG_TAG OK: Docker healthy, OrbStack Helper CPU=${CPU}% RSS=${MEM_RSS_MB}MB pressure_free=${PRESSURE_FREE_PCT}% swap_used=${SWAP_USED_MB}MB"
fi
