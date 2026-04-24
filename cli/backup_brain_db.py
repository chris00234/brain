#!/usr/bin/env python3
"""cli/backup_brain_db.py — daily SQLite .backup of brain.db + autonomy.db.

W3 fix (2026-04-17): brain.db is the atoms truth layer + autonomy state.
ChromaDB had a backup job (ai.openclaw.chroma-backup.plist) but these two
SQLite DBs did not — loss would destroy the durable memory layer.

Strategy:
  - Use SQLite's online .backup API (lock-free, consistent) via Python sqlite3
  - Write to ~/server/brain/logs/backups/<db>-YYYYMMDD.db
  - Rotate: keep last N days (default 14)
  - Invoked daily by launchd (ai.openclaw.brain-backup.plist)

Exit codes:
  0 — all DBs backed up (or no-ops for missing sources)
  1 — at least one backup failed

Log output (JSON on stdout) for observability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("brain.backup_brain_db")

BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
BACKUP_DIR = BRAIN_LOGS_DIR / "backups"
RETENTION_DAYS = 14
MINIO_BUCKET = "rag-backups"
MINIO_PREFIX = "brain-db-backup/"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _upload_to_minio(label: str, local_path: Path) -> dict:
    """Mirror the local backup to MinIO so a disk failure doesn't destroy
    the atoms truth layer. Matches backup_qdrant.py / backup_neo4j.py
    pattern. Returns {status, key?, reason?}.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _minio import s3_client as _s3_client

        s3 = _s3_client()
    except Exception as exc:
        return {"status": "skipped", "reason": f"minio_unavailable: {exc}"}
    try:
        key = f"{MINIO_PREFIX}{local_path.name}"
        s3.upload_file(str(local_path), MINIO_BUCKET, key)
        digest = _sha256_file(local_path)
        checksum_key = key.replace(".db", ".sha256")
        checksum_body = f"{digest}  {local_path.name}\n"
        s3.put_object(Bucket=MINIO_BUCKET, Key=checksum_key, Body=checksum_body.encode())
        return {"status": "ok", "key": key, "sha256": digest}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)[:200]}


DATABASES = [
    ("brain.db", BRAIN_LOGS_DIR / "brain.db"),
    ("autonomy.db", BRAIN_LOGS_DIR / "autonomy.db"),
]


def _backup_one(label: str, src: Path, dest_dir: Path) -> dict:
    result = {"label": label, "src": str(src), "status": "skipped", "bytes": 0, "duration_s": 0.0}
    if not src.exists():
        result["reason"] = "source_missing"
        return result
    today = datetime.now(UTC).strftime("%Y%m%d")
    dest = dest_dir / f"{src.stem}-{today}.db"
    t0 = time.time()
    try:
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        result["status"] = "ok"
        result["dest"] = str(dest)
        result["bytes"] = dest.stat().st_size
        result["duration_s"] = round(time.time() - t0, 3)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:200]
    return result


def _rotate(dest_dir: Path, keep_days: int) -> int:
    """Delete backup files older than keep_days. Returns count deleted."""
    if not dest_dir.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    deleted = 0
    for f in dest_dir.glob("*.db"):
        try:
            # Parse date from filename suffix: <stem>-YYYYMMDD.db
            date_part = f.stem.rsplit("-", 1)[-1]
            if len(date_part) != 8 or not date_part.isdigit():
                continue
            file_date = datetime.strptime(date_part, "%Y%m%d").replace(tzinfo=UTC)
            if file_date < cutoff:
                f.unlink()
                deleted += 1
        except Exception as _exc:
            log.debug("silenced exception in backup_brain_db.py: %s", _exc)
            continue
    return deleted


def run(keep_days: int = RETENTION_DAYS) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dest_dir": str(BACKUP_DIR),
        "keep_days": keep_days,
        "results": [],
        "rotated": 0,
        "all_ok": True,
    }
    for label, src in DATABASES:
        res = _backup_one(label, src, BACKUP_DIR)
        summary["results"].append(res)
        if res["status"] == "error":
            summary["all_ok"] = False
            continue
        if res["status"] == "ok":
            res["minio"] = _upload_to_minio(label, Path(res["dest"]))
    summary["rotated"] = _rotate(BACKUP_DIR, keep_days)
    summary["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return summary


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["all_ok"] else 1)
