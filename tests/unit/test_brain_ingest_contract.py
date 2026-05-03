from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

knowledge = importlib.import_module("routes.knowledge")


def test_brain_ingest_writes_source_aware_contract(tmp_path, monkeypatch):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    monkeypatch.setattr(knowledge, "BRAIN_DIR", brain_dir)

    import brain_core.openclaw_dispatch as openclaw_dispatch

    monkeypatch.setattr(
        openclaw_dispatch,
        "dispatch",
        lambda **kwargs: SimpleNamespace(
            ok=True,
            text=json.dumps(
                {
                    "title": "Contract note",
                    "summary": "Semantic chunking and tags are preserved.",
                    "key_facts": ["fact"],
                    "domain": "infra",
                }
            ),
            error="",
        ),
    )

    req = knowledge.BrainIngestRequest(
        content="This production note should preserve semantic chunking and source tags.",
        source="unit-test",
        category="infra",
        tags=["semantic", "tagging"],
    )

    out = knowledge.brain_ingest.__wrapped__(SimpleNamespace(), req)

    assert out["status"] == "ingested"
    written = list((tmp_path / "knowledge" / "raw" / "inbox").glob("manual_*.json"))
    assert len(written) == 1
    record = json.loads(written[0].read_text())
    for key in (
        "schema_version",
        "chunk_version",
        "tag_policy_version",
        "content_hash",
        "chunk_strategy",
        "semantic_chunk_candidate",
        "tags",
        "context_tags",
        "metadata",
    ):
        assert record.get(key) not in (None, "", [], {})
    assert "semantic" in record["tags"]
    assert record["source_type"] == "manual_ingest"
