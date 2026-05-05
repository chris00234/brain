"""Document provenance helpers for Brain indexing and recall.

The vector store stores *chunks*, while Chris usually asks for the original
document.  Keep a stable document-level identity beside every chunk so recall
can answer both "what did it say?" and "where did this come from?".
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _stable_slug(value: str, *, max_len: int = 80) -> str:
    slug = _NON_WORD.sub("-", value.lower()).strip("-")
    return slug[:max_len].strip("-") or "document"


def canonical_source(source: str) -> str:
    """Return a stable canonical source string for ids and aliases.

    Existing source metadata is usually an absolute path, but manual/URL
    ingests may provide arbitrary names.  Paths are normalized without requiring
    the file to exist; URLs keep scheme+host+path; opaque sources pass through.
    """

    raw = (source or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    try:
        return str(Path(raw).expanduser())
    except (OSError, RuntimeError, ValueError):
        return raw


def document_id_for_source(source: str, *, title: str = "", content: str = "") -> str:
    """Build a stable document id.

    Prefer source/path stability.  When source is missing (manual snippets),
    fall back to title plus a content prefix so repeated snippets dedupe.
    """

    canonical = canonical_source(source)
    basis = canonical or f"{title.strip()}:{content[:500]}"
    digest = hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()[:16]
    label = _stable_slug(Path(canonical).name if canonical else title)
    return f"doc:{label}:{digest}"


def source_kind(source: str) -> str:
    raw = (source or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return "url"
    if raw.startswith("raw_") or raw.startswith("manual_"):
        return "manual"
    if raw and Path(raw).suffix:
        return "file"
    return "named_source" if raw else "unknown"


def document_provenance_fields(
    *,
    source: str,
    content: str = "",
    title: str = "",
    doc_type: str = "",
    section: str = "",
) -> dict[str, Any]:
    """Return metadata fields that link chunks, notes, and source documents."""

    canonical = canonical_source(source)
    p = Path(canonical) if canonical and "://" not in canonical else None
    source_name = p.name if p else (urlparse(canonical).path.rsplit("/", 1)[-1] or canonical)
    doc_title = title or source_name or "Untitled document"
    doc_id = document_id_for_source(canonical, title=doc_title, content=content)

    aliases = [a for a in {canonical, source, source_name, doc_title, p.stem if p else ""} if a]
    return {
        "document_id": doc_id,
        "source_document_id": doc_id,
        "source_kind": source_kind(source),
        "source_path": canonical,
        "source_name": source_name,
        "document_title": doc_title,
        "document_section": section or "",
        "document_type": doc_type or "",
        "source_aliases": aliases,
    }


def merge_source_aliases(existing: Any, additions: list[str]) -> list[str]:
    merged: list[str] = []
    for value in list(existing or []) + additions:
        if not value:
            continue
        text = str(value)
        if text not in merged:
            merged.append(text)
    return merged
