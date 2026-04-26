from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain_core"))

from ingest import healthcheck  # noqa: E402


def test_collection_counts_include_qdrant_aliases(monkeypatch):
    class FakeStore:
        def list_collections(self):
            return ["knowledge", "canonical"]

        def count(self, name):
            return {"knowledge": 10, "canonical": 20, "context": 3, "patterns": 1}.get(name, 0)

    monkeypatch.setitem(
        sys.modules,
        "vector_store",
        types.SimpleNamespace(get_vector_store=lambda: FakeStore()),
    )

    counts = healthcheck.get_collection_counts()

    assert counts["knowledge"] == 10
    assert counts["context"] == 3
    assert counts["patterns"] == 1


def test_latest_backup_age_uses_qdrant_prefix(monkeypatch):
    calls = []
    now = datetime.now(UTC)

    class FakeS3:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return {
                "Contents": [
                    {
                        "Key": "qdrant-backup-2026-04-24.tar.gz",
                        "LastModified": now - timedelta(hours=2),
                    }
                ]
            }

    monkeypatch.setitem(sys.modules, "_minio", types.SimpleNamespace(s3_client=lambda: FakeS3()))

    age, reason = healthcheck.latest_backup_age_hours()

    assert reason == "ok"
    assert age is not None and 1.9 <= age <= 2.1
    assert calls[0]["Prefix"] == "qdrant-backup-"
