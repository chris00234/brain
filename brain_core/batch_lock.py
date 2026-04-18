"""brain_core/batch_lock.py — Global file lock for heavy batch jobs.

OrbStack crashes when multiple batch processes (reindex, ingest scripts)
concurrently flood Docker-exposed ports (ChromaDB :8000, Ollama :11434)
with HTTP connections. This module provides a file lock so only one heavy
batch job runs at a time.

Usage:
    from brain_core.batch_lock import batch_lock

    with batch_lock("reindex"):
        do_heavy_work()
"""

import fcntl
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE = Path("/tmp/.brain-batch.lock")
LOCK_TIMEOUT = 600  # 10 min max wait before giving up


@contextmanager
def batch_lock(job_name: str, timeout: int = LOCK_TIMEOUT):
    """Acquire an exclusive file lock. Blocks until available or timeout."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)
    start = time.monotonic()
    acquired = False

    try:
        while time.monotonic() - start < timeout:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                # Read who holds the lock
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    holder = os.read(fd, 256).decode().strip()
                except Exception:
                    holder = "unknown"
                elapsed = int(time.monotonic() - start)
                if elapsed % 30 == 0 and elapsed > 0:
                    print(
                        f"  [{job_name}] waiting for batch lock (held by: {holder}, {elapsed}s)...",
                        file=sys.stderr,
                    )
                time.sleep(2)

        if not acquired:
            print(f"  [{job_name}] TIMEOUT waiting for batch lock after {timeout}s", file=sys.stderr)
            raise TimeoutError(f"batch lock timeout after {timeout}s")

        # Write our identity into the lock file
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{job_name} pid={os.getpid()}".encode())
        os.fsync(fd)

        yield

    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
