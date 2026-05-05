"""Source-level entry contract, chunking, and tagging policy for Brain ingest.

The policy deliberately separates three concerns:

* tagging/metadata normalization applies to every source because filters,
  governance, and recall provenance need a common shape;
* chunking is source-aware, not universally semantic. Long natural-language
  documents may use semantic boundaries; atomic events, code, and transcripts
  preserve their source-native unit of meaning;
* storage contract fields are stamped on every vector payload so old and new
  ingest paths converge on the same observable entry quality.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ENTRY_SCHEMA_VERSION = "brain-entry-v2"
CHUNK_POLICY_VERSION = "source-aware-v2"
TAG_POLICY_VERSION = "normalized-tags-v1"
PRIVACY_REDACTION_VERSION = "secret-patterns-v1"

SENSITIVE_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "explicit_password",
        re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_ -]?key|token)\s*[:=]\s*[^\s]{8,}"),
    ),
)

_TAG_RE = re.compile(r"[^a-z0-9_:-]+")

SEMANTIC_SOURCE_TYPES = frozenset(
    {
        "pdf",
        "note",
        "apple-note",
        "obsidian-note",
        "blog_post",
        "gmail",
        "email",
        "manual_ingest",
        "raw-browser",
    }
)
TURN_SOURCE_TYPES = frozenset(
    {
        "message",
        "session-memory",
        "raw-openclaw_session",
        "raw-claude_code_session",
        "openclaw_session",
        "claude_code_session",
    }
)
ATOMIC_SOURCE_TYPES = frozenset(
    {
        "event",
        "calendar",
        "reminder",
        "apple_health",
        "health",
        "screen_time",
        "kuma_heartbeat",
        "uptime",
        "active_contact",
        "image_caption",
        "raw-screen_time",
        "raw-git_activity",
        "raw-shell",
    }
)
AST_SOURCE_TYPES = frozenset({"code", "code-function", "python-function", "repo-function"})
STRUCTURED_FILE_TYPES = frozenset(
    {
        "docker-compose",
        "docker_compose",
        "nginx-conf",
        "nginx_conf",
        "agent-config",
        "agent_config",
        "json",
        "yaml",
        "toml",
        "config",
        "canonical_note",
        "distilled_note",
    }
)


def semantic_chunking_enabled() -> bool:
    return os.getenv("BRAIN_SEMANTIC_CHUNKING", "").lower() in {"1", "true", "yes"}


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


def _source_kind(source: str) -> str:
    return source_kind(source)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()


def sensitive_text_findings(text: str) -> list[str]:
    """Return secret-like pattern ids without exposing the matched text."""

    return [code for code, pattern in SENSITIVE_TEXT_PATTERNS if pattern.search(text or "")]


def redact_sensitive_text(text: str) -> tuple[str, list[str]]:
    """Redact secret-like patterns before storing retrievable text."""

    redacted = text or ""
    findings: list[str] = []
    for code, pattern in SENSITIVE_TEXT_PATTERNS:
        redacted, count = pattern.subn(f"[REDACTED:{code}]", redacted)
        if count:
            findings.append(code)
    return redacted, findings


def normalized_source_type(doc: Mapping[str, Any], *, collection: str | None = None) -> str:
    for key in ("source_type", "document_type", "type", "subtype", "kind"):
        value = doc.get(key)
        if value not in (None, "", [], {}):
            return normalize_tag(value).replace("-", "_") or "unknown"
    if collection:
        return normalize_tag(collection).replace("-", "_") or "unknown"
    return "unknown"


def normalize_tag(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace(" ", "-")
    text = _TAG_RE.sub("-", text).strip("-:_")
    return text[:80]


def normalize_tags(values: Any) -> list[str]:
    if values is None:
        raw_values: Iterable[Any] = []
    elif isinstance(values, str):
        raw_values = re.split(r"[,\s]+", values)
    elif isinstance(values, Iterable):
        raw_values = values
    else:
        raw_values = [values]

    out: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        tag = normalize_tag(value)
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _looks_like_natural_document(source: str) -> bool:
    parsed = urlparse(source or "")
    if parsed.scheme in {"http", "https"}:
        return True
    suffix = Path(source or "").suffix.lower()
    return suffix in {".md", ".markdown", ".txt", ".pdf", ".html", ".htm", ".rst"}


def is_semantic_candidate(doc: Mapping[str, Any], *, content: str = "") -> bool:
    doc_type = normalized_source_type(doc)
    source = str(doc.get("source") or doc.get("source_ref") or doc.get("path") or "")
    if doc_type in ATOMIC_SOURCE_TYPES or doc_type in AST_SOURCE_TYPES or doc_type in STRUCTURED_FILE_TYPES:
        return False
    return (doc_type in SEMANTIC_SOURCE_TYPES or _looks_like_natural_document(source)) and len(
        content or ""
    ) >= 300


def infer_chunk_strategy(
    doc: Mapping[str, Any], *, content: str = "", semantic_enabled: bool | None = None
) -> str:
    """Return the desired source-native chunking strategy for a document.

    Strategies are metadata, not an instruction to blindly re-split everything:
    ``semantic`` is for long natural-language/doc sources; ``atomic`` preserves
    one event/fact per record; ``ast`` is for code; ``turn_based`` is for chat or
    agent transcripts; ``structured`` is for config/JSON/YAML-style data;
    ``paragraph`` is the safe fallback.
    """

    doc_type = normalized_source_type(doc)
    source = str(doc.get("source") or doc.get("source_ref") or doc.get("path") or "")
    kind = str(doc.get("source_kind") or _source_kind(source))

    if doc_type in ATOMIC_SOURCE_TYPES:
        return "atomic"
    if doc_type in AST_SOURCE_TYPES or kind == "code":
        return "ast"
    if doc_type in TURN_SOURCE_TYPES:
        return "turn_based"
    if doc_type in STRUCTURED_FILE_TYPES:
        return "structured"

    if is_semantic_candidate(doc, content=content):
        enabled = semantic_chunking_enabled() if semantic_enabled is None else semantic_enabled
        return "semantic" if enabled else "paragraph"
    return "paragraph"


def policy_tags(doc: Mapping[str, Any], *, content: str = "", collection: str | None = None) -> list[str]:
    source = str(doc.get("source") or doc.get("source_ref") or doc.get("path") or "")
    strategy = infer_chunk_strategy(doc, content=content)
    source_type = normalized_source_type(doc, collection=collection)

    raw_tags: list[Any] = []
    for key in ("tags", "context_tags"):
        raw_tags.extend(normalize_tags(doc.get(key)))
    for key, prefix in (
        ("domain", "domain"),
        ("subtype", "subtype"),
        ("category", "category"),
        ("service", "service"),
        ("agent", "agent"),
        ("source_type", "source"),
        ("type", "type"),
        ("kind", "kind"),
        ("scope", "scope"),
        ("speaker_entity", "speaker"),
    ):
        value = doc.get(key)
        if value not in (None, "", [], {}):
            raw_tags.append(f"{prefix}:{value}")

    if collection:
        raw_tags.append(f"collection:{collection}")
    raw_tags.append(f"source:{source_type}")
    raw_tags.append(f"chunk:{strategy}")
    raw_tags.append(f"source_kind:{_source_kind(source)}")
    return normalize_tags(raw_tags)


def metadata_for_document(
    doc: Mapping[str, Any], *, content: str = "", collection: str | None = None
) -> dict[str, Any]:
    strategy = infer_chunk_strategy(doc, content=content)
    tags = policy_tags(doc, content=content, collection=collection)
    source = str(doc.get("source") or doc.get("source_ref") or doc.get("path") or "")
    source_type = normalized_source_type(doc, collection=collection)
    metadata: dict[str, Any] = {
        "schema_version": ENTRY_SCHEMA_VERSION,
        "entry_schema_version": ENTRY_SCHEMA_VERSION,
        "chunk_version": CHUNK_POLICY_VERSION,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "tag_policy_version": TAG_POLICY_VERSION,
        "content_hash": content_hash(content),
        "source_kind": _source_kind(source),
        "source_type": source_type,
        "chunk_strategy": strategy,
        "semantic_chunk_candidate": is_semantic_candidate(doc, content=content),
        "tags": tags,
        "context_tags": tags,
    }
    if collection:
        metadata["collection"] = collection
        metadata["vector_collection"] = collection
    if doc.get("topic_key"):
        metadata["topic_key"] = str(doc.get("topic_key"))[:120]
    if doc.get("speaker_entity"):
        metadata["speaker_entity"] = str(doc.get("speaker_entity"))[:80]
    if doc.get("scope"):
        metadata["scope"] = str(doc.get("scope"))[:80]
    return metadata


def merge_policy_metadata(base: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Merge policy metadata without clobbering existing higher-quality fields."""

    merged = dict(base)
    for key, value in policy.items():
        if key in {"tags", "context_tags"}:
            merged[key] = normalize_tags([*(normalize_tags(merged.get(key))), *(normalize_tags(value))])
        elif key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    return merged


