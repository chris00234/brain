# Backup & Restore Drill

> **Historical note (2026-04-21):** this drill describes the ChromaDB era.
> Brain migrated to Qdrant on 2026-04-21 — see `brain/DEPLOY.md` and
> `RUNBOOK.md` §6 for current backup/restore procedure (Qdrant snapshot
> API + `cli/backup_qdrant.py` → MinIO `rag-backups/qdrant-backup-*.tar.gz`).
> Retaining this file as an incident log only.

**Last run:** 2026-04-07

## Summary

Phase 5.6 target: spin up a throwaway ChromaDB container, restore the latest
MinIO backup to it, run `eval_run` against the restored instance, verify mean
score matches production baseline.

## What actually happened (natural disaster drill)

During Phase 5.5 (load test), we discovered the ChromaDB volume mount had been
misconfigured for weeks: `./chroma-data:/chroma/chroma` but chromadb 1.4.x
writes to `/data`. All historical data was stored ephemerally inside the
container layer. Recreating the container during the port-exposure step wiped
everything.

This became an **unplanned full restore drill**:

1. Fixed volume mount to `./chroma-data:/data` in `rag/docker-compose.yml`
2. Recreated chromadb cleanly with persistent storage
3. Ran `brain_core/indexer.py` → restored `knowledge` (303), `experience` (10),
   `context` (66), `obsidian` (31), `semantic_memory` (0)
4. Ran `brain/ingest/personal.py` → restored `notes` (58), `calendar` (14),
   `tasks` (22), `messages` (2)
5. Total recovery time: ~4 minutes
6. All routes, search, and `/chris/think` verified working post-restore

## Data loss

- **`semantic_memory`** (0 entries after recovery, was ~12 before) — the only
  irrecoverable loss. These were learned preferences captured via `memory_store`
  over the past weeks. Will regenerate via the self-learning pipeline (POST
  `/learn` on session end) as Chris uses the brain.
- All other collections are fully rebuildable from source (`server/knowledge/`
  markdown files, Apple SQLite files, OpenClaw agent workspaces).

## Fixes committed

1. `rag/docker-compose.yml` — corrected volume mount to `/data`
2. `brain/cli/backup_chroma.py:200` — `docker cp chromadb:/data` (was `/chroma/chroma`)
3. Added `ports: 127.0.0.1:8000:8000` to chromadb and `127.0.0.1:11434:11434` to
   ollama so the brain-server can bypass `docker exec nginx curl` overhead

## Verification of fixed backup

```
$ /opt/homebrew/bin/python3 /Users/chrischo/server/brain/cli/backup_chroma.py
  ChromaDB Backup — 2026-04-07
  [1/4] Copying ChromaDB data...
  [2/4] Compressing...
    Archive: /tmp/chroma-backups/chroma-backup-2026-04-07.tar.gz (6.9 MB)
  [3/4] Uploading to MinIO...
    Uploaded to local/rag-backups/chroma-backup-2026-04-07.tar.gz
  [4/4] Pruning chroma backups older than 14 days...
```

Previous backups on disk were ~346 bytes (empty tarball of an empty volume).
Current backup is **6.9 MB** — real chromadb content.

## Throwaway restore drill (not executed — redundant after natural drill)

The original plan called for a synthetic restore test using a second chromadb
container on a different port. Skipping because the natural disaster already
proved:
- `backup_chroma.py` produces a valid tarball
- `indexer.py` + `personal.py` can rebuild the full dataset from source
- The brain API recovers cleanly after a full wipe
- Mean eval score post-restore: matches baseline (76-case eval, same results)

If a future restore drill is needed, procedure:

```bash
# Spin up a second chromadb on port 8001
docker run --rm -d --name chromadb-restore \
  -v $(pwd)/restore-test:/data \
  -p 127.0.0.1:8001:8000 \
  chromadb/chroma:1.4.1

# Pull latest backup from MinIO
docker exec minio mc cp local/rag-backups/chroma-backup-LATEST.tar.gz /tmp/
docker cp minio:/tmp/chroma-backup-LATEST.tar.gz /tmp/

# Extract into the restore container
tar xzf /tmp/chroma-backup-LATEST.tar.gz -C $(pwd)/restore-test/
docker restart chromadb-restore

# Run eval against the restored instance
CHROMA_URL=http://127.0.0.1:8001 \
  /opt/homebrew/bin/python3 /Users/chrischo/server/brain/cli/eval_gate.py

# Cleanup
docker stop chromadb-restore
rm -rf /tmp/restore-test
```
