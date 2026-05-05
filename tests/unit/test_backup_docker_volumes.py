from __future__ import annotations

import os
import time


def test_rotate_preserves_latest_per_label_while_enforcing_size_cap(tmp_path):
    from cli.backup_docker_volumes import _rotate

    old_huge = tmp_path / "uptime-kuma-20260501.tar.gz"
    newer_small = tmp_path / "uptime-kuma-20260503.tar.gz"
    ghost = tmp_path / "ghost-20260503.tar.gz"
    for path, size, age in [
        (old_huge, 2 * 1024 * 1024, 30),
        (newer_small, 10, 10),
        (ghost, 10, 5),
    ]:
        path.write_bytes(b"x" * size)
        ts = time.time() - age
        os.utime(path, (ts, ts))

    deleted = _rotate(tmp_path, keep_days=7, max_total_mb=0)
    assert deleted == 0

    deleted = _rotate(tmp_path, keep_days=7, max_total_mb=1)

    assert deleted == 1
    assert not old_huge.exists()
    assert newer_small.exists()
    assert ghost.exists()
