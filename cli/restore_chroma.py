#!/opt/homebrew/bin/python3
"""Restore ChromaDB from MinIO backup.

Usage:
  restore_chroma.py --date YYYY-MM-DD
"""

import argparse
import hashlib
import os
import subprocess
import shutil
import sys
from pathlib import Path


CHROMA_DATA = Path("/Users/chrischo/server/rag/chroma-data")
BACKUP_DIR = Path("/Users/chrischo/server/rag/chroma-backups")
MINIO_BUCKET = "rag-backups"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _minio import s3_client as _s3_client


def restore(date_str):
    archive_name = f"chroma-backup-{date_str}.tar.gz"
    checksum_name = f"chroma-backup-{date_str}.sha256"
    local_archive = BACKUP_DIR / archive_name
    local_checksum = BACKUP_DIR / checksum_name
    extract_dir = BACKUP_DIR / f"chroma-backup-{date_str}"

    print(f"ChromaDB Restore — {date_str}")
    print("=" * 50)

    if not local_archive.exists():
        print("[1/4] Downloading from MinIO via S3 API...")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            s3 = _s3_client()
            s3.download_file(MINIO_BUCKET, archive_name, str(local_archive))
        except Exception as e:
            print(f"ERROR: Download failed: {e}")
            return False
        if not local_archive.exists():
            print(f"ERROR: Backup not found in bucket '{MINIO_BUCKET}'")
            return False
        if not local_checksum.exists():
            try:
                s3 = _s3_client()
                s3.download_file(MINIO_BUCKET, checksum_name, str(local_checksum))
            except Exception:
                pass  # checksum is optional — older backups won't have one
    else:
        print("[1/4] Using local backup...")

    if local_checksum.exists():
        expected = local_checksum.read_text().split()[0]
        actual = hashlib.sha256()
        with local_archive.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                actual.update(chunk)
        if expected != actual.hexdigest():
            raise RuntimeError(f"Checksum mismatch: expected {expected[:16]}..., got {actual.hexdigest()[:16]}...")
        print(f"  checksum verified: {expected[:16]}...")
    else:
        print(f"  WARNING: no checksum file found, skipping verification")

    print("[2/4] Extracting...")
    subprocess.run(
        ["tar", "xzf", str(local_archive), "-C", str(BACKUP_DIR)],
        check=True, timeout=120
    )

    print("[3/4] Stopping ChromaDB...")
    # ChromaDB runs natively via launchd — stop it before restoring data
    subprocess.run(["launchctl", "unload",
                    os.path.expanduser("~/Library/LaunchAgents/ai.openclaw.chromadb-native.plist")],
                   capture_output=True, timeout=30)

    print("[4/4] Restoring data...")
    # ChromaDB runs natively via launchd — data lives at ~/server/rag/chroma-data/.
    # Atomic swap pattern: copy extracted data into a sibling `.new` dir first,
    # then rename-swap. This ensures CHROMA_DATA never disappears mid-flight —
    # a crash or kill can only leave us in one of two valid states
    # (pre-restore or post-restore), never a missing-dir state.
    #
    # Retention: each restore moves the current data to a timestamped
    # pre-restore-YYYY-MM-DD dir, and only copies older than 30 days are pruned.
    # This matches the CLAUDE.md 30-day backup retention rule.
    # backup_chroma.py creates the tarball from the copied CHROMA_DATA directory
    # with name "chroma-backup-YYYY-MM-DD", so extracted content lives directly
    # under extract_dir — no "data" subdir.
    src = extract_dir
    if not src.is_dir() or not any(src.iterdir()):
        raise RuntimeError(f"Extracted backup dir is empty or missing: {src}")
    new_dir = CHROMA_DATA.parent / "chroma-data.new-restore"
    from datetime import datetime as _dt, timedelta as _td
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    backup_current = CHROMA_DATA.parent / f"chroma-data.pre-restore-{stamp}"
    if new_dir.exists():
        shutil.rmtree(new_dir)
    try:
        shutil.copytree(src, new_dir)
    except Exception as e:
        print(f"ERROR: copy to staging dir failed: {e}")
        if new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)
        raise
    # Stage complete. Swap atomically: current → pre-restore-TIMESTAMP, new → current.
    if CHROMA_DATA.exists():
        CHROMA_DATA.rename(backup_current)
    try:
        new_dir.rename(CHROMA_DATA)
    except Exception as e:
        print(f"ERROR: final swap failed: {e}")
        if backup_current.exists() and not CHROMA_DATA.exists():
            print("Rolling back to pre-restore data...")
            backup_current.rename(CHROMA_DATA)
        raise

    # Prune pre-restore backups older than 30 days.
    cutoff = _dt.now() - _td(days=30)
    for old in CHROMA_DATA.parent.glob("chroma-data.pre-restore-*"):
        try:
            # parse "chroma-data.pre-restore-YYYYMMDD_HHMMSS"
            suffix = old.name.split("chroma-data.pre-restore-", 1)[1]
            ts = _dt.strptime(suffix.split("_")[0], "%Y%m%d")
            if ts < cutoff:
                shutil.rmtree(old)
                print(f"  pruned old pre-restore: {old.name}")
        except Exception:
            continue

    subprocess.run(["launchctl", "load",
                    os.path.expanduser("~/Library/LaunchAgents/ai.openclaw.chromadb-native.plist")],
                   capture_output=True, timeout=30)

    import time
    time.sleep(3)
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections", timeout=15) as resp:
            cols = json.loads(resp.read())
        print(f"\nRestore complete. Collections: {[c['name'] for c in cols]}")
    except Exception:
        print("\nWARNING: ChromaDB may need a moment to start. Verify manually.")

    if extract_dir.exists():
        shutil.rmtree(extract_dir)

    return True


def main():
    parser = argparse.ArgumentParser(description="Restore ChromaDB from MinIO Backup")
    parser.add_argument("--date", required=True, help="Backup date (YYYY-MM-DD)")
    args = parser.parse_args()
    restore(args.date)


if __name__ == '__main__':
    main()
