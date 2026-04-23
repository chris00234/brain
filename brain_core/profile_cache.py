"""Cached reader for identity + state markdown files.

Used by /profile routes and /metrics to expose the current profile/state
snapshot to agents. TTL-guarded; mtime-aware so edits pick up promptly.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from config import IDENTITY_FILE, STATE_FILE

PROFILE_CACHE_TTL = 60


class ProfileCache:
    def __init__(self, paths: list[Path] | Path, ttl_seconds: int = 60) -> None:
        self.paths: list[Path] = paths if isinstance(paths, list) else [paths]
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._content: str | None = None
        self._mtimes: tuple[float, ...] = ()
        self._last_check: float = 0.0

    def get(self) -> str | None:
        with self._lock:
            now = time.time()
            if self._content is not None and (now - self._last_check) < self.ttl:
                return self._content
            existing = [p for p in self.paths if p.exists()]
            if not existing:
                return None
            current_mtimes = tuple(p.stat().st_mtime for p in existing)
            if self._content is None or current_mtimes != self._mtimes:
                parts = [p.read_text() for p in existing]
                self._content = "\n\n".join(parts)
                self._mtimes = current_mtimes
            self._last_check = now
            return self._content

    def section(self, name: str) -> str | None:
        full = self.get()
        if not full:
            return None
        target = name.replace("_", " ").lower()
        out_lines: list[str] = []
        capturing = False
        in_frontmatter = False
        for line in full.splitlines():
            stripped = line.strip()
            if stripped.startswith("---"):
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if stripped.startswith("## "):
                if capturing:
                    break
                if stripped[3:].strip().lower().startswith(target):
                    capturing = True
                    out_lines.append(line)
                    continue
            if capturing:
                out_lines.append(line)
        return "\n".join(out_lines).strip() if out_lines else None


profile_cache = ProfileCache([IDENTITY_FILE, STATE_FILE], ttl_seconds=PROFILE_CACHE_TTL)
