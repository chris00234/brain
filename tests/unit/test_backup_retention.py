"""tests/unit/test_backup_retention.py — per-family newest-N keep policy.

Locks the contract: only files matching `<family>-YYYYMMDD[T...]` are
considered; everything else is left alone; per family newest N are
kept and the older files are deleted.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from backup_retention import run_backup_retention  # noqa: E402


def _touch(p: Path, mtime: float) -> None:
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))


def test_retention_keeps_newest_per_family(tmp_path: Path) -> None:
    now = time.time()
    files = {}
    for i in range(5):
        for fam in ("ghost", "couchdb"):
            p = tmp_path / f"{fam}-2026050{i + 1}.tar.gz"
            _touch(p, now - i * 86400)
            files[p.name] = p
    # An unrelated file that doesn't match the family regex must be left alone.
    misc = tmp_path / "README.md"
    misc.write_text("ignore")

    summary = run_backup_retention(target_dir=tmp_path, keep_per_family=2)
    assert summary["status"] == "ok"
    # Each family kept 2, deleted 3.
    assert summary["families"]["ghost"] == {"total": 5, "kept": 2, "deleted": 3}
    assert summary["families"]["couchdb"] == {"total": 5, "kept": 2, "deleted": 3}
    assert misc.exists()
    # The newest files survive.
    assert (tmp_path / "ghost-20260501.tar.gz").exists()
    assert (tmp_path / "couchdb-20260501.tar.gz").exists()
    # Older files were removed.
    assert not (tmp_path / "ghost-20260503.tar.gz").exists()


def test_retention_dry_run_does_not_delete(tmp_path: Path) -> None:
    now = time.time()
    for i in range(4):
        _touch(tmp_path / f"ghost-2026050{i + 1}.tar.gz", now - i * 86400)
    summary = run_backup_retention(target_dir=tmp_path, keep_per_family=1, dry_run=True)
    assert summary["families"]["ghost"]["deleted"] == 3
    # Files still on disk.
    assert (tmp_path / "ghost-20260504.tar.gz").exists()


def test_retention_missing_target(tmp_path: Path) -> None:
    summary = run_backup_retention(target_dir=tmp_path / "nope", keep_per_family=3)
    assert summary["status"] == "missing"
