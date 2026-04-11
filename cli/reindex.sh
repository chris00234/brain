#!/bin/zsh
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.docker/bin:$PATH"

LOG_DIR="/Users/chrischo/server/brain/logs"
mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$LOG_DIR/reindex-$TS.log"

{
  echo "[$(date)] RAG incremental reindex start"
  /opt/homebrew/bin/python3 /Users/chrischo/server/brain/brain_core/indexer.py
  echo "[$(date)] Noise cleanup"
  echo 'DEPRECATED: cleanup_noise.py removed'
  echo "[$(date)] RAG reindex done"
} >> "$LOG_FILE" 2>&1
