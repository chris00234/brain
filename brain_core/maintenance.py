"""brain_core/maintenance.py — scheduled maintenance tasks.

D3: Job log rotation — truncate logs older than 7 days, cap at 1MB.
D4: ChromaDB integrity check — PRAGMA integrity_check on the SQLite file.

Called by the scheduler as fire-and-forget jobs. Alerts via openclaw_dispatch
to Jenna if integrity check fails.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import subprocess as _sp

try:
    from config import BRAIN_LOGS_DIR as LOGS_DIR, JOBS_LOGS_DIR, CHROMA_DB, OPENCLAW_BIN
except ImportError:
    LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    JOBS_LOGS_DIR = LOGS_DIR / "jobs"
    CHROMA_DB = Path("/Users/chrischo/server/rag/chroma-data/chroma.sqlite3")
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
MAX_LOG_SIZE = 524_288  # 512KB
MAX_LOG_AGE_DAYS = 3


def rotate_logs() -> dict:
    """Truncate job logs older than 3 days or larger than 512KB.

    Uses write-to-tmp + os.replace so concurrent log writers can't lose lines
    between the tail read and the truncating write (the server writes to its
    own server.log continuously; the old `f.write_text(tail.stdout)` path
    opened `w` mode which truncates, racing any in-flight writes).
    """
    import os as _os
    rotated = 0
    cutoff = datetime.now() - timedelta(days=MAX_LOG_AGE_DAYS)

    def _atomic_tail_rewrite(f: Path, lines: int) -> bool:
        try:
            tail = _sp.run(["tail", "-n", str(lines), str(f)], capture_output=True, text=True, timeout=10)
            tmp = f.with_suffix(f.suffix + ".rot.tmp")
            tmp.write_text(tail.stdout)
            _os.replace(tmp, f)
            return True
        except Exception:
            return False

    for log_dir in [JOBS_LOGS_DIR, LOGS_DIR]:
        if not log_dir.exists():
            continue
        for f in log_dir.glob("*.log"):
            try:
                stat = f.stat()
                too_old = datetime.fromtimestamp(stat.st_mtime) < cutoff
                too_big = stat.st_size > MAX_LOG_SIZE
                if too_old or too_big:
                    if _atomic_tail_rewrite(f, 100):
                        rotated += 1
            except Exception:
                pass

    # Prune timestamped reindex logs — keep last 7
    reindex_logs = sorted(LOGS_DIR.glob("reindex-*.log"), key=lambda f: f.name)
    if len(reindex_logs) > 7:
        for old_log in reindex_logs[:-7]:
            try:
                old_log.unlink()
                rotated += 1
            except Exception:
                pass

    # Also rotate error logs
    for f in LOGS_DIR.glob("*.err.log"):
        try:
            if f.stat().st_size > MAX_LOG_SIZE:
                if _atomic_tail_rewrite(f, 50):
                    rotated += 1
        except Exception:
            pass

    print(f"[log_rotation] rotated {rotated} files")
    return {"rotated": rotated}


def check_chroma_integrity() -> dict:
    """Run PRAGMA integrity_check on ChromaDB SQLite file.

    Copies the file first (to avoid locking the live DB), then runs the check.
    Alerts via Jenna Telegram if corruption is detected.
    """
    if not CHROMA_DB.exists():
        return {"status": "skip", "reason": "chroma.sqlite3 not found"}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = Path(tmpdir) / "check.sqlite3"
        try:
            import shutil
            shutil.copy2(CHROMA_DB, tmp_db)
            for suffix in ("-shm", "-wal"):
                src = CHROMA_DB.parent / f"chroma.sqlite3{suffix}"
                if src.exists():
                    shutil.copy2(src, Path(tmpdir) / f"check.sqlite3{suffix}")
        except Exception as e:
            return {"status": "error", "reason": f"copy failed: {e}"}

        try:
            conn = sqlite3.connect(str(tmp_db))
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
        except Exception as e:
            return {"status": "error", "reason": f"integrity_check failed: {e}"}

    status = result[0] if result else "unknown"
    if status == "ok":
        print(f"[chroma_integrity] OK — {CHROMA_DB.stat().st_size / 1024 / 1024:.1f}MB")
        return {"status": "ok", "size_mb": round(CHROMA_DB.stat().st_size / 1024 / 1024, 1)}

    # Corruption detected — alert via Jenna
    msg = f"ALERT: ChromaDB integrity check FAILED: {status}. Immediate attention required."
    print(f"[chroma_integrity] {msg}")
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from openclaw_dispatch import dispatch
        dispatch(agent="jenna", message=msg, thinking="off", timeout=30)
    except Exception:
        pass

    return {"status": "corrupted", "detail": status}


def vacuum_embed_cache(max_size_mb: int = 100, max_age_days: int = 180) -> dict:
    """Evict old embedding cache entries (TTL-based) then VACUUM if over size limit."""
    try:
        from config import EMBED_CACHE_DB
    except ImportError:
        EMBED_CACHE_DB = LOGS_DIR / "embedding_cache.db"
    if not EMBED_CACHE_DB.exists():
        return {"status": "skip", "reason": "no cache file"}
    size_mb = EMBED_CACHE_DB.stat().st_size / 1024 / 1024
    if size_mb <= max_size_mb:
        print(f"[embed_cache_vacuum] {size_mb:.0f}MB <= {max_size_mb}MB, skipping")
        return {"status": "ok", "size_mb": round(size_mb, 1)}
    try:
        conn = sqlite3.connect(str(EMBED_CACHE_DB), isolation_level=None)
        count_before = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        # Prefer TTL-based eviction if created_at is populated
        has_ts = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE created_at != '' AND created_at IS NOT NULL"
        ).fetchone()[0]
        if has_ts > count_before * 0.5:
            conn.execute(
                "DELETE FROM embeddings WHERE created_at != '' AND julianday('now') - julianday(created_at) > ?",
                (max_age_days,),
            )
        else:
            # Fallback: rowid-based for legacy entries without timestamps
            keep = int(count_before * 0.8)
            conn.execute(f"DELETE FROM embeddings WHERE rowid NOT IN (SELECT rowid FROM embeddings ORDER BY rowid DESC LIMIT {keep})")
        conn.execute("VACUUM")
        count_after = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        conn.close()
        new_size = EMBED_CACHE_DB.stat().st_size / 1024 / 1024
        print(f"[embed_cache_vacuum] {count_before} → {count_after} entries, {size_mb:.0f}MB → {new_size:.0f}MB")
        return {"status": "vacuumed", "before": count_before, "after": count_after, "size_mb": round(new_size, 1)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def prune_memory_access(keep_days: int = 180) -> dict:
    """Prune memory_access rows older than keep_days. Prevents unbounded growth."""
    try:
        db_path = LOGS_DIR / "autonomy.db"
        if not db_path.exists():
            return {"status": "skip", "reason": "no autonomy.db"}
        conn = sqlite3.connect(str(db_path))
        before = conn.execute("SELECT COUNT(*) FROM memory_access").fetchone()[0]
        conn.execute(
            "DELETE FROM memory_access WHERE julianday('now') - julianday(last_accessed_at) > ?",
            (keep_days,),
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM memory_access").fetchone()[0]
        conn.close()
        pruned = before - after
        if pruned:
            print(f"[prune_memory_access] {before} → {after} rows (pruned {pruned} older than {keep_days}d)")
        return {"status": "ok", "before": before, "after": after, "pruned": pruned}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def rotate_jsonl_logs(max_lines: int = 500) -> dict:
    """Truncate JSONL failure logs to last max_lines entries. Prevents unbounded growth.

    Atomic write: tail to .tmp + os.replace, so a crash mid-rotation can't
    truncate or corrupt the source file.
    """
    import os as _os
    rotated = 0
    for f in LOGS_DIR.glob("*.jsonl"):
        try:
            lines = f.read_text().splitlines()
            if len(lines) > max_lines:
                tmp = f.with_suffix(f.suffix + ".rot.tmp")
                tmp.write_text("\n".join(lines[-max_lines:]) + "\n")
                _os.replace(tmp, f)
                rotated += 1
        except Exception:
            pass
    if rotated:
        print(f"[rotate_jsonl] truncated {rotated} JSONL logs to {max_lines} lines")
    return {"rotated": rotated}


def prune_raw_inbox(max_age_days: int = 30) -> dict:
    """Move raw inbox records older than max_age_days to raw/orphaned/ instead of deleting."""
    try:
        inbox = Path("/Users/chrischo/server/knowledge/raw/inbox")
        if not inbox.exists():
            return {"status": "skip"}
        orphaned_dir = inbox.parent / "orphaned"
        orphaned_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now() - timedelta(days=max_age_days)
        moved = 0
        for f in inbox.glob("*.json"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.rename(orphaned_dir / f.name)
                    moved += 1
            except Exception:
                pass
        orphaned_count = sum(1 for _ in orphaned_dir.glob("*.json"))
        if moved:
            print(f"[prune_raw_inbox] moved {moved} records older than {max_age_days}d to raw/orphaned/ (total orphaned: {orphaned_count})")
        return {"status": "ok", "moved": moved, "orphaned_total": orphaned_count}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def vacuum_autonomy_db() -> dict:
    """VACUUM autonomy.db to reclaim space after DELETE/UPDATE churn."""
    try:
        db_path = LOGS_DIR / "autonomy.db"
        if not db_path.exists():
            return {"status": "skip", "reason": "no autonomy.db"}
        size_before = db_path.stat().st_size / 1024
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("VACUUM")
        conn.close()
        size_after = db_path.stat().st_size / 1024
        if size_before != size_after:
            print(f"[vacuum_autonomy_db] {size_before:.0f}KB → {size_after:.0f}KB")
        return {"status": "ok", "before_kb": round(size_before), "after_kb": round(size_after)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def incremental_stale_cleanup() -> dict:
    """Check collections for stale/orphaned docs and remove them.

    Unlike full-reindex stale cleanup, this runs weekly and only removes docs
    whose source files no longer exist on disk. Does not require re-embedding.
    """
    try:
        from indexer import chroma_api, _get_collection_id
    except ImportError:
        return {"status": "error", "reason": "indexer not importable"}

    collections = ["knowledge", "experience", "canonical"]
    total_cleaned = 0

    for col_name in collections:
        col_id = _get_collection_id(col_name)
        if not col_id:
            continue
        try:
            count = chroma_api("GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count")
            count = int(count) if isinstance(count, (int, str)) else 0
            if count == 0:
                continue
            resp = chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get", {
                "limit": count, "include": ["metadatas"],
            })
            ids = resp.get("ids", [])
            metas = resp.get("metadatas", [])
            stale = []
            for doc_id, meta in zip(ids, metas):
                source = (meta or {}).get("source", "")
                if source and source.startswith("/") and not Path(source).exists():
                    stale.append(doc_id)
            if stale:
                BATCH = 20
                for s in range(0, len(stale), BATCH):
                    chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete", {
                        "ids": stale[s:s+BATCH],
                    })
                print(f"[stale_cleanup] {col_name}: removed {len(stale)} orphaned docs")
                total_cleaned += len(stale)
        except Exception as e:
            print(f"[stale_cleanup] {col_name}: error — {e}")

    return {"status": "ok", "total_cleaned": total_cleaned}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("task", choices=[
        "rotate_logs", "chroma_integrity", "vacuum_embed_cache",
        "prune_memory_access", "rotate_jsonl", "prune_raw_inbox",
        "vacuum_autonomy_db", "stale_cleanup", "all_cleanup",
    ])
    args = parser.parse_args()

    if args.task == "rotate_logs":
        print(json.dumps(rotate_logs()))
    elif args.task == "chroma_integrity":
        print(json.dumps(check_chroma_integrity()))
    elif args.task == "vacuum_embed_cache":
        print(json.dumps(vacuum_embed_cache()))
    elif args.task == "prune_memory_access":
        print(json.dumps(prune_memory_access()))
    elif args.task == "rotate_jsonl":
        print(json.dumps(rotate_jsonl_logs()))
    elif args.task == "prune_raw_inbox":
        print(json.dumps(prune_raw_inbox()))
    elif args.task == "vacuum_autonomy_db":
        print(json.dumps(vacuum_autonomy_db()))
    elif args.task == "stale_cleanup":
        print(json.dumps(incremental_stale_cleanup()))
    elif args.task == "all_cleanup":
        results = {
            "rotate_logs": rotate_logs(),
            "rotate_jsonl": rotate_jsonl_logs(),
            "prune_memory_access": prune_memory_access(),
            "prune_raw_inbox": prune_raw_inbox(),
            "vacuum_embed_cache": vacuum_embed_cache(),
            "vacuum_autonomy_db": vacuum_autonomy_db(),
        }
        print(json.dumps(results))
