#!/Users/chrischo/server/brain/.venv/bin/python
"""Monthly backup verification smoke test.

Downloads the latest chroma tar + sha256 from MinIO, verifies the checksum,
extracts to a temp dir, runs PRAGMA integrity_check on each sqlite3 file,
counts rows in the chroma collections table, and alerts Jenna on failure.

Runs on the 1st of each month at 4:30am via brain scheduler.
"""

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


BRAIN_ROOT = Path("/Users/chrischo/server/brain")
OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
MINIO_BUCKET = "rag-backups"
CHROMA_PREFIX = "chroma-backup-"
MIN_COLLECTIONS = 10  # expected minimum — we have ~15 collections in prod (2026-04-12)


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _minio import s3_client as _s3_client


def _latest_backup_key(s3) -> str | None:
    resp = s3.list_objects_v2(Bucket=MINIO_BUCKET, Prefix=CHROMA_PREFIX)
    tarballs = [
        obj["Key"] for obj in resp.get("Contents", [])
        if obj["Key"].endswith(".tar.gz")
    ]
    if not tarballs:
        return None
    tarballs.sort(reverse=True)
    return tarballs[0]


def _alert_failure(error_msg: str) -> None:
    try:
        subprocess.run(
            [
                OPENCLAW_BIN, "agent",
                "--agent", "jenna",
                "--message", f"BACKUP VERIFY FAILED: {error_msg}",
                "--thinking", "off", "--timeout", "30",
            ],
            timeout=35, capture_output=True,
        )
    except Exception as e:
        print(f"  WARNING: alert dispatch failed: {e}")


def verify_backup() -> int:
    print(f"Backup Verify — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    tmp_dir = Path(tempfile.mkdtemp(prefix="backup-verify-"))
    try:
        print("[1/5] Locating latest backup in MinIO...")
        try:
            s3 = _s3_client()
            archive_key = _latest_backup_key(s3)
        except Exception as e:
            err = f"MinIO list failed: {e}"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        if not archive_key:
            err = f"no {CHROMA_PREFIX}*.tar.gz backups found in bucket"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        print(f"  latest: {archive_key}")
        checksum_key = archive_key.replace(".tar.gz", ".sha256")

        print("[2/5] Downloading tar + checksum...")
        local_archive = tmp_dir / archive_key
        local_checksum = tmp_dir / checksum_key
        try:
            s3.download_file(MINIO_BUCKET, archive_key, str(local_archive))
        except Exception as e:
            err = f"download of {archive_key} failed: {e}"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        checksum_available = True
        try:
            s3.download_file(MINIO_BUCKET, checksum_key, str(local_checksum))
        except Exception:
            checksum_available = False
            print(f"  WARNING: no checksum file (legacy backup)")

        print("[3/5] Verifying checksum...")
        if checksum_available:
            expected = local_checksum.read_text().split()[0]
            actual = hashlib.sha256()
            with local_archive.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    actual.update(chunk)
            if expected != actual.hexdigest():
                err = f"checksum mismatch on {archive_key}: expected {expected[:16]}..., got {actual.hexdigest()[:16]}..."
                print(f"  ERROR: {err}")
                _alert_failure(err)
                return 1
            print(f"  OK — {expected[:16]}...")
        else:
            print("  skipped (no checksum file)")

        print("[4/5] Extracting archive...")
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()
        try:
            subprocess.run(
                ["tar", "xzf", str(local_archive), "-C", str(extract_dir)],
                check=True, timeout=180,
            )
        except subprocess.CalledProcessError as e:
            err = f"tar extraction failed: {e}"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        print("[5/5] Running sqlite integrity + row counts...")
        sqlite_files = list(extract_dir.rglob("*.sqlite3"))
        if not sqlite_files:
            err = "no .sqlite3 files found in extracted backup"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        collection_count = 0
        for db_file in sqlite_files:
            try:
                conn = sqlite3.connect(str(db_file))
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    status = result[0] if result else "unknown"
                    if status != "ok":
                        err = f"integrity_check FAILED on {db_file.name}: {status}"
                        print(f"  ERROR: {err}")
                        _alert_failure(err)
                        return 1
                    print(f"  {db_file.name}: integrity ok")

                    if db_file.name == "chroma.sqlite3":
                        try:
                            row = conn.execute("SELECT COUNT(*) FROM collections").fetchone()
                            collection_count = row[0] if row else 0
                            print(f"  {db_file.name}: {collection_count} collections")
                        except Exception as e:
                            print(f"  WARNING: collection count query failed: {e}")
                finally:
                    conn.close()
            except Exception as e:
                err = f"sqlite open failed on {db_file.name}: {e}"
                print(f"  ERROR: {err}")
                _alert_failure(err)
                return 1

        if collection_count < MIN_COLLECTIONS:
            err = f"only {collection_count} collections in backup (expected >= {MIN_COLLECTIONS})"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        print(f"\nBackup verify PASSED — {archive_key} ({collection_count} collections)")
        return 0
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(verify_backup())
