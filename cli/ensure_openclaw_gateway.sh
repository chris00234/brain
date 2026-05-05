#!/usr/bin/env bash
set -euo pipefail
PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
SESSION="${OPENCLAW_GATEWAY_SCREEN_SESSION:-openclaw-gateway}"
LOG="${OPENCLAW_GATEWAY_SCREEN_LOG:-/tmp/openclaw-gateway-screen.log}"

socket_ok() {
  python - "$PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
try:
    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
}

if socket_ok; then
  echo "openclaw gateway already listening on 127.0.0.1:${PORT}"
  exit 0
fi

if command -v screen >/dev/null 2>&1; then
  screen -S "$SESSION" -X quit >/dev/null 2>&1 || true
  screen -dmS "$SESSION" /bin/zsh -lc "openclaw gateway run --port '$PORT' >>'$LOG' 2>&1"
  for _ in {1..20}; do
    sleep 1
    if socket_ok; then
      echo "openclaw gateway started in screen session ${SESSION} on 127.0.0.1:${PORT}"
      exit 0
    fi
  done
fi

openclaw gateway start >/tmp/openclaw-gateway-start.log 2>&1 || true
for _ in {1..10}; do
  sleep 1
  if socket_ok; then
    echo "openclaw gateway started via service on 127.0.0.1:${PORT}"
    exit 0
  fi
done

echo "openclaw gateway failed to listen on 127.0.0.1:${PORT}" >&2
exit 1
