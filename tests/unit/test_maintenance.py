from __future__ import annotations

import gzip
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_compress_large_log_backups_preserves_content(tmp_path, monkeypatch):
    import maintenance

    backup = tmp_path / "autonomy.db.pre-fix.bak"
    payload = b"rollback-data" * 100
    backup.write_bytes(payload)
    monkeypatch.setattr(maintenance, "LOGS_DIR", tmp_path)

    out = maintenance.compress_large_log_backups(min_size_mb=0)

    assert out["compressed"] == 1
    assert not backup.exists()
    gz_path = tmp_path / "autonomy.db.pre-fix.bak.gz"
    assert gz_path.exists()
    with gzip.open(gz_path, "rb") as fh:
        assert fh.read() == payload


def test_compress_large_log_backups_skips_small_files(tmp_path, monkeypatch):
    import maintenance

    backup = tmp_path / "small.bak"
    backup.write_bytes(b"small")
    monkeypatch.setattr(maintenance, "LOGS_DIR", tmp_path)

    out = maintenance.compress_large_log_backups(min_size_mb=1)

    assert out == {"compressed": 0, "skipped": 1}
    assert backup.exists()
    assert not (tmp_path / "small.bak.gz").exists()