def enrich_payload_for_entry(
    payload: Mapping[str, Any] | None,
    *,
    content: str = "",
    collection: str | None = None,
    point_id: str | None = None,
) -> dict[str, Any]:
    """Return a payload that satisfies the v2 entry contract.

    This is intentionally safe to call from the vector-store boundary. It adds
    missing contract fields, normalizes tags, and never fails the write path.
    Existing caller-provided metadata wins except for tag merging.
    """

    base = dict(payload or {})
    policy = metadata_for_document(base, content=content, collection=collection)
    enriched = merge_policy_metadata(base, policy)
    if point_id and not enriched.get("vector_point_id"):
        enriched["vector_point_id"] = point_id
    redaction_findings = sensitive_text_findings(content or "")
    if redaction_findings:
        enriched["privacy_redaction_version"] = PRIVACY_REDACTION_VERSION
        enriched["privacy_redaction_count"] = len(redaction_findings)
        enriched["privacy_redaction_codes"] = redaction_findings
    # Ensure canonical aliases are present even when callers supplied one of
    # the version keys but not the others.
    enriched.setdefault("schema_version", ENTRY_SCHEMA_VERSION)
    enriched.setdefault("entry_schema_version", ENTRY_SCHEMA_VERSION)
    enriched.setdefault("chunk_version", CHUNK_POLICY_VERSION)
    enriched.setdefault("chunk_policy_version", CHUNK_POLICY_VERSION)
    enriched.setdefault("tag_policy_version", TAG_POLICY_VERSION)
    enriched.setdefault("content_hash", content_hash(content))
    enriched["tags"] = normalize_tags(enriched.get("tags"))
    enriched["context_tags"] = normalize_tags(enriched.get("context_tags") or enriched.get("tags"))
    return enriched


