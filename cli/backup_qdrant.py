#!/Users/chrischo/server/brain/.venv/bin/python
"""Nightly backup to MinIO — Qdrant snapshots + knowledge tree.

Critical: raw/inbox is the source of truth for everything ingested between
weekly canonical promotions. If it dies, brain capture from the last week is
gone.

MinIO is accessed via boto3 S3 API. Credentials resolved by cli/_minio.py.

Qdrant backups use the native snapshot API per collection
(``POST /collections/{name}/snapshots`` → ``GET .../snapshots/{file}``).
Each snapshot is a point-in-time consistent dump including vectors,
payloads, and HNSW indexes.

Usage:
  backup_qdrant.py [--retain-days 14]
  backup_qdrant.py --qdrant-only
  backup_qdrant.py --knowledge-only
  backup_qdrant.py --semantic-only
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


QDRANT_URL = "http://127.0.0.1:6333"
KNOWLEDGE_ROOT = Path("/Users/chrischo/server/knowledge")
KNOWLEDGE_SUBDIRS = ("raw/inbox", "canonical", "distilled")
BACKUP_DIR = Path("/Users/chrischo/server/brain/qdrant-backups")
LOG_DIR = Path("/Users/chrischo/server/brain/logs")
MINIO_ALIAS = "local"
MINIO_BUCKET = "rag-backups"

COLLECTIONS = ["canonical", "semantic_memory", "experience", "knowledge", "code", "personal", "obsidian"]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _minio import s3_client as _s3_client  # noqa: E402


def mc_upload(host_path: Path, remote_name: str) -> bool:
    try:
        s3 = _s3_client()
        s3.upload_file(str(host_path), MINIO_BUCKET, remote_name)
        return True
    except Exception as e:
        print(f"  ERROR: S3 upload failed: {e}")
        return False


def mc_list_prefix(prefix: str) -> list[str]:
    try:
        s3 = _s3_client()
        resp = s3.list_objects_v2(Bucket=MINIO_BUCKET, Prefix=prefix)
        return [obj["Key"] for obj in resp.get("Contents", [])]
    except Exception:
        return []


def mc_remove(remote_name: str) -> None:
    try:
        s3 = _s3_client()
        s3.delete_object(Bucket=MINIO_BUCKET, Key=remote_name)
    except Exception as e:
        print(f"  WARN: remove {remote_name} failed: {e}")


def prune_remote(prefix: str, cutoff: datetime) -> int:
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


def _snapshot_collection(name: str, dest: Path) -> bool:
    """Create a Qdrant snapshot and download it to `dest`.

    Qdrant's snapshot workflow:
      1. POST /collections/{name}/snapshots  → returns {name: <file>}
      2. GET  /collections/{name}/snapshots/{file}  → streams the .snapshot
      3. DELETE the snapshot server-side (keep disk lean)
    """
    import urllib.request

    try:
        req = urllib.request.Request(  # noqa: S310 — QDRANT_URL is a constant localhost URL
            f"{QDRANT_URL}/collections/{name}/snapshots",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            body = json.loads(resp.read())
        snap_name = body.get("result", {}).get("name")
        if not snap_name:
            print(f"  {name}: snapshot creation returned no name")
            return False
    except Exception as e:
        print(f"  {name}: snapshot POST failed: {e}")
        return False

    try:
        with urllib.request.urlopen(  # noqa: S310
            f"{QDRANT_URL}/collections/{name}/snapshots/{snap_name}", timeout=600
        ) as resp:
            out = dest / f"{name}.snapshot"
            with out.open("wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
    except Exception as e:
        print(f"  {name}: snapshot download failed: {e}")
        return False

    try:
        req = urllib.request.Request(  # noqa: S310
            f"{QDRANT_URL}/collections/{name}/snapshots/{snap_name}",
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=30).read()  # noqa: S310
    except Exception as e:
        print(f"  WARN: snapshot cleanup failed for {name}: {e}")

    return True


def backup(retain_days: int) -> bool:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    backup_name = f"qdrant-backup-{date_str}"
    backup_path = BACKUP_DIR / backup_name
    archive_path = BACKUP_DIR / f"{backup_name}.tar.gz"

    print(f"Qdrant Backup — {date_str}")
    print("=" * 50)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        shutil.rmtree(backup_path)
    backup_path.mkdir(parents=True)

    print(f"[1/4] Snapshotting {len(COLLECTIONS)} collections...")
    succeeded = 0
    for name in COLLECTIONS:
        if _snapshot_collection(name, backup_path):
            succeeded += 1
            size = (backup_path / f"{name}.snapshot").stat().st_size / (1024 * 1024)
            print(f"  {name}: {size:.1f} MB")
    if succeeded == 0:
        print("  ERROR: no snapshots produced")
        shutil.rmtree(backup_path)
        return False

    print("[2/4] Compressing...")
    subprocess.run(
        ["tar", "czf", str(archive_path), "-C", str(BACKUP_DIR), backup_name],
        check=True,
        timeout=180,
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

    print(f"[4/4] Pruning qdrant backups older than {retain_days} days...")
    cutoff = now - timedelta(days=retain_days)
    prune_local("qdrant-backup-", cutoff)
    if uploaded:
        prune_remote("qdrant-backup-", cutoff)

    if backup_path.exists():
        shutil.rmtree(backup_path)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"backup-{date_str}.log"
    log_file.write_text(
        f"Qdrant backup completed: {now.isoformat()}\n"
        f"Size: {size_mb:.1f} MB\nCollections: {succeeded}/{len(COLLECTIONS)}\n"
    )
    print(f"\nBackup complete. Log: {log_file}")
    return True


def backup_knowledge(retain_days: int) -> bool:
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
            check=True,
            timeout=180,
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


def backup_semantic_memory(retain_days: int) -> bool:
    """Extra safety net — raw JSON dump of semantic_memory collection."""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    name = f"semantic-memory-{date_str}.json"
    out_path = BACKUP_DIR / name

    print("\nSemantic Memory Backup")
    print("=" * 50)

    try:
        # Use qdrant_client directly — avoids pulling brain_core's heavy
        # import cascade (sentence_transformers, cross-encoder) just to run
        # a scroll. Keeps the backup process lightweight.
        from qdrant_client import QdrantClient

        client = QdrantClient(url=QDRANT_URL, timeout=60)
        entries = []
        next_offset = None
        PAGE = 500
        while True:
            pts, next_offset = client.scroll(
                collection_name="semantic_memory",
                limit=PAGE,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            if not pts:
                break
            for p in pts:
                payload = dict(p.payload or {})
                doc = payload.pop("_document", "")
                eid = payload.pop("_original_id", str(p.id))
                entries.append(
                    {
                        "id": eid,
                        "content": doc,
                        "metadata": payload,
                    }
                )
            if not next_offset:
                break

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            {"memories": entries, "count": len(entries), "exported": now.isoformat()},
            ensure_ascii=False,
            indent=2,
        )
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

    cutoff = now - timedelta(days=retain_days)
    for f in BACKUP_DIR.glob("semantic-memory-*.json"):
        try:
            fdate = datetime.strptime(f.stem.replace("semantic-memory-", ""), "%Y-%m-%d")
            if fdate < cutoff:
                f.unlink()
        except ValueError:
            continue

    return uploaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup Qdrant + knowledge to MinIO")
    parser.add_argument("--retain-days", type=int, default=14, help="Days to retain backups")
    parser.add_argument("--qdrant-only", action="store_true", help="Skip knowledge backup")
    parser.add_argument("--knowledge-only", action="store_true", help="Skip Qdrant backup")
    parser.add_argument("--semantic-only", action="store_true", help="Only backup semantic memory")
    args = parser.parse_args()

    failed = False

    if args.semantic_only:
        if not backup_semantic_memory(args.retain_days):
            failed = True
    else:
        if not args.knowledge_only and not backup(args.retain_days):
            failed = True
        if not args.qdrant_only and not backup_knowledge(args.retain_days):
            failed = True
        if not args.qdrant_only and not args.knowledge_only:
            backup_semantic_memory(args.retain_days)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
