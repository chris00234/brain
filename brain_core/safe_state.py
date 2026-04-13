"""brain_core/safe_state.py — atomic state file operations with file locking.

Prevents concurrent ingest adapters from corrupting shared state files.
Uses fcntl.flock for advisory locking + atomic rename for crash safety.
"""
import fcntl
import json
import os
import random
from pathlib import Path


def _lock_path(path: Path) -> Path:
    """Persistent lock file that is never renamed — stable inode for flock."""
    return path.with_suffix(".lock")


def load_state(path: Path) -> dict:
    lock = _lock_path(path)
    lock_fd = os.open(str(lock), os.O_CREAT | os.O_RDONLY, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_SH)
    try:
        if not path.exists():
            return {}
        with open(path, "r") as f:
            return json.loads(f.read())
    except (json.JSONDecodeError, FileNotFoundError):
        return {}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    lock = _lock_path(path)
    lock_fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(state, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(path)  # atomic on POSIX, under stable lock file
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomic file write: write to unique .tmp, fsync, then rename."""
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}.{random.randint(0, 9999)}"
    fd = tmp.open("w", encoding="utf-8")
    try:
        fd.write(content)
        fd.flush()
        os.fsync(fd.fileno())
    finally:
        fd.close()
    tmp.rename(path)
