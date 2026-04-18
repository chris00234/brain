#!/Users/chrischo/server/brain/.venv/bin/python
"""cli/cleanup_test_contradictions.py — scrub test-fixture noise.

2026-04-17: integration test `test_r1_new_memory_triggers_predictive_error`
intentionally creates contradictions between two `canonical test city`
memories and between `prefers Vue/React` memories. The test cleans up
its own memory records in `finally:` but does NOT clean up the
semantic_contradictions rows it triggered, so every test run leaves
contradictions behind. After N runs, /brain/doubt surfaces these as
"open contradictions" that have nothing to do with Chris's real beliefs.

This script:
  1. Walks semantic_contradictions looking for docs matching the test
     fixtures (explicit markers: "canonical test city", "prefers Vue for
     frontend", debug tag prefixes R1/R7/DBG).
  2. Deletes matching contradictions from Chroma.
  3. Also deletes matching test atoms in atoms SQLite + semantic_memory
     Chroma that never got reaped.
Idempotent. Safe to rerun.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

from http_pool import http_json  # type: ignore
from search import get_collections  # type: ignore

CHROMA_BASE = "http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"

TEST_MARKERS = [
    "canonical test city",
    "prefers Vue for frontend",
    "prefers React for frontend N2",
    "prefers React for frontend projects",  # matches the R7/R706/DBG test fixtures
    "prefers Vue for frontend projects",
    "R1 probe ",
    "R2 probe ",
    "R7probe",
    "R706",
    "DBG1776",
    # The canonical_preference_frontend_stack note is real; protect it by
    # never matching its exact title "Preferred frontend stack".
]


def _is_test_noise(text: str) -> bool:
    t = (text or "").lower()
    # Protect the real canonical about frontend stack
    if "preferred frontend stack" in t or "react + vite + typescript" in t:
        return False
    for marker in TEST_MARKERS:
        if marker.lower() in t:
            return True
    return False


def main() -> int:
    cols = get_collections()
    contra_col = cols.get("semantic_contradictions")
    sm_col = cols.get("semantic_memory")
    if not contra_col:
        print("no semantic_contradictions collection")
        return 1

    # 1. Fetch contradictions
    resp = http_json(
        "POST",
        f"{CHROMA_BASE}/{contra_col}/get",
        {"limit": 500, "include": ["documents", "metadatas"]},
    )
    ids = resp.get("ids", []) or []
    docs = resp.get("documents", []) or []
    metas = resp.get("metadatas", []) or []

    test_contra_ids: list[str] = []
    test_member_ids: set[str] = set()
    for i, cid in enumerate(ids):
        doc = docs[i] if i < len(docs) else ""
        if _is_test_noise(doc):
            test_contra_ids.append(cid)
            m = metas[i] if i < len(metas) else {}
            for k in ("memory_id_a", "memory_id_b", "new_id", "old_id"):
                v = (m or {}).get(k)
                if v and isinstance(v, str):
                    # Normalize semantic_memory: prefix into raw chroma_id
                    if v.startswith("semantic_memory:"):
                        v = v.split(":", 1)[1]
                    test_member_ids.add(v)

    print(f"found {len(test_contra_ids)}/{len(ids)} test-fixture contradictions")
    print(f"linked test atoms to delete: {len(test_member_ids)}")

    # 2. Delete from semantic_contradictions
    if test_contra_ids:
        http_json(
            "POST",
            f"{CHROMA_BASE}/{contra_col}/delete",
            {"ids": test_contra_ids},
        )
        print(f"deleted {len(test_contra_ids)} contradiction rows")

    # 3. Also sweep semantic_memory for orphan test atoms by SQLite audit
    brain_db = Path("/Users/chrischo/server/brain/logs/brain.db")
    sqlite_atoms_deleted = 0
    chroma_atoms_deleted = 0
    if brain_db.exists() and sm_col:
        conn = sqlite3.connect(str(brain_db))
        rows = conn.execute(
            "SELECT id, chroma_id, text FROM atoms "
            "WHERE tier = 'episodic' AND kind IN ('fact', 'preference')"
        ).fetchall()
        test_atom_ids: list[str] = []
        test_chroma_ids: list[str] = []
        for atom_id, chroma_id, text in rows:
            if _is_test_noise(text):
                test_atom_ids.append(atom_id)
                if chroma_id:
                    test_chroma_ids.append(chroma_id)
        if test_atom_ids:
            placeholders = ",".join("?" for _ in test_atom_ids)
            conn.execute(f"DELETE FROM atoms WHERE id IN ({placeholders})", test_atom_ids)
            conn.commit()
            sqlite_atoms_deleted = len(test_atom_ids)
        conn.close()
        if test_chroma_ids:
            # Batches of 100 to avoid payload size limits
            for i in range(0, len(test_chroma_ids), 100):
                batch = test_chroma_ids[i : i + 100]
                try:
                    http_json(
                        "POST",
                        f"{CHROMA_BASE}/{sm_col}/delete",
                        {"ids": batch},
                    )
                    chroma_atoms_deleted += len(batch)
                except Exception as e:
                    print(f"chroma delete batch failed: {e}")

    summary = {
        "contradictions_deleted": len(test_contra_ids),
        "atoms_deleted_sqlite": sqlite_atoms_deleted,
        "atoms_deleted_chroma_sm": chroma_atoms_deleted,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
