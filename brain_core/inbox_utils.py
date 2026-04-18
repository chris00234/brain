"""Shared helpers for reading raw/inbox records.

Collapses the duplicated file-glob + near-duplicate-detection logic that was
open-coded in ingest/browser.py, ingest/gmail.py, and brain_core/boot_context.py.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

try:
    from config import INBOX_DIR
except ImportError:
    INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")


_DEDUP_TOKEN_RE = re.compile(r"[a-z0-9_\-]{3,}")
_dedup_token_cache: dict[str, set[str]] = {}
_DEDUP_CACHE_MAX = 200


def get_recent_inbox_records(
    prefix: str = "raw_",
    hours: float | None = None,
    limit: int | None = None,
    parse: bool = False,
    inbox_dir: Path | None = None,
) -> list:
    """Return recent raw inbox records sorted by mtime desc.

    Args:
        prefix: glob prefix (e.g. 'raw_oc_' for OpenClaw sessions, 'raw_cc_' for
            Claude Code sessions, 'raw_' for everything). A trailing '*' is added.
        hours: if set, only include files with mtime within this many hours.
        limit: if set, cap the result count after sorting.
        parse: if True, json.loads() each file and return list of dicts
            (parse failures silently skipped). If False, return list of Paths.
        inbox_dir: override for INBOX_DIR (mainly for tests).

    Returns:
        list[Path] if parse=False, list[dict] if parse=True.
    """
    inbox = inbox_dir or INBOX_DIR
    if not inbox.exists():
        return []
    paths = list(inbox.glob(f"{prefix}*.json"))
    if hours is not None:
        cutoff = time.time() - hours * 3600
        paths = [p for p in paths if p.stat().st_mtime >= cutoff]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        paths = paths[:limit]
    if not parse:
        return paths
    records: list[dict] = []
    for p in paths:
        try:
            records.append(json.loads(p.read_text()))
        except Exception:
            continue
    return records


def is_near_duplicate(
    content: str,
    window: int = 50,
    threshold: float = 0.7,
    inbox_dir: Path | None = None,
) -> bool:
    """Return True if content is a near-duplicate of any recent raw inbox record.

    Uses Jaccard similarity on lowercased alphanumeric tokens ≥ 3 chars.
    Caches token sets per file path to avoid re-reading files across calls in the
    same process. Cache is bounded at _DEDUP_CACHE_MAX entries.
    """
    tokens = set(_DEDUP_TOKEN_RE.findall(content.lower()))
    if len(tokens) < 5:
        return False

    if len(_dedup_token_cache) > _DEDUP_CACHE_MAX:
        _dedup_token_cache.clear()

    recent = get_recent_inbox_records(
        prefix="raw_",
        limit=window,
        inbox_dir=inbox_dir,
        parse=False,
    )
    for f in recent:
        fkey = str(f)
        existing_tokens = _dedup_token_cache.get(fkey)
        if existing_tokens is None:
            try:
                existing = json.loads(f.read_text())
                existing_tokens = set(_DEDUP_TOKEN_RE.findall(existing.get("content", "").lower()))
                _dedup_token_cache[fkey] = existing_tokens
            except Exception:
                continue
        if not existing_tokens:
            continue
        overlap = len(tokens & existing_tokens)
        union = max(len(tokens | existing_tokens), 1)
        if overlap / union > threshold:
            return True
    return False
