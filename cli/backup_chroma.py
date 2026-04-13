#!/Users/chrischo/server/brain/.venv/bin/python
"""Nightly backup to MinIO — ChromaDB + raw/inbox + canonical + distilled.

Critical: raw/inbox is the source of truth for everything ingested between
weekly canonical promotions. If it dies, brain capture from the last week is
gone. Phase 0c made these backups load-bearing.

MinIO is accessed via boto3 S3 API (endpoint http://192.168.97.5:9000).
Credentials are read from ~/server/minio/.env.

Usage:
  backup_chroma.py [--retain-days 14]
"""

import argparse
import hashlib
import os
import subprocess
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


CHROMA_DATA = Path("/Users/chrischo/server/rag/chroma-data")
KNOWLEDGE_ROOT = Path("/Users/chrischo/server/knowledge")
KNOWLEDGE_SUBDIRS = ("raw/inbox", "canonical", "distilled")
BACKUP_DIR = Path("/Users/chrischo/server/rag/chroma-backups")
LOG_DIR = Path("/Users/chrischo/server/brain/logs")
MINIO_ALIAS = "local"
MINIO_BUCKET = "rag-backups"


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _minio import s3_client as _s3_client


def mc_upload(host_path, remote_name):
    try:
        s3 = _s3_client()
        s3.upload_file(str(host_path), MINIO_BUCKET, remote_name)
        return True
    except Exception as e:
        print(f"  ERROR: S3 upload failed: {e}")
        return False


def mc_list_prefix(prefix):
    try:
        s3 = _s3_client()
        resp = s3.list_objects_v2(Bucket=MINIO_BUCKET, Prefix=prefix)
        return [obj["Key"] for obj in resp.get("Contents", [])]
    except Exception:
        return []


def mc_remove(remote_name):
    try:
        s3 = _s3_client()
        s3.delete_object(Bucket=MINIO_BUCKET, Key=remote_name)
    except Exception:
        pass


def prune_remote(prefix: str, cutoff: datetime) -> int:
    """Remove remote backups older than cutoff for a given prefix. Returns count deleted."""
    deleted = 0
    for fname in mc_list_prefix(prefix):
        try:
            date_str = fname.replace(prefix, "").replace(".tar.gz", "")
            fdate = datetime.strptime(date_str, "%Y-%m-%d")
            if fdate < cutoff:
                mc_remove(fname)
                deleted += 1
                print(f"  Deleted MinIO: {fname}")
        except ValueError:
            continue
    return deleted


def prune_local(prefix: str, cutoff: datetime) -> int:
    deleted = 0
    for f in BACKUP_DIR.glob(f"{prefix}*.tar.gz"):
        try:
            date_str = f.stem.replace(prefix, "")
            fdate = datetime.strptime(date_str, "%Y-%m-%d")
            if fdate < cutoff:
                f.unlink()
                deleted += 1
                print(f"  Deleted local: {f.name}")
        except ValueError:
            continue
    return deleted


