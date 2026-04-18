#!/bin/bash
# Checks if brain-server is responding and alerts via Jenna if not.
# Designed to be called by the existing ai.openclaw.watchdog launchd plist.

set -u

BRAIN_URL="http://127.0.0.1:8791/healthz"
SECRET_FILE="$HOME/.openclaw/credentials/.personal_webhook_secret"
OPENCLAW_BIN="$HOME/.local/bin/openclaw"
STATE_FILE="/tmp/.brain-watchdog-state"

if [ ! -r "$SECRET_FILE" ]; then
    echo "$(date -Iseconds) watchdog skipped: missing $SECRET_FILE" >&2
    exit 0
fi

# Load the secret into a header file so it never appears in the process table
# (which curl's -H "Authorization: Bearer $SECRET" would expose to every user
# running ps on the machine).
HEADER_FILE=$(mktemp -t brain_wd_hdr_XXXXXX)
trap 'rm -f "$HEADER_FILE"' EXIT
{
    printf 'Authorization: Bearer '
    cat "$SECRET_FILE"
    printf '\n'
} > "$HEADER_FILE"
chmod 600 "$HEADER_FILE"

status=$(curl -s -o /dev/null -w "%{http_code}" -H "@$HEADER_FILE" "$BRAIN_URL" --max-time 5 2>/dev/null)

if [ "$status" = "200" ]; then
    # Healthy — clear alert state
    rm -f "$STATE_FILE" 2>/dev/null
    exit 0
fi

# Unhealthy — check if we already alerted (avoid spam)
if [ -f "$STATE_FILE" ]; then
    age=$(($(date +%s) - $(stat -f %m "$STATE_FILE")))
    if [ "$age" -lt 300 ]; then
        exit 1  # Already alerted within 5 minutes
    fi
fi

touch "$STATE_FILE"

# 2026-04-16 Tier 2 fix: before alerting, attempt self-recovery via
# launchctl kickstart. Previously the watchdog only sent an alert and
# exited 1 — which is fine when launchd's own KeepAlive detects the
# crash, but useless when the process is HUNG (alive but not serving).
# KeepAlive only catches exit codes, not stalls. kickstart -k forces a
# restart whether the process has crashed or hung.
LAUNCHD_LABEL="ai.openclaw.brain-server"
GUI_TARGET="gui/$(id -u)/${LAUNCHD_LABEL}"
launchctl kickstart -k "$GUI_TARGET" >/dev/null 2>&1 || true

"$OPENCLAW_BIN" agent --agent jenna \
    --message "[BRAIN DOWN] brain-server at $BRAIN_URL returned HTTP $status. Watchdog issued \`launchctl kickstart -k $GUI_TARGET\`. Re-check in 30s." \
    --deliver --json --thinking off --timeout 30 2>/dev/null

exit 1