def sparse_index_text(content: str, payload: Mapping[str, Any] | None = None) -> str:
    """Build exact-match text for sparse/BM25 indexing.

    Dense embeddings should stay focused on the source content. Sparse vectors
    are different: they are the right place to expose filenames, paths, tags,
    services, and provenance aliases so exact source lookups remain stable after
    re-chunking short structured files.
    """

    p = dict(payload or {})
    fields: list[Any] = [content or ""]
    for key in (
        "source",
        "source_path",
        "source_name",
        "document_title",
        "document_section",
        "document_type",
        "source_type",
        "type",
        "service",
        "agent",
        "tags",
        "context_tags",
        "source_aliases",
        "sources",
        "topic_key",
    ):
        value = p.get(key)
        if value not in (None, "", [], {}):
            fields.append(value)

    def flatten(value: Any) -> str:
        if isinstance(value, Mapping):
            return " ".join(f"{k} {flatten(v)}" for k, v in value.items())
        if isinstance(value, Iterable) and not isinstance(value, str | bytes):
            return " ".join(flatten(v) for v in value)
        return str(value or "")

    text = "\n".join(flatten(v) for v in fields if v not in (None, "", [], {}))
    # Add path components as word tokens (`default.conf` -> `default conf`) so
    # queries need not match exact punctuation.
    source = str(p.get("source_path") or p.get("source") or "")
    if source:
        text += "\n" + re.sub(r"[^A-Za-z0-9가-힣]+", " ", source)
    return text[:12000]
