from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import entry_contract_audit  # noqa: E402


def test_missing_contract_keys_detects_empty_required_fields():
    payload = {
        "schema_version": "brain-entry-v2",
        "entry_schema_version": "brain-entry-v2",
        "chunk_version": "source-aware-v2",
        "chunk_policy_version": "source-aware-v2",
        "tag_policy_version": "normalized-tags-v1",
        "content_hash": "abc",
        "source_kind": "file",
        "source_type": "note",
        "chunk_strategy": "semantic",
        "semantic_chunk_candidate": True,
        "tags": [],
        "context_tags": ["note"],
        "vector_collection": "knowledge",
        "vector_point_id": "vec-1",
    }

    assert entry_contract_audit.missing_contract_keys(payload) == ["tags"]


def test_missing_contract_keys_accepts_full_contract():
    payload = {key: "x" for key in entry_contract_audit.REQUIRED_ENTRY_KEYS}
    payload["semantic_chunk_candidate"] = False
    payload["tags"] = ["source:note"]
    payload["context_tags"] = ["source:note"]

    assert entry_contract_audit.missing_contract_keys(payload) == []
