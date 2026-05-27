#!/bin/bash
set -e

# Rotate OpenClaw and RAG logs daily.
# Keeps last 7 days of compressed archives, deletes older.

LOG_DIRS=(
  "/Users/chrischo/server/brain/logs"
  "/Users/chrischo/server/rag/logs"
)

MAX_SIZE_MB=10
KEEP_DAYS=7

for dir in "${LOG_DIRS[@]}"; do
  [ -d "$dir" ] || continue

  # Rotate any .log file larger than MAX_SIZE_MB
  find "$dir" -maxdepth 2 -name "*.log" -type f | while read -r logfile; do
    size_mb=$(du -m "$logfile" | cut -f1)
    if [ "$size_mb" -gt "$MAX_SIZE_MB" ]; then
      ts=$(date +%Y%m%d-%H%M%S)
      mv "$logfile" "${logfile}.${ts}"
      gzip "${logfile}.${ts}"
      touch "$logfile"
      echo "[rotate] ${logfile} (${size_mb}MB) -> ${logfile}.${ts}.gz"
    fi
  done

  # Delete compressed archives older than KEEP_DAYS
  find "$dir" -maxdepth 2 -name "*.log.*.gz" -type f -mtime +${KEEP_DAYS} -delete
done

echo "[rotate] Done at $(date -Iseconds)"
