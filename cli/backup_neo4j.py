#!/usr/bin/env python3
"""Nightly Neo4j backup to MinIO.

Dumps Neo4j database, compresses, uploads to rag-backups bucket.
14-day retention (same as ChromaDB backup).
"""

import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

BACKUP_BUCKET = "rag-backups"
NEO4J_DATA_DIR = Path("/opt/homebrew/var/neo4j/data")
MAX_BACKUPS = 14


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _minio import s3_client as _s3_client


def backup():
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    archive_name = f"neo4j-backup-{date_str}.tar.gz"

    if not NEO4J_DATA_DIR.exists():
        print(f"ERROR: Neo4j data dir not found: {NEO4J_DATA_DIR}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / archive_name
        dump_dir = Path(tmp) / "neo4j-dump"
        dump_dir.mkdir()

        print("[1/3] Dumping Neo4j database (consistent offline export)...")
        # Use neo4j-admin for consistent dump (handles transaction logs properly)
        dump_result = subprocess.run(
            ["/opt/homebrew/bin/neo4j-admin", "database", "dump", "neo4j", f"--to-path={dump_dir}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        neo4j_stopped = False
        if dump_result.returncode != 0:
            print(f"  neo4j-admin dump failed ({dump_result.stderr[:100]}), falling back to file copy")
            subprocess.run(["/opt/homebrew/bin/neo4j", "stop"], capture_output=True, timeout=30)
            import time

            time.sleep(2)
            neo4j_stopped = True

        try:
            print("  Creating archive...")
            with tarfile.open(archive_path, "w:gz") as tar:
                if dump_dir.exists() and any(dump_dir.iterdir()):
                    tar.add(str(dump_dir), arcname="neo4j-dump")
                else:
                    tar.add(str(NEO4J_DATA_DIR), arcname="neo4j-data")
        finally:
            if neo4j_stopped:
                subprocess.run(["/opt/homebrew/bin/neo4j", "start"], capture_output=True, timeout=30)
        size_mb = archive_path.stat().st_size / 1024 / 1024
        print(f"  Archive: {size_mb:.1f} MB")

        print("[2/3] Uploading to MinIO...")
        try:
            s3 = _s3_client()
            s3.upload_file(str(archive_path), BACKUP_BUCKET, archive_name)
            print(f"  Uploaded: {BACKUP_BUCKET}/{archive_name}")
        except Exception as e:
            print(f"ERROR: Upload failed: {e}")
            return 1

        print("[3/3] Pruning old backups...")
        try:
            resp = s3.list_objects_v2(Bucket=BACKUP_BUCKET, Prefix="neo4j-backup-")
            objects = sorted(resp.get("Contents", []), key=lambda o: o["Key"])
            if len(objects) > MAX_BACKUPS:
                to_delete = objects[: len(objects) - MAX_BACKUPS]
                for obj in to_delete:
                    s3.delete_object(Bucket=BACKUP_BUCKET, Key=obj["Key"])
                    print(f"  Pruned: {obj['Key']}")
        except Exception as e:
            print(f"  Prune warning: {e}")

    print("Backup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(backup())
