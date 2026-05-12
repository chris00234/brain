from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import source_policy


def test_tags_apply_to_atomic_sources_without_semantic_chunking(monkeypatch):
    monkeypatch.setenv("BRAIN_SEMANTIC_CHUNKING", "1")

    meta = source_policy.metadata_for_document(
        {
            "type": "reminder",
            "source": "apple-reminders",
            "service": "Family",
            "tags": ["High Value", "family"],
        },
        content="Buy milk",
    )

    assert meta["chunk_strategy"] == "atomic"
    assert meta["semantic_chunk_candidate"] is False
    assert "high-value" in meta["tags"]
    assert "type:reminder" in meta["tags"]
    assert "chunk:atomic" in meta["tags"]


def test_semantic_strategy_is_for_long_natural_documents(monkeypatch):
    monkeypatch.setenv("BRAIN_SEMANTIC_CHUNKING", "1")
    content = "This is a natural-language note. " * 30

    meta = source_policy.metadata_for_document(
        {"type": "obsidian-note", "source": "/tmp/note.md", "domain": "chris"},
        content=content,
    )

    assert meta["chunk_strategy"] == "semantic"
    assert meta["semantic_chunk_candidate"] is True
    assert "domain:chris" in meta["tags"]
    assert "chunk:semantic" in meta["tags"]


def test_structured_config_file_is_not_semantic_even_when_long(monkeypatch):
    monkeypatch.setenv("BRAIN_SEMANTIC_CHUNKING", "1")
    content = "services:\n  brain:\n    image: brain\n" * 40

    meta = source_policy.metadata_for_document(
        {"type": "docker-compose", "source": "/tmp/docker-compose.yml", "service": "brain"},
        content=content,
    )

    assert meta["chunk_strategy"] == "structured"
    assert meta["semantic_chunk_candidate"] is False
    assert "type:docker-compose" in meta["tags"]


def test_entry_contract_fields_are_stamped_for_every_source(monkeypatch):
    monkeypatch.delenv("BRAIN_SEMANTIC_CHUNKING", raising=False)

    payload = source_policy.enrich_payload_for_entry(
        {"type": "reminder", "source": "apple-reminders", "tags": ["Family"]},
        content="Buy milk",
        collection="personal",
        point_id="rem-1",
    )

    assert payload["schema_version"] == source_policy.ENTRY_SCHEMA_VERSION
    assert payload["chunk_version"] == source_policy.CHUNK_POLICY_VERSION
    assert payload["tag_policy_version"] == source_policy.TAG_POLICY_VERSION
    assert payload["content_hash"] == source_policy.content_hash("Buy milk")
    assert payload["source_type"] == "reminder"
    assert payload["source_kind"] == "named_source"
    assert payload["vector_collection"] == "personal"
    assert payload["chunk_strategy"] == "atomic"
    assert payload["vector_point_id"] == "rem-1"
    assert "family" in payload["tags"]
    assert "collection:personal" in payload["tags"]


def test_sensitive_text_redaction_suppresses_secret_patterns():
    # Build the GitHub-PAT-shaped fixture at runtime so the source file
    # doesn't carry a literal that GitHub's secret scanner would flag.
    fake_pat = "g" + "hp_" + "abcdefghijklmnopqrstuvwxyz123456"
    redacted, findings = source_policy.redact_sensitive_text(f"the value is {fake_pat}")

    assert findings == ["github_token"]
    assert "ghp_" not in redacted
    assert "[REDACTED:github_token]" in redacted


def test_entry_contract_records_privacy_redaction_metadata():
    payload = source_policy.enrich_payload_for_entry(
        {"type": "note", "source": "apple-notes://1"},
        content="password: definitely-secret-value",
        collection="personal",
    )

    assert payload["privacy_redaction_version"] == source_policy.PRIVACY_REDACTION_VERSION
    assert payload["privacy_redaction_count"] == 1
    assert payload["privacy_redaction_codes"] == ["explicit_password"]


def test_structured_strategy_for_config_files(monkeypatch):
    monkeypatch.setenv("BRAIN_SEMANTIC_CHUNKING", "1")
    meta = source_policy.metadata_for_document(
        {"type": "docker-compose", "source": "/tmp/docker-compose.yml"},
        content="services:\n  app:\n    image: test\n" * 50,
    )

    assert meta["chunk_strategy"] == "structured"
    assert meta["semantic_chunk_candidate"] is False


def test_canonical_notes_are_structured_not_sentence_semantic(monkeypatch):
    monkeypatch.setenv("BRAIN_SEMANTIC_CHUNKING", "1")
    meta = source_policy.metadata_for_document(
        {"type": "canonical-note", "source": "/tmp/canonical/foo.md"},
        content=("## Statement\n\nThis is a governed truth note. " * 40),
    )

    assert meta["chunk_strategy"] == "structured"
    assert meta["semantic_chunk_candidate"] is False
