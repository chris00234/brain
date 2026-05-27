#!/usr/bin/env bash
set -euo pipefail

PROFILES_CSV="${HERMES_GATEWAY_PROFILES:-jenna,liz,ellie,sage,market}"
IFS=',' read -r -a PROFILES <<< "$PROFILES_CSV"
UID_VALUE="$(id -u)"
FAILED=()

for profile in "${PROFILES[@]}"; do
  service="ai.hermes.gateway-${profile}"
  if launchctl print "gui/${UID_VALUE}/${service}" >/dev/null 2>&1; then
    echo "${service} loaded"
    continue
  fi
  if launchctl kickstart -k "gui/${UID_VALUE}/${service}" >/dev/null 2>&1; then
    echo "${service} kickstarted"
    continue
  fi
  FAILED+=("${service}")
done

if ((${#FAILED[@]} > 0)); then
  printf 'Hermes gateway services unavailable: %s
' "${FAILED[*]}" >&2
  exit 1
fi

echo "Hermes gateway services healthy: ${PROFILES_CSV}"
