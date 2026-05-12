#!/usr/bin/env python3
"""cli/backup_brain_db.py — daily SQLite .backup of brain.db + autonomy.db.

W3 fix (2026-04-17): brain.db is the atoms truth layer + autonomy state.
ChromaDB had a backup job (ai.openclaw.chroma-backup.plist) but these two
SQLite DBs did not — loss would destroy the durable memory layer.

Strategy:
  - Use SQLite's online .backup API (lock-free, consistent) via Python sqlite3
  - Write to ~/server/brain/logs/backups/<db>-YYYYMMDD.db
  - Rotate: keep last N days locally (default 7); MinIO keeps the longer DR
    window so local logs/ stays under the SLO budget.
  - Invoked daily by launchd (ai.openclaw.brain-backup.plist)

Exit codes:
  0 — all DBs backed up (or no-ops for missing sources)
  1 — at least one backup failed

Log output (JSON on stdout) for observability.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("brain.backup_brain_db")

BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
BACKUP_DIR = BRAIN_LOGS_DIR / "backups"
# Local retention covers the rapid-restore window only (oops-I-just-corrupted-it).
# MinIO holds the longer DR window via the upload path below, so the local copy
# can be aggressive without losing durability. 2026-05-11 fix: 7→4 days local
# reclaims ~210 MB inside logs/, keeping the directory under the logs_dir SLO
# budget as brain.db itself crossed 400 MB.
RETENTION_DAYS = 4
MINIO_BUCKET = "rag-backups"
MINIO_PREFIX = "brain-db-backup/"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _compress(path: Path) -> Path:
    """Gzip in place, return .gz path, remove source. Reduces nightly brain
    backups ~5x (e.g. 110MB → 22MB) so the daily run doesn't blow the
    logs_dir SLO budget across the 14-day retention window."""
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(str(gz_path), "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    path.unlink()
    return gz_path


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
        # Strip .db.gz or .db (longest match first) before appending .sha256
        # so the checksum key is e.g. brain-20260430.sha256 regardless of
        # whether the upload is gzipped.
        checksum_base = key
        for suffix in (".db.gz", ".db"):
            if checksum_base.endswith(suffix):
                checksum_base = checksum_base[: -len(suffix)]
                break
        checksum_key = checksum_base + ".sha256"
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
        # Compress in-place so MinIO upload + 14-day local retention stay small.
        raw_bytes = dest.stat().st_size
        gz_path = _compress(dest)
        result["status"] = "ok"
        result["dest"] = str(gz_path)
        result["bytes"] = gz_path.stat().st_size
        result["raw_bytes"] = raw_bytes
        result["duration_s"] = round(time.time() - t0, 3)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:200]
    return result


def _rotate(dest_dir: Path, keep_days: int) -> int:
    """Delete backup files older than keep_days. Returns count deleted.

    Matches both <stem>-YYYYMMDD.db and <stem>-YYYYMMDD.db.gz. The previous
    implementation globbed only *.db, so .gz files accumulated indefinitely.
    """
    if not dest_dir.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    deleted = 0
    for f in dest_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if name.endswith(".db.gz"):
            base = name[: -len(".db.gz")]
        elif name.endswith(".db"):
            base = name[: -len(".db")]
        else:
            continue
        try:
            date_part = base.rsplit("-", 1)[-1]
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
