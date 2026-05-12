#!/Users/chrischo/server/brain/.venv/bin/python
"""cli/backup_docker_volumes.py — daily snapshot of critical Docker bind-mount
data (vaultwarden, ghost, couchdb, uptime-kuma).

Gap: brain/qdrant/neo4j are backed up nightly to MinIO but the Docker
services living under ~/server/*/data were off-site-less. A disk failure
would wipe the vaultwarden password DB. This closes that gap.

Strategy: tar the bind-mount source (host-side) — all targets use
./data or ./content bind mounts, no named Docker volumes to juggle.
SQLite-backed services (vaultwarden, uptime-kuma) are in WAL mode; the
tar captures main + -wal + -shm together so a restore replays cleanly.
Ghost content is static files. CouchDB uses its own on-disk format — the
daily tar is "good enough" for DR (losing <24h of ingest in exchange for
a single-process backup story).

Outputs:
  local  : /Users/chrischo/server/brain/logs/backups/docker-volumes/
           └ <service>-YYYYMMDD.tar.gz + .sha256 sidecar
  minio  : s3://rag-backups/docker-volumes/

Retention: 7 days local plus a size cap so anomalous volume growth cannot
breach the logs_dir_total_mb SLO. The newest local backup per service is
always preserved; MinIO holds the longer retention copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path

log = logging.getLogger("brain.backup_docker_volumes")

BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
BACKUP_DIR = BRAIN_LOGS_DIR / "backups" / "docker-volumes"
# 2026-05-11: docker-volumes lives under brain/logs/ so it counts toward the
# logs_dir SLO. 7→4 days local + 512→320 MB cap reclaims ~200 MB without
# losing durability — MinIO retains the long-DR window via the upload below.
RETENTION_DAYS = 4
LOCAL_SIZE_CAP_MB = 320
MINIO_BUCKET = "rag-backups"
MINIO_PREFIX = "docker-volumes/"

# (label, source_path, glob_excludes)
TARGETS: list[tuple[str, Path, list[str]]] = [
    ("vaultwarden", Path("/Users/chrischo/server/vaultwarden/data"), ["*.tmp", "tmp/*"]),
    ("ghost", Path("/Users/chrischo/server/ghost/content"), ["logs/*", "*.log"]),
    ("couchdb", Path("/Users/chrischo/server/couchdb/data"), []),
    (
        "uptime-kuma",
        Path("/Users/chrischo/server/uptime-kuma/data"),
        [
            "*.tmp",
            "*.bak-*",
            "*.corrupt-*",
            "*.recover-*",
            "*.recovered-*",
            "*.replaced-corrupt-*",
            "*.rebuilt-*",
            "*.dump-*.sql",
            "*.recover-*.sql",
        ],
    ),
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _upload_to_minio(local_path: Path) -> dict:
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
        checksum_key = key.rsplit(".tar.gz", 1)[0] + ".sha256"
        checksum_body = f"{digest}  {local_path.name}\n"
        s3.put_object(Bucket=MINIO_BUCKET, Key=checksum_key, Body=checksum_body.encode())
        return {"status": "ok", "key": key, "sha256": digest}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)[:200]}


def _backup_one(label: str, src: Path, excludes: list[str], dest_dir: Path) -> dict:
    result = {"label": label, "src": str(src), "status": "skipped", "bytes": 0, "duration_s": 0.0}
    if not src.exists():
        result["reason"] = "source_missing"
        return result
    today = datetime.now(UTC).strftime("%Y%m%d")
    dest = dest_dir / f"{label}-{today}.tar.gz"
    t0 = time.time()
    try:

        def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
            rel = tarinfo.name
            for pat in excludes:
                if Path(rel).match(pat) or fnmatch(Path(rel).name, pat):
                    return None
            return tarinfo

        with tarfile.open(dest, "w:gz", compresslevel=6) as tf:
            tf.add(str(src), arcname=src.name, filter=_filter)
        result["status"] = "ok"
        result["dest"] = str(dest)
        result["bytes"] = dest.stat().st_size
        result["duration_s"] = round(time.time() - t0, 3)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:200]
    return result


def _label_for_backup(path: Path) -> str:
    stem = path.name
    for suffix in (".tar.gz", ".sha256"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    # <label>-YYYYMMDD; labels may contain dashes.
    head, sep, tail = stem.rpartition("-")
    if sep and len(tail) == 8 and tail.isdigit():
        return head
    return stem


def _protected_latest_by_label(files: list[Path]) -> set[Path]:
    latest: dict[str, Path] = {}
    for path in files:
        label = _label_for_backup(path)
        current = latest.get(label)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            latest[label] = path
    return set(latest.values())


def _rotate(dest_dir: Path, keep_days: int, *, max_total_mb: int = LOCAL_SIZE_CAP_MB) -> int:
    if not dest_dir.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    deleted = 0
    for f in list(dest_dir.iterdir()):
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    if max_total_mb <= 0:
        return deleted

    files = [f for f in dest_dir.iterdir() if f.is_file()]
    protected = _protected_latest_by_label(files)
    total = sum(f.stat().st_size for f in files)
    cap_bytes = max_total_mb * 1024 * 1024
    if total <= cap_bytes:
        return deleted

    for f in sorted(files, key=lambda p: p.stat().st_mtime):
        if total <= cap_bytes:
            break
        if f in protected:
            continue
        try:
            size = f.stat().st_size
            f.unlink()
            total -= size
            deleted += 1
        except OSError:
            pass
    return deleted


def main() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "targets": [],
        "rotated": 0,
    }
    any_error = False
    for label, src, excludes in TARGETS:
        res = _backup_one(label, src, excludes, BACKUP_DIR)
        if res["status"] == "ok":
            res["minio"] = _upload_to_minio(Path(res["dest"]))
        else:
            any_error = True
        summary["targets"].append(res)
    summary["rotated"] = _rotate(BACKUP_DIR, RETENTION_DAYS)
    print(json.dumps(summary, indent=2))
    return 0 if not any_error else 1


if __name__ == "__main__":
    sys.exit(main())