def backup_knowledge(retain_days: int) -> bool:
    """Snapshot raw/inbox + canonical + distilled into one tarball, push to MinIO.

    Critical because raw/inbox holds everything captured since the last weekly
    canonical promotion. Without this, a `rm -rf` event loses the entire
    in-flight ingestion stream.
    """
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    name = f"knowledge-backup-{date_str}"
    archive_path = BACKUP_DIR / f"{name}.tar.gz"

    print("\nKnowledge Backup")
    print("=" * 50)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print("[1/3] Tarring raw/inbox + canonical + distilled...")

    existing = [d for d in KNOWLEDGE_SUBDIRS if (KNOWLEDGE_ROOT / d).exists()]
    if not existing:
        print("  WARNING: no knowledge subdirs exist; skipping")
        return False

    try:
        subprocess.run(
            ["tar", "czf", str(archive_path), "-C", str(KNOWLEDGE_ROOT), *existing],
            check=True, timeout=180,
        )
    except subprocess.CalledProcessError as e:
        print(f"  ERROR: tar failed: {e}")
        return False

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"  Archive: {archive_path} ({size_mb:.1f} MB)")

    digest = _sha256_file(archive_path)
    checksum_path = BACKUP_DIR / f"{name}.sha256"
    checksum_path.write_text(f"{digest}  {archive_path.name}\n")
    print(f"  checksum: {digest[:16]}...")

    print("[2/3] Uploading to MinIO...")
    uploaded = mc_upload(archive_path, archive_path.name)
    if uploaded:
        print(f"  Uploaded to {MINIO_ALIAS}/{MINIO_BUCKET}/{archive_path.name}")
        if mc_upload(checksum_path, checksum_path.name):
            print(f"  Uploaded checksum to {MINIO_ALIAS}/{MINIO_BUCKET}/{checksum_path.name}")
    else:
        print(f"  WARNING: upload failed; local archive preserved at {archive_path}")

    print(f"[3/3] Pruning knowledge backups older than {retain_days} days...")
    cutoff = now - timedelta(days=retain_days)
    prune_local("knowledge-backup-", cutoff)
    if uploaded:
        prune_remote("knowledge-backup-", cutoff)

    return uploaded


def backup(retain_days):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    backup_name = f"chroma-backup-{date_str}"
    backup_path = BACKUP_DIR / backup_name
    archive_path = BACKUP_DIR / f"{backup_name}.tar.gz"

    print(f"ChromaDB Backup — {date_str}")
    print("=" * 50)

    print("[1/4] Copying ChromaDB data...")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        shutil.rmtree(backup_path)

    # ChromaDB runs natively now — data is directly on disk at ~/server/rag/chroma-data/
    if not CHROMA_DATA.exists():
        print(f"  ERROR: ChromaDB data dir not found: {CHROMA_DATA}")
        return False

    # Copy the non-sqlite bits first (HNSW index files, etc.) via copytree,
    # skipping the sqlite3 files + their WAL/SHM sidecars — those need the
    # online backup API for point-in-time consistency while writes continue.
    import sqlite3

    def _skip_sqlite(src, names):
        skipped = []
        for n in names:
            if n.endswith(".sqlite3") or n.endswith(".sqlite3-wal") or n.endswith(".sqlite3-shm"):
                skipped.append(n)
        return skipped

    shutil.copytree(CHROMA_DATA, backup_path, ignore=_skip_sqlite)

    # For each sqlite3 file, use the online backup API (conn.backup) to get a
    # consistent snapshot while the live DB is still being written to.
    # This is transactional — no race between checkpoint and copy.
    for db_file in CHROMA_DATA.rglob("*.sqlite3"):
        rel = db_file.relative_to(CHROMA_DATA)
        dst_file = backup_path / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            src_conn = sqlite3.connect(str(db_file))
            dst_conn = sqlite3.connect(str(dst_file))
            try:
                src_conn.backup(dst_conn)
            finally:
                src_conn.close()
                dst_conn.close()
        except Exception as e:
            print(f"  WARNING: online backup failed for {rel}: {e}, falling back to copy+checkpoint")
            try:
                conn = sqlite3.connect(str(db_file))
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    conn.close()
            except Exception:
                pass
            shutil.copy2(db_file, dst_file)

    print("[2/4] Compressing...")
    subprocess.run(
        ["tar", "czf", str(archive_path), "-C", str(BACKUP_DIR), backup_name],
        check=True, timeout=120
    )
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"  Archive: {archive_path} ({size_mb:.1f} MB)")

    digest = _sha256_file(archive_path)
    checksum_path = BACKUP_DIR / f"{backup_name}.sha256"
    checksum_path.write_text(f"{digest}  {archive_path.name}\n")
    print(f"  checksum: {digest[:16]}...")

    print("[3/4] Uploading to MinIO...")
    uploaded = mc_upload(archive_path, archive_path.name)
    if uploaded:
        print(f"  Uploaded to {MINIO_ALIAS}/{MINIO_BUCKET}/{archive_path.name}")
        if mc_upload(checksum_path, checksum_path.name):
            print(f"  Uploaded checksum to {MINIO_ALIAS}/{MINIO_BUCKET}/{checksum_path.name}")
    else:
        print(f"  WARNING: MinIO upload failed; local backup preserved at {archive_path}")

    print(f"[4/4] Pruning chroma backups older than {retain_days} days...")
    cutoff = now - timedelta(days=retain_days)
    prune_local("chroma-backup-", cutoff)
    if uploaded:
        prune_remote("chroma-backup-", cutoff)

    if backup_path.exists():
        shutil.rmtree(backup_path)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"backup-{date_str}.log"
    log_file.write_text(f"Backup completed: {now.isoformat()}\nSize: {size_mb:.1f} MB\nRetain: {retain_days} days\n")
    print(f"\nBackup complete. Log: {log_file}")
    return True


