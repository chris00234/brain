"""brain_core/maintenance.py — scheduled maintenance tasks.

D3: Job log rotation — truncate logs older than 7 days, cap at 1MB.
D4: Qdrant collections integrity check — readyz probe + points count sanity.

Called by the scheduler as fire-and-forget jobs. Alerts via openclaw_dispatch
to Jenna if integrity check fails.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import subprocess as _sp
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from config import BRAIN_LOGS_DIR as LOGS_DIR
    from config import JOBS_LOGS_DIR, OPENCLAW_BIN
except ImportError:
    LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    JOBS_LOGS_DIR = LOGS_DIR / "jobs"
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
    cutoff = datetime.now(UTC) - timedelta(days=MAX_LOG_AGE_DAYS)

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
                too_old = datetime.fromtimestamp(stat.st_mtime, tz=UTC) < cutoff
                too_big = stat.st_size > MAX_LOG_SIZE
                if (too_old or too_big) and _atomic_tail_rewrite(f, 100):
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
            if f.stat().st_size > MAX_LOG_SIZE and _atomic_tail_rewrite(f, 50):
                rotated += 1
        except Exception:
            pass

    print(f"[log_rotation] rotated {rotated} files")
    return {"rotated": rotated}


def vacuum_embed_cache(max_size_mb: int = 100, max_age_days: int = 14) -> dict:
    """Evict old embedding cache entries (TTL-based) then VACUUM if over size limit.

    TTL ceiling lowered from 180d → 14d on 2026-04-12 — the prior default
    never matched any rows (oldest entry was 3 days old) so the vacuum was
    a nightly no-op and the cache grew to 311 MB. Embeddings are cheap to
    recompute via Ollama; 14 days is plenty of hit-ratio benefit.
    """
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
        deleted_by_ttl = 0
        if has_ts > count_before * 0.5:
            before_ttl = conn.total_changes
            conn.execute(
                "DELETE FROM embeddings WHERE created_at != '' AND julianday('now') - julianday(created_at) > ?",
                (max_age_days,),
            )
            deleted_by_ttl = conn.total_changes - before_ttl
        else:
            # Fallback: rowid-based for legacy entries without timestamps
            keep = int(count_before * 0.8)
            conn.execute(
                "DELETE FROM embeddings WHERE rowid NOT IN (SELECT rowid FROM embeddings ORDER BY rowid DESC LIMIT ?)",
                (keep,),
            )
        # Size cap is the actual SLO guard. TTL alone can leave a fresh ingest
        # burst at 800MB+ for days, so after TTL pruning estimate a newest-row
        # cap proportional to the target and trim oldest rows before VACUUM.
        remaining = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if size_mb > max_size_mb and remaining > 0:
            keep_ratio = min(1.0, max(0.05, (max_size_mb / size_mb) * 1.15))
            keep_rows = max(1000, int(remaining * keep_ratio))
            if keep_rows < remaining:
                conn.execute(
                    "DELETE FROM embeddings WHERE rowid NOT IN "
                    "(SELECT rowid FROM embeddings ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                    (keep_rows,),
                )
        conn.execute("VACUUM")
        count_after = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        conn.close()
        new_size = EMBED_CACHE_DB.stat().st_size / 1024 / 1024
        print(
            f"[embed_cache_vacuum] {count_before} → {count_after} entries, {size_mb:.0f}MB → {new_size:.0f}MB"
        )
        return {
            "status": "vacuumed",
            "before": count_before,
            "after": count_after,
            "deleted_by_ttl": deleted_by_ttl,
            "size_mb": round(new_size, 1),
        }
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

    Per-file aggressive caps (2026-04-12): some JSONL logs have ~5KB per line
    (full openclaw command + stack trace), so the default 500-line cap still
    lets them bloat to ~2.5 MB. Aggressive caps apply a tighter floor for
    known-noisy logs.
    """
    import os as _os

    AGGRESSIVE_CAPS = {
        "dispatch-failures.jsonl": 100,  # ~5KB per line → ~500KB ceiling
    }
    rotated = 0
    for f in LOGS_DIR.glob("*.jsonl"):
        try:
            cap = AGGRESSIVE_CAPS.get(f.name, max_lines)
            lines = f.read_text().splitlines()
            if len(lines) > cap:
                tmp = f.with_suffix(f.suffix + ".rot.tmp")
                tmp.write_text("\n".join(lines[-cap:]) + "\n")
                _os.replace(tmp, f)
                rotated += 1
        except Exception:
            pass
    if rotated:
        print(f"[rotate_jsonl] truncated {rotated} JSONL logs")
    return {"rotated": rotated}


