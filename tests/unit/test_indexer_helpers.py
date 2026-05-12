"""Behavioral unit tests for indexer.py pure helpers.

indexer.py is 1849 lines and previously had only smoke-test coverage.
These helpers are I/O-free and drive the chunking + secret-filter
pipeline used by every memory write:

  - filter_secrets: redacts api keys, tokens, ghp_/sk- secrets, ssh keys
  - file_hash: md5 hex digest (deterministic)
  - chunk_text: paragraph -> sentence -> char-boundary chunker
  - enforce_max_chunk_size: post-processor that preserves parent
    section headers on parts 2+ (the 2026-04-12 fix)

These are the lowest-level primitives in the ingest path. Quiet
regressions here corrupt every memory write across the system.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── filter_secrets ───────────────────────────────────────────────────────


def test_filter_secrets_redacts_api_key_assignments():
    from indexer import filter_secrets

    out = filter_secrets("api_key=AKIAIOSFODNN7EXAMPLE")
    assert "AKIA" not in out
    assert "[REDACTED]" in out


def test_filter_secrets_redacts_ghp_token():
    from indexer import filter_secrets

    # 36-char ghp token pattern
    raw = "X-Github: ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    out = filter_secrets(raw)
    assert "ghp_abcdefghij" not in out
    assert "[REDACTED]" in out


def test_filter_secrets_redacts_sk_openai_token():
    from indexer import filter_secrets

    # 48-char sk- token
    raw = "API key: sk-" + "a" * 48
    out = filter_secrets(raw)
    assert "[REDACTED]" in out


def test_filter_secrets_redacts_long_base64():
    from indexer import filter_secrets

    payload = "Header: " + "A" * 80 + "=="
    out = filter_secrets(payload)
    assert "[REDACTED]" in out


def test_filter_secrets_leaves_clean_text_untouched():
    from indexer import filter_secrets

    raw = "The quick brown fox jumps over the lazy dog."
    assert filter_secrets(raw) == raw


# ── file_hash ────────────────────────────────────────────────────────────


def test_file_hash_is_deterministic_md5_hex():
    from indexer import file_hash

    h = file_hash("hello world")
    assert len(h) == 32
    assert h == file_hash("hello world")
    assert h != file_hash("hello world!")


# ── chunk_text ───────────────────────────────────────────────────────────


def test_chunk_text_short_input_returns_single_full_chunk():
    from indexer import chunk_text

    out = chunk_text("short text")
    assert len(out) == 1
    assert out[0]["content"] == "short text"
    assert out[0]["section"] == "full"


def test_chunk_text_splits_oversized_at_paragraph_boundaries():
    from indexer import MAX_CHUNK_SIZE, chunk_text

    # 3 paragraphs, each near max — should produce 3 chunks
    p1 = "alpha. " * 100  # ~700 chars
    p2 = "beta. " * 100  # ~600 chars
    p3 = "gamma. " * 100  # ~700 chars
    text = f"{p1}\n\n{p2}\n\n{p3}"
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    for c in chunks:
        # No chunk should radically exceed the cap (overlap tolerated)
        assert len(c["content"]) <= MAX_CHUNK_SIZE + 250
        assert c["section"].startswith("part") or c["section"] == "full"


def test_chunk_text_splits_long_paragraph_at_sentences():
    from indexer import chunk_text

    # Single huge paragraph with sentence boundaries
    sentences = [f"This is sentence number {i}." for i in range(80)]
    text = " ".join(sentences)
    chunks = chunk_text(text)
    assert len(chunks) > 1


# ── enforce_max_chunk_size ───────────────────────────────────────────────


def test_enforce_max_chunk_size_propagates_section_to_part_2_plus():
    """2026-04-12 fix: parts 2+ of an oversized chunk must carry the
    parent section header so the embed text retains its topic anchor.

    chunk_text splits on \\n\\n paragraphs then sentence boundaries; use
    real sentences so the recursive split actually fires.
    """
    from indexer import enforce_max_chunk_size

    # Build oversized content with sentence punctuation so chunk_text
    # can split it. Use a section name NOT present in the body — otherwise
    # the "header already in chunk?" branch fires and skips prepending.
    sentence = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    body = sentence * 80
    chunks = [{"content": body, "section": "Migration Notes"}]
    out = enforce_max_chunk_size(chunks)
    assert len(out) > 1, f"expected split, got {len(out)} chunk(s)"
    parts_2_plus = out[1:]
    assert all(
        "## Migration Notes" in c["content"] for c in parts_2_plus
    ), "parent section header was not propagated to parts 2+"


def test_enforce_max_chunk_size_does_not_double_header_when_already_present():
    """If a sub-chunk already starts with the parent section text, the
    function must NOT add a second copy of the header."""
    from indexer import enforce_max_chunk_size

    # Construct a single oversized chunk whose body already begins with
    # the section name. enforce should split it but NOT duplicate the header.
    body = "deployment notes go here. " + "lorem ipsum " * 200
    chunks = [{"content": body, "section": "Deployment"}]
    out = enforce_max_chunk_size(chunks)
    # No chunk should contain "## Deployment\n\ndeployment notes"
    for c in out:
        assert c["content"].count("## Deployment") <= 1


def test_enforce_max_chunk_size_preserves_small_chunks_unchanged():
    from indexer import enforce_max_chunk_size

    chunks = [
        {"content": "short one", "section": "alpha"},
        {"content": "another short", "section": "beta"},
    ]
    out = enforce_max_chunk_size(chunks)
    assert out == chunks
