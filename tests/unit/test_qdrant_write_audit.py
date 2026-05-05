from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))

import audit_qdrant_writes  # noqa: E402


def test_audit_rejects_raw_qdrant_write(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from qdrant_client import QdrantClient\n"
        "client = QdrantClient(url='http://localhost:6333')\n"
        "client.upsert(collection_name='knowledge', points=[])\n"
    )

    violations = audit_qdrant_writes.run([bad])

    assert len(violations) == 1
    assert violations[0].method == "upsert"


def test_audit_ignores_vector_store_boundary():
    violations = audit_qdrant_writes.run([ROOT / "brain_core" / "qdrant_store.py"])

    assert violations == []
