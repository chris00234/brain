from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import sqlite3
import tarfile
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "backup_restore_drill", BRAIN_ROOT / "cli" / "backup_restore_drill.py"
)
backup_restore_drill = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backup_restore_drill)


def _make_gzipped_db(path: Path) -> None:
    raw = path.with_suffix("")
    with sqlite3.connect(raw) as conn:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t(value) VALUES ('ok')")
    with raw.open("rb") as src, gzip.open(path, "wb") as dst:
        dst.write(src.read())
    raw.unlink()


def test_restore_drill_verifies_latest_sqlite_backups(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_gzipped_db(backup_dir / "brain-20260501.db.gz")
    _make_gzipped_db(backup_dir / "autonomy-20260501.db.gz")
    report = tmp_path / "backup_restore_drill.json"
    monkeypatch.setattr(backup_restore_drill, "REPORT_FILE", report)
    monkeypatch.setattr(
        backup_restore_drill,
        "verify_qdrant_backup",
        lambda tmp_dir: {"component": "qdrant", "status": "ok"},
    )
    monkeypatch.setattr(
        backup_restore_drill,
        "verify_neo4j_backup",
        lambda tmp_dir: {"component": "neo4j", "status": "ok"},
    )

    out = backup_restore_drill.run(backup_dir=backup_dir)

    assert out["all_ok"] is True
    sqlite_results = [r for r in out["results"] if r["component"] == "sqlite"]
    assert {r["stem"] for r in sqlite_results} == {"brain", "autonomy"}
    assert all(r["integrity_check"] == "ok" for r in sqlite_results)
    assert report.exists()


def test_restore_drill_fails_when_backup_missing(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_gzipped_db(backup_dir / "brain-20260501.db.gz")
    monkeypatch.setattr(backup_restore_drill, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setattr(
        backup_restore_drill,
        "verify_qdrant_backup",
        lambda tmp_dir: {"component": "qdrant", "status": "ok"},
    )
    monkeypatch.setattr(
        backup_restore_drill,
        "verify_neo4j_backup",
        lambda tmp_dir: {"component": "neo4j", "status": "ok"},
    )

    out = backup_restore_drill.run(backup_dir=backup_dir)

    assert out["all_ok"] is False
    assert any(r.get("error") == "backup_missing" for r in out["results"])


def test_qdrant_backup_verifies_archive_checksum_and_snapshots(tmp_path, monkeypatch):
    backup_dir = tmp_path / "qdrant-backups"
    backup_dir.mkdir()
    archive = backup_dir / "qdrant-backup-20260501.tar.gz"
    payload = b"x" * backup_restore_drill.MIN_SNAPSHOT_BYTES
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("qdrant-snapshots/semantic_memory.snapshot")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix("").with_suffix(".sha256").write_text(f"{digest}  {archive.name}\n")

    monkeypatch.setattr(
        backup_restore_drill,
        "_restore_qdrant_snapshots",
        lambda snapshots, tmp_dir: {"status": "ok", "restored": [p.name for p in snapshots]},
    )

    out = backup_restore_drill.verify_qdrant_backup(tmp_path, backup_dir=backup_dir)

    assert out["status"] == "ok"
    assert out["checksum_status"] == "ok"
    assert out["snapshot_count"] == 1
    assert out["restore"]["status"] == "ok"


class _FakeS3:
    def __init__(self, files: dict[str, Path]):
        self.files = files

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        return {"Contents": [{"Key": k} for k in self.files if k.startswith(prefix)]}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.files[key].read_bytes())


def test_neo4j_backup_verifies_archive_checksum_and_payload(tmp_path, monkeypatch):
    archive = tmp_path / "neo4j-backup-20260501.tar.gz"
    payload = b"neo4j dump"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("neo4j-dump/database.dump")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    checksum = tmp_path / "neo4j-backup-20260501.sha256"
    checksum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    fake = _FakeS3({archive.name: archive, checksum.name: checksum})
    monkeypatch.setattr(backup_restore_drill, "_s3_client", lambda: fake)

    out = backup_restore_drill.verify_neo4j_backup(tmp_path)

    assert out["status"] == "ok"
    assert out["checksum_status"] == "ok"
    assert out["payload_file_count"] == 1


def test_qdrant_restore_prefers_production_snapshot_over_healthcheck(tmp_path, monkeypatch):
    selected_cmds = []
    qbin = tmp_path / "qdrant"
    qbin.write_text("#!/bin/sh\nsleep 30\n")
    qbin.chmod(0o755)
    health = tmp_path / "healthcheck_probe.snapshot"
    prod = tmp_path / "distilled.snapshot"
    health.write_bytes(b"h" * backup_restore_drill.MIN_SNAPSHOT_BYTES)
    prod.write_bytes(b"p" * (backup_restore_drill.MIN_SNAPSHOT_BYTES + 1))
    monkeypatch.setattr(backup_restore_drill, "QDRANT_BIN", str(qbin))
    monkeypatch.setattr(backup_restore_drill, "_wait_for_qdrant", lambda port: True)
    monkeypatch.setattr(backup_restore_drill, "_qdrant_collection_count", lambda port, collection: 1)

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            selected_cmds.extend(cmd)
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(backup_restore_drill.subprocess, "Popen", FakeProc)

    out = backup_restore_drill._restore_qdrant_snapshots([health, prod], tmp_path)

    assert out["status"] == "ok"
    assert "distilled.snapshot" in " ".join(selected_cmds)
    assert "healthcheck_probe.snapshot" not in " ".join(selected_cmds)


def test_qdrant_restore_sets_jemalloc_env_for_macos(tmp_path, monkeypatch):
    """Qdrant on macOS aborts with jemalloc background_thread:true unless
    overridden; the drill must launch it with the override or the SLO breaches."""
    captured_env: dict[str, str] = {}
    qbin = tmp_path / "qdrant"
    qbin.write_text("#!/bin/sh\nsleep 30\n")
    qbin.chmod(0o755)
    snap = tmp_path / "distilled.snapshot"
    snap.write_bytes(b"p" * (backup_restore_drill.MIN_SNAPSHOT_BYTES + 1))
    monkeypatch.setattr(backup_restore_drill, "QDRANT_BIN", str(qbin))
    monkeypatch.setattr(backup_restore_drill, "_wait_for_qdrant", lambda port: True)
    monkeypatch.setattr(backup_restore_drill, "_qdrant_collection_count", lambda port, collection: 1)

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(backup_restore_drill.subprocess, "Popen", FakeProc)

    backup_restore_drill._restore_qdrant_snapshots([snap], tmp_path)

    assert captured_env.get("MALLOC_CONF") == "background_thread:false"
    assert captured_env.get("_RJEM_MALLOC_CONF") == "background_thread:false"
