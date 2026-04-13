#!/bin/bash
# Wrapper for ingest_personal.py — runs under /bin/bash so TCC permissions
# (Full Disk Access for chat.db, Apple Events for Notes/Calendar/Reminders)
# bind to a stable parent process binary that doesn't get revoked on Python upgrades.
#
# Grant Full Disk Access to /bin/bash in:
#   System Settings → Privacy & Security → Full Disk Access
#
# Grant Apple Events authorization to /bin/bash in:
#   System Settings → Privacy & Security → Automation
#   (or trigger by running this script once from Terminal.app and accepting the prompts)

set -uo pipefail

export HOME="/Users/chrischo"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

PYTHON="${BRAIN_PYTHON:-/Users/chrischo/server/brain/.venv/bin/python}"
SCRIPT="/Users/chrischo/server/brain/ingest/personal.py"

exec "$PYTHON" "$SCRIPT" "$@"
