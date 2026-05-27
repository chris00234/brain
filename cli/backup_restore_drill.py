#!/usr/bin/env python3
"""Backup restore-readiness drill for Brain backups.

Restores/verifies backups in isolated temp paths only. Production DBs, Qdrant,
and Neo4j are never replaced or written.

Coverage:
  - SQLite: restore latest brain/autonomy .db(.gz), PRAGMA integrity_check.
  - Qdrant: verify latest local qdrant tar/checksum, validate all snapshots,
    and restore the smallest snapshot into a temporary Qdrant instance.
  - Neo4j: download latest MinIO archive/checksum, verify digest, and validate
    archive contents are structurally restorable.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
BACKUP_DIR = BRAIN_ROOT / "logs" / "backups"
QDRANT_BACKUP_DIR = BRAIN_ROOT / "qdrant-backups"
REPORT_FILE = BRAIN_ROOT / "logs" / "backup_restore_drill.json"
EXPECTED_STEMS = ("brain", "autonomy")
MIN_SNAPSHOT_BYTES = 4 * 1024
QDRANT_BIN = os.getenv("BRAIN_QDRANT_BIN", "/Users/chrischo/.local/bin/qdrant")
QDRANT_FULL_RESTORE_LIMIT = max(1, int(os.getenv("BRAIN_QDRANT_RESTORE_DRILL_LIMIT", "1")))
QDRANT_START_TIMEOUT_S = max(20.0, float(os.getenv("BRAIN_QDRANT_RESTORE_START_TIMEOUT_S", "120")))
MINIO_BUCKET = "rag-backups"
NEO4J_PREFIX = "neo4j-backup-"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_backup(stem: str, backup_dir: Path = BACKUP_DIR) -> Path | None:
    candidates = list(backup_dir.glob(f"{stem}-*.db.gz")) + list(backup_dir.glob(f"{stem}-*.db"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _restore_to_temp(backup_path: Path, tmp_dir: Path) -> Path:
    restored = tmp_dir / backup_path.name.removesuffix(".gz")
    if backup_path.suffix == ".gz":
        with gzip.open(backup_path, "rb") as src, restored.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    else:
        shutil.copy2(backup_path, restored)
    return restored


def verify_sqlite_backup(backup_path: Path, tmp_dir: Path) -> dict:
    started = time.time()
    result = {
        "backup": str(backup_path),
        "status": "error",
        "bytes": backup_path.stat().st_size if backup_path.exists() else 0,
        "duration_s": 0.0,
    }
    restored = _restore_to_temp(backup_path, tmp_dir)
    try:
        with sqlite3.connect(f"file:{restored}?mode=ro", uri=True) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            table_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','index','view','trigger')"
            ).fetchone()
        integrity_value = str(integrity[0] if integrity else "missing")
        result["integrity_check"] = integrity_value
        result["schema_objects"] = int(table_count[0] if table_count else 0)
        result["restored_bytes"] = restored.stat().st_size
        result["status"] = "ok" if integrity_value == "ok" else "error"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    finally:
        result["duration_s"] = round(time.time() - started, 3)
    return result


def _latest_archive(prefix: str, backup_dir: Path) -> Path | None:
    candidates = list(backup_dir.glob(f"{prefix}*.tar.gz"))
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _verify_sidecar_checksum(archive: Path) -> dict:
    checksum_path = archive.with_suffix("").with_suffix(".sha256")
    if not checksum_path.exists():
        return {"checksum_status": "missing"}
    expected = checksum_path.read_text().split()[0]
    actual = _sha256_file(archive)
    return {
        "checksum_status": "ok" if expected == actual else "mismatch",
        "sha256": actual,
        "checksum_file": str(checksum_path),
    }


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_qdrant(port: int, timeout_s: float = QDRANT_START_TIMEOUT_S) -> bool:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/collections"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:  # noqa: S310
                return resp.status == 200
        except Exception:
            time.sleep(0.5)
    return False


def _qdrant_collection_count(port: int, collection: str) -> int | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/collections/{collection}", timeout=5) as resp:
            body = json.loads(resp.read())
        result = body.get("result") or {}
        return int(result.get("points_count") or result.get("vectors_count") or 0)
    except Exception:
        return None


def _restore_qdrant_snapshots(snapshots: list[Path], tmp_dir: Path) -> dict:
    if not snapshots:
        return {"status": "error", "error": "no_snapshots"}
    qdrant_bin = Path(QDRANT_BIN)
    if not qdrant_bin.exists():
        return {"status": "skipped", "reason": f"qdrant_bin_missing:{qdrant_bin}"}

    production_snapshots = [p for p in snapshots if "healthcheck" not in p.stem.lower()] or snapshots
    selected = sorted(production_snapshots, key=lambda p: p.stat().st_size)[:QDRANT_FULL_RESTORE_LIMIT]
    port = _free_port()
    grpc_port = _free_port()
    qdrant_dir = tmp_dir / "qdrant-restore"
    qdrant_dir.mkdir()
    config_path = qdrant_dir / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "storage:",
                f"  storage_path: {qdrant_dir / 'storage'}",
                "service:",
                "  host: 127.0.0.1",
                f"  http_port: {port}",
                f"  grpc_port: {grpc_port}",
                "cluster:",
                "  enabled: false",
                "telemetry_disabled: true",
                "",
            ]
        )
    )
    cmd = [str(qdrant_bin), "--config-path", str(config_path), "--disable-telemetry", "--force-snapshot"]
    restored: list[dict] = []
    for snap in selected:
        target = f"restore_{snap.stem}".replace("-", "_")[:64]
        cmd.extend(["--snapshot", f"{snap}:{target}"])
        restored.append({"snapshot": snap.name, "target_collection": target, "bytes": snap.stat().st_size})

    out = qdrant_dir / "qdrant.out.log"
    err = qdrant_dir / "qdrant.err.log"
    # macOS jemalloc rejects `background_thread:true` because it is built without
    # pthread background-thread support; without this override the restore drill
    # aborts before any collection is loaded. Setting both keys covers qdrant
    # builds that read `_RJEM_MALLOC_CONF` (rust jemalloc) and `MALLOC_CONF`.
    drill_env = {
        **os.environ,
        "MALLOC_CONF": "background_thread:false",
        "_RJEM_MALLOC_CONF": "background_thread:false",
    }
    proc = subprocess.Popen(cmd, stdout=out.open("w"), stderr=err.open("w"), env=drill_env)
    try:
        if not _wait_for_qdrant(port):
            return {
                "status": "error",
                "error": (err.read_text(errors="ignore")[-500:] if err.exists() else "start_timeout"),
            }
        for item in restored:
            item["points_count"] = _qdrant_collection_count(port, item["target_collection"])
        if any(item.get("points_count") is None for item in restored):
            return {"status": "error", "restored": restored, "error": "restored_collection_unreadable"}
        return {"status": "ok", "restored": restored, "http_port": port}
    finally:
        proc.terminate()
        with suppress(Exception):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            with suppress(Exception):
                proc.wait(timeout=5)


def verify_qdrant_backup(tmp_dir: Path, backup_dir: Path = QDRANT_BACKUP_DIR) -> dict:
    started = time.time()
    archive = _latest_archive("qdrant-backup-", backup_dir)
    result: dict[str, Any] = {"component": "qdrant", "status": "error", "duration_s": 0.0}
    if archive is None:
        result["error"] = "backup_missing"
        return result
    result.update({"archive": str(archive), "bytes": archive.stat().st_size})
    result.update(_verify_sidecar_checksum(archive))
    if result.get("checksum_status") == "mismatch":
        result["duration_s"] = round(time.time() - started, 3)
        return result

    extract_dir = tmp_dir / "qdrant-extracted"
    extract_dir.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")
        snapshots = sorted(extract_dir.rglob("*.snapshot"))
        result["snapshot_count"] = len(snapshots)
        result["snapshots"] = [
            {
                "name": p.name,
                "bytes": p.stat().st_size,
                "status": "ok" if p.stat().st_size >= MIN_SNAPSHOT_BYTES else "small",
            }
            for p in snapshots
        ]
        small = [s for s in result["snapshots"] if s["status"] != "ok"]
        if not snapshots:
            result["error"] = "no_snapshots"
        elif small:
            result["error"] = f"small_snapshots:{[s['name'] for s in small]}"
        else:
            restore = _restore_qdrant_snapshots(snapshots, tmp_dir)
            result["restore"] = restore
            result["status"] = "ok" if restore.get("status") in {"ok", "skipped"} else "error"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    finally:
        result["duration_s"] = round(time.time() - started, 3)
    return result


def _s3_client() -> Any:
    sys.path.insert(0, str(BRAIN_ROOT / "cli"))
    from _minio import s3_client as _client

    return _client()


def _list_keys(s3: Any, prefix: str) -> list[str]:
    keys: list[str] = []
    continuation: str | None = None
    while True:
        kwargs = {"Bucket": MINIO_BUCKET, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
        if not continuation:
            break
    return keys


def verify_neo4j_backup(tmp_dir: Path) -> dict:
    started = time.time()
    result: dict[str, Any] = {"component": "neo4j", "status": "error", "duration_s": 0.0}
    try:
        s3 = _s3_client()
        archives = sorted(k for k in _list_keys(s3, NEO4J_PREFIX) if k.endswith(".tar.gz"))
        if not archives:
            result["error"] = "backup_missing"
            return result
        key = archives[-1]
        local_archive = tmp_dir / Path(key).name
        s3.download_file(MINIO_BUCKET, key, str(local_archive))
        result.update({"archive": key, "bytes": local_archive.stat().st_size})
        checksum_key = key.replace(".tar.gz", ".sha256")
        local_checksum = tmp_dir / Path(checksum_key).name
        try:
            s3.download_file(MINIO_BUCKET, checksum_key, str(local_checksum))
            expected = local_checksum.read_text().split()[0]
            actual = _sha256_file(local_archive)
            result["checksum_status"] = "ok" if expected == actual else "mismatch"
            result["sha256"] = actual
            if expected != actual:
                return result
        except Exception:
            result["checksum_status"] = "missing"

        with tarfile.open(local_archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
        dump_members = [
            m.name for m in members if m.name.startswith("neo4j-dump/") or m.name.startswith("neo4j-data/")
        ]
        result["file_count"] = len(members)
        result["payload_file_count"] = len(dump_members)
        result["sample_files"] = dump_members[:10]
        result["status"] = "ok" if dump_members else "error"
        if not dump_members:
            result["error"] = "no_neo4j_payload_files"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    finally:
        result["duration_s"] = round(time.time() - started, 3)
    return result


def run(backup_dir: Path = BACKUP_DIR, stems: tuple[str, ...] = EXPECTED_STEMS) -> dict:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    summary = {
        "started_at": started_at,
        "backup_dir": str(backup_dir),
        "results": [],
        "all_ok": True,
    }
    with tempfile.TemporaryDirectory(prefix="brain-restore-drill-") as td:
        tmp_dir = Path(td)
        for stem in stems:
            backup = latest_backup(stem, backup_dir)
            if backup is None:
                res = {"component": "sqlite", "stem": stem, "status": "error", "error": "backup_missing"}
            else:
                res = {"component": "sqlite", "stem": stem, **verify_sqlite_backup(backup, tmp_dir)}
            if res.get("status") != "ok":
                summary["all_ok"] = False
            summary["results"].append(res)

        for res in (verify_qdrant_backup(tmp_dir), verify_neo4j_backup(tmp_dir)):
            if res.get("status") != "ok":
                summary["all_ok"] = False
            summary["results"].append(res)
    summary["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(0 if out["all_ok"] else 1)
