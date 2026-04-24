#!/Users/chrischo/server/brain/.venv/bin/python
"""Monthly backup verification smoke test.

Downloads the latest qdrant backup tar + sha256 from MinIO, verifies the
checksum, extracts, ensures every expected collection has a non-empty
.snapshot file, and alerts Jenna on failure.

Runs on the 1st of each month at 4:30am via brain scheduler.
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
MINIO_BUCKET = "rag-backups"
QDRANT_PREFIX = "qdrant-backup-"
QDRANT_URL = "http://127.0.0.1:6333"
# Fallback set used only when the live Qdrant is unreachable. When live,
# we discover collections dynamically so new collections are verified
# without a code change and retired collections stop alerting spuriously.
FALLBACK_COLLECTIONS = {
    "canonical",
    "semantic_memory",
    "experience",
    "knowledge",
    "code",
    "personal",
    "obsidian",
}
MIN_SNAPSHOT_BYTES = 4 * 1024  # <4KB is almost certainly an empty/corrupt snapshot


def _expected_collections() -> set[str]:
    """Ask Qdrant for the live collection list; fall back to a static set."""
    import json as _json
    import urllib.request as _url

    try:
        with _url.urlopen(f"{QDRANT_URL}/collections", timeout=10) as resp:  # noqa: S310
            body = _json.loads(resp.read())
        names = {
            c.get("name")
            for c in body.get("result", {}).get("collections", [])
            if c.get("name")
        }
        return names or FALLBACK_COLLECTIONS
    except Exception:
        return FALLBACK_COLLECTIONS


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _minio import s3_client as _s3_client


def _list_all_keys(s3, prefix: str) -> list[str]:
    """Paginated list_objects_v2 — single call caps at 1000 keys."""
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


def _latest_backup_key(s3) -> str | None:
    tarballs = [k for k in _list_all_keys(s3, QDRANT_PREFIX) if k.endswith(".tar.gz")]
    if not tarballs:
        return None
    tarballs.sort(reverse=True)
    return tarballs[0]


def _alert_failure(error_msg: str) -> None:
    try:
        subprocess.run(
            [
                OPENCLAW_BIN,
                "agent",
                "--agent",
                "jenna",
                "--message",
                f"BACKUP VERIFY FAILED: {error_msg}",
                "--thinking",
                "off",
                "--timeout",
                "30",
            ],
            timeout=35,
            capture_output=True,
        )
    except Exception as e:
        print(f"  WARNING: alert dispatch failed: {e}")


def verify_backup() -> int:
    print(f"Backup Verify — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    tmp_dir = Path(tempfile.mkdtemp(prefix="backup-verify-"))
    try:
        print("[1/5] Locating latest qdrant backup in MinIO...")
        try:
            s3 = _s3_client()
            archive_key = _latest_backup_key(s3)
        except Exception as e:
            err = f"MinIO list failed: {e}"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        if not archive_key:
            err = f"no {QDRANT_PREFIX}*.tar.gz backups found in bucket"
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
            print("  WARNING: no checksum file (legacy backup)")

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
                check=True,
                timeout=180,
            )
        except subprocess.CalledProcessError as e:
            err = f"tar extraction failed: {e}"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        print("[5/5] Validating snapshot files per collection...")
        snapshots = {p.stem: p for p in extract_dir.rglob("*.snapshot")}
        if not snapshots:
            err = "no .snapshot files found in extracted qdrant backup"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        expected = _expected_collections()
        missing = expected - set(snapshots)
        if missing:
            err = f"qdrant backup missing collections: {sorted(missing)}"
            print(f"  ERROR: {err}")
            _alert_failure(err)
            return 1

        for name, path in snapshots.items():
            size = path.stat().st_size
            if size < MIN_SNAPSHOT_BYTES:
                err = f"{name}.snapshot suspiciously small ({size} bytes < {MIN_SNAPSHOT_BYTES})"
                print(f"  ERROR: {err}")
                _alert_failure(err)
                return 1
            print(f"  {name}.snapshot: {size / (1024 * 1024):.1f} MB")

        print(
            f"\nBackup verify PASSED — {archive_key} ({len(snapshots)} snapshots, "
            f"{len(expected)} expected)"
        )
        return 0
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(verify_backup())