def backup_semantic_memory(retain_days):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    name = f"semantic-memory-{date_str}.json"
    out_path = BACKUP_DIR / name

    print("\nSemantic Memory Backup")
    print("=" * 50)

    try:
        # Query ChromaDB directly to bypass server's 200 limit
        import json as _json
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from http_pool import http_json
        from search import get_collections

        cols = get_collections()
        sem_id = cols.get("semantic_memory")
        if not sem_id:
            print("  ERROR: semantic_memory collection not found in ChromaDB")
            return False

        resp = http_json(
            "POST",
            f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_id}/get",
            {"limit": 10000, "include": ["documents", "metadatas"]},
        )
        entries = []
        ids = resp.get("ids", [])
        docs = resp.get("documents", [])
        metas = resp.get("metadatas", [])
        for i, eid in enumerate(ids):
            entries.append({
                "id": eid,
                "content": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
            })

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        data = _json.dumps({"memories": entries, "count": len(entries), "exported": now.isoformat()}, ensure_ascii=False, indent=2)
        out_path.write_text(data)
        size_kb = out_path.stat().st_size / 1024
        print(f"[1/2] Exported {len(entries)} entries ({size_kb:.0f} KB) to {out_path}")
    except Exception as e:
        print(f"  ERROR: semantic memory export failed: {e}")
        return False

    uploaded = mc_upload(out_path, name)
    if uploaded:
        print(f"[2/2] Uploaded to {MINIO_ALIAS}/{MINIO_BUCKET}/{name}")
    else:
        print(f"  WARNING: upload failed; local copy preserved at {out_path}")

    # Prune old
    cutoff = now - timedelta(days=retain_days)
    for f in BACKUP_DIR.glob("semantic-memory-*.json"):
        try:
            fdate = datetime.strptime(f.stem.replace("semantic-memory-", ""), "%Y-%m-%d")
            if fdate < cutoff:
                f.unlink()
        except ValueError:
            continue

    return uploaded


def main():
    parser = argparse.ArgumentParser(description="Backup ChromaDB + knowledge to MinIO")
    parser.add_argument("--retain-days", type=int, default=14, help="Days to retain backups")
    parser.add_argument("--chroma-only", action="store_true", help="Skip knowledge backup")
    parser.add_argument("--knowledge-only", action="store_true", help="Skip ChromaDB backup")
    parser.add_argument("--semantic-only", action="store_true", help="Only backup semantic memory")
    args = parser.parse_args()

    failed = False

    if args.semantic_only:
        if not backup_semantic_memory(args.retain_days):
            failed = True
    else:
        if not args.knowledge_only:
            if not backup(args.retain_days):
                failed = True
        if not args.chroma_only:
            if not backup_knowledge(args.retain_days):
                failed = True
        if not args.chroma_only and not args.knowledge_only:
            backup_semantic_memory(args.retain_days)

    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