def prune_scheduler_history(keep_days: int = 30) -> dict:
    """Prune rows older than keep_days from scheduler_history.db job_history table.

    Preventive: currently ~75 rows/day, so at 30 days the table holds ~2250 rows
    (~1 MB). Without this the DB would grow forever. Added 2026-04-12.
    """
    db = LOGS_DIR / "scheduler_history.db"
    if not db.exists():
        return {"status": "skip", "reason": "no scheduler_history.db"}
    try:
        conn = sqlite3.connect(str(db))
        before = conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
        conn.execute(
            "DELETE FROM job_history WHERE julianday('now') - julianday(started_at) > ?",
            (keep_days,),
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
        conn.close()
        deleted = before - after
        if deleted:
            print(
                f"[prune_scheduler_history] {before} → {after} rows (deleted {deleted} older than {keep_days}d)"
            )
        return {"status": "ok", "before": before, "after": after, "deleted": deleted}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def prune_raw_inbox(max_age_days: int = 30) -> dict:
    """Move raw inbox records older than max_age_days to raw/orphaned/ instead of deleting."""
    try:
        inbox = Path("/Users/chrischo/server/knowledge/raw/inbox")
        if not inbox.exists():
            return {"status": "skip"}
        orphaned_dir = inbox.parent / "orphaned"
        orphaned_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        moved = 0
        for f in inbox.glob("*.json"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, tz=UTC) < cutoff:
                    f.rename(orphaned_dir / f.name)
                    moved += 1
            except Exception:
                pass
        orphaned_count = sum(1 for _ in orphaned_dir.glob("*.json"))
        if moved:
            print(
                f"[prune_raw_inbox] moved {moved} records older than {max_age_days}d to raw/orphaned/ (total orphaned: {orphaned_count})"
            )
        return {"status": "ok", "moved": moved, "orphaned_total": orphaned_count}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def prune_raw_orphaned(max_age_days: int = 180) -> dict:
    """Delete raw/orphaned/ records older than max_age_days.

    2026-04-16 Tier 2 fix: prune_raw_inbox only moves old inbox records
    into raw/orphaned/; nothing ever cleaned orphaned itself, so the
    directory grew without bound. 180-day retention is long enough to
    preserve any forensic lookups after a bad promotion decision.
    """
    try:
        orphaned = Path("/Users/chrischo/server/knowledge/raw/orphaned")
        if not orphaned.exists():
            return {"status": "skip"}
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        deleted = 0
        for f in orphaned.glob("*.json"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, tz=UTC) < cutoff:
                    f.unlink()
                    deleted += 1
            except Exception:
                pass
        remaining = sum(1 for _ in orphaned.glob("*.json"))
        if deleted:
            print(
                f"[prune_raw_orphaned] deleted {deleted} records older than {max_age_days}d (remaining: {remaining})"
            )
        return {"status": "ok", "deleted": deleted, "remaining": remaining}
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


CANONICAL_DIR = Path("/Users/chrischo/server/knowledge/canonical")
INFRA_KEYWORDS = re.compile(
    r"\b(docker|container|service|port|launchd|plist|nginx|chromadb|ollama|neo4j|orbstack)\b",
    re.IGNORECASE,
)
PORT_RE = re.compile(
    r"\bport\s*[:=]?\s*(\d{2,5})\b|\b(\d{4,5})\s*/\s*tcp\b|localhost:(\d{2,5})|127\.0\.0\.1:(\d{2,5})",
    re.IGNORECASE,
)
DOCKER_NAME_RE = re.compile(
    r"\b(container|service|name)\s*[:=]?\s*[\"']?([a-z][a-z0-9_-]+)[\"']?", re.IGNORECASE
)
LAUNCHD_RE = re.compile(r"\b(ai\.openclaw\.[a-z0-9._-]+)\b")


def _find_infra_notes() -> list[Path]:
    """Find canonical markdown files that mention infrastructure keywords."""
    notes = []
    for md in CANONICAL_DIR.rglob("*.md"):
        try:
            text = md.read_text(errors="replace")[:8000]
            if INFRA_KEYWORDS.search(text):
                notes.append(md)
        except Exception:
            pass
    return notes


def _extract_facts(text: str) -> dict:
    """Extract ports, Docker service names, and launchd labels from text."""
    ports: set[int] = set()
    for m in PORT_RE.finditer(text):
        raw = next(g for g in m.groups() if g is not None)
        p = int(raw)
        if 80 <= p <= 65535:
            ports.add(p)

    docker_names: set[str] = set()
    for m in DOCKER_NAME_RE.finditer(text):
        name = m.group(2)
        if len(name) >= 3 and name not in ("the", "and", "for", "not", "all"):
            docker_names.add(name)

    launchd_labels: set[str] = set()
    for m in LAUNCHD_RE.finditer(text):
        launchd_labels.add(m.group(1))

    return {"ports": sorted(ports), "docker": sorted(docker_names), "launchd": sorted(launchd_labels)}


def validate_infra_facts() -> dict:
    """Cross-check canonical infra notes against live Docker/launchd/port state.

    Returns {checked, stale, timestamp} and writes to logs/infra_validation.json.
    """
    notes = _find_infra_notes()
    if not notes:
        return {"checked": 0, "stale": [], "timestamp": datetime.now(UTC).isoformat()}

    # Gather live state
    try:
        docker_out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        live_docker = set(docker_out.stdout.strip().splitlines()) if docker_out.returncode == 0 else set()
    except Exception:
        live_docker = set()

    try:
        launchd_out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        live_launchd = set()
        for line in launchd_out.stdout.splitlines()[1:]:  # skip header
            parts = line.split("\t")
            if len(parts) >= 3:
                live_launchd.add(parts[2])
    except Exception:
        live_launchd = set()

    def _port_listening(port: int) -> bool:
        try:
            r = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0 and len(r.stdout.strip().splitlines()) > 1
        except Exception:
            return False

    stale: list[dict] = []
    checked = 0

    for note_path in notes:
        try:
            text = note_path.read_text(errors="replace")
        except Exception:
            continue
        facts = _extract_facts(text)
        rel = str(note_path.relative_to(CANONICAL_DIR))

        for name in facts["docker"]:
            checked += 1
            if name not in live_docker:
                stale.append({"note": rel, "type": "docker", "name": name, "issue": "not running"})

        for label in facts["launchd"]:
            checked += 1
            if label not in live_launchd:
                stale.append({"note": rel, "type": "launchd", "name": label, "issue": "not loaded"})

        for port in facts["ports"]:
            checked += 1
            if not _port_listening(port):
                stale.append({"note": rel, "type": "port", "name": str(port), "issue": "nothing listening"})

    result = {
        "checked": checked,
        "stale": stale,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # Persist
    try:
        out_path = LOGS_DIR / "infra_validation.json"
        out_path.write_text(json.dumps(result, indent=2))
    except Exception:
        pass

    if stale:
        print(f"[infra_validation] {checked} facts checked, {len(stale)} stale")
    else:
        print(f"[infra_validation] {checked} facts checked, all OK")
    return result


def incremental_stale_cleanup() -> dict:
    """Check collections for stale/orphaned docs and remove them.

    Unlike full-reindex stale cleanup, this runs weekly and only removes docs
    whose source files no longer exist on disk. Does not require re-embedding.
    """
    try:
        from vector_store import get_vector_store
    except ImportError:
        return {"status": "error", "reason": "vector_store not importable"}

    store = get_vector_store()
    collections = ["knowledge", "experience", "canonical"]
    total_cleaned = 0

    # Page through each collection via cursor so a single `get(limit=count)`
    # can't materialize 500k+ payloads in memory at once.
    PAGE = 1000
    for col_name in collections:
        try:
            count = store.count(col_name)
            if count == 0:
                continue
            stale: list[str] = []
            cursor = None
            while True:
                page = store.get(
                    col_name,
                    limit=PAGE,
                    offset=cursor,
                    with_payload=True,
                    with_documents=False,
                )
                if not page:
                    break
                for p in page:
                    source = (p.payload or {}).get("source", "")
                    if source and source.startswith("/") and not Path(source).exists():
                        stale.append(p.id)
                if len(page) < PAGE:
                    break
                cursor = page[-1].id
            if stale:
                BATCH = 20
                for s in range(0, len(stale), BATCH):
                    store.delete(col_name, stale[s : s + BATCH])
                print(f"[stale_cleanup] {col_name}: removed {len(stale)} orphaned docs")
                total_cleaned += len(stale)
        except Exception as e:
            print(f"[stale_cleanup] {col_name}: error — {e}")

    return {"status": "ok", "total_cleaned": total_cleaned}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "task",
        choices=[
            "rotate_logs",
            "vacuum_embed_cache",
            "prune_memory_access",
            "rotate_jsonl",
            "prune_raw_inbox",
            "prune_raw_orphaned",
            "vacuum_autonomy_db",
            "stale_cleanup",
            "validate_infra",
            "all_cleanup",
            "prune_scheduler_history",
        ],
    )
    args = parser.parse_args()

    if args.task == "rotate_logs":
        print(json.dumps(rotate_logs()))
    elif args.task == "vacuum_embed_cache":
        print(json.dumps(vacuum_embed_cache()))
    elif args.task == "prune_memory_access":
        print(json.dumps(prune_memory_access()))
    elif args.task == "rotate_jsonl":
        print(json.dumps(rotate_jsonl_logs()))
    elif args.task == "prune_raw_inbox":
        print(json.dumps(prune_raw_inbox()))
    elif args.task == "prune_raw_orphaned":
        print(json.dumps(prune_raw_orphaned()))
    elif args.task == "vacuum_autonomy_db":
        print(json.dumps(vacuum_autonomy_db()))
    elif args.task == "stale_cleanup":
        print(json.dumps(incremental_stale_cleanup()))
    elif args.task == "validate_infra":
        print(json.dumps(validate_infra_facts()))
    elif args.task == "prune_scheduler_history":
        print(json.dumps(prune_scheduler_history()))
    elif args.task == "all_cleanup":
        results = {
            "rotate_logs": rotate_logs(),
            "rotate_jsonl": rotate_jsonl_logs(),
            "prune_memory_access": prune_memory_access(),
            "prune_raw_inbox": prune_raw_inbox(),
            "vacuum_embed_cache": vacuum_embed_cache(),
            "vacuum_autonomy_db": vacuum_autonomy_db(),
            "prune_scheduler_history": prune_scheduler_history(),
        }
        print(json.dumps(results))
