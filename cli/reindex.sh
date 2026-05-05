#!/bin/zsh
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.docker/bin:$PATH"

# launchd commonly starts jobs with a low soft maxfiles limit (often 256),
# which is too small for Qdrant-heavy reindex batches. Raise the per-job
# limit without changing global launchd/system limits.
ulimit -n 65536 2>/dev/null || ulimit -n 8192 2>/dev/null || true

LOG_DIR="/Users/chrischo/server/brain/logs"
mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$LOG_DIR/reindex-$TS.log"

{
  echo "[$(date)] RAG incremental reindex start"
  "${BRAIN_PYTHON:-/Users/chrischo/server/brain/.venv/bin/python}" /Users/chrischo/server/brain/brain_core/indexer.py
  echo "[$(date)] Noise cleanup"
  echo 'DEPRECATED: cleanup_noise.py removed'
  echo "[$(date)] RAG reindex done"
} >> "$LOG_FILE" 2>&1
