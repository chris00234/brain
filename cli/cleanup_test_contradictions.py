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

from vector_store import get_vector_store  # type: ignore

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
    return any(marker.lower() in t for marker in TEST_MARKERS)


def main() -> int:
    store = get_vector_store()

    # 1. Fetch contradictions
    points = store.get(
        "semantic_contradictions",
        limit=500,
        with_payload=True,
        with_documents=True,
    )
    if not points:
        print("no semantic_contradictions found")
        return 1

    test_contra_ids: list[str] = []
    test_member_ids: set[str] = set()
    for p in points:
        doc = p.document or ""
        if _is_test_noise(doc):
            test_contra_ids.append(p.id)
            m = p.payload or {}
            for k in ("memory_id_a", "memory_id_b", "new_id", "old_id"):
                v = m.get(k)
                if v and isinstance(v, str):
                    if v.startswith("semantic_memory:"):
                        v = v.split(":", 1)[1]
                    test_member_ids.add(v)

    print(f"found {len(test_contra_ids)}/{len(points)} test-fixture contradictions")
    print(f"linked test atoms to delete: {len(test_member_ids)}")

    # 2. Delete from semantic_contradictions
    if test_contra_ids:
        store.delete("semantic_contradictions", test_contra_ids)
        print(f"deleted {len(test_contra_ids)} contradiction rows")

    # 3. Also sweep semantic_memory for orphan test atoms by SQLite audit
    brain_db = Path("/Users/chrischo/server/brain/logs/brain.db")
    sqlite_atoms_deleted = 0
    chroma_atoms_deleted = 0
    if brain_db.exists():
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
            conn.execute(f"DELETE FROM atoms WHERE id IN ({placeholders})", test_atom_ids)  # noqa: S608 — placeholders are ? marks, not user input
            conn.commit()
            sqlite_atoms_deleted = len(test_atom_ids)
        conn.close()
        if test_chroma_ids:
            # Batches of 100 to avoid payload size limits
            for i in range(0, len(test_chroma_ids), 100):
                batch = test_chroma_ids[i : i + 100]
                try:
                    store.delete("semantic_memory", batch)
                    chroma_atoms_deleted += len(batch)
                except Exception as e:
                    print(f"delete batch failed: {e}")

    summary = {
        "contradictions_deleted": len(test_contra_ids),
        "atoms_deleted_sqlite": sqlite_atoms_deleted,
        "atoms_deleted_chroma_sm": chroma_atoms_deleted,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
