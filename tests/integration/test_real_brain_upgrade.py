"""Phase N — "real brain upgrade" R1–R7 verification suite.

Integration tests that validate the measurable claims from
/Users/chrischo/.claude/plans/eventual-wobbling-seahorse.md.

Skipped by default — set BRAIN_INTEGRATION_TESTS=1 to run against a live
brain-server on 127.0.0.1:8791. Tests prefixed R<n> map directly to the
plan's verification suite. R3 (30-day loop autonomy) and R6 (6-month drift)
are out of scope for one-run tests — covered by production telemetry
instead.

Usage:
    BRAIN_INTEGRATION_TESTS=1 .venv/bin/python -m pytest tests/integration/test_real_brain_upgrade.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_INTEGRATION_TESTS") != "1",
    reason="live brain required (BRAIN_INTEGRATION_TESTS=1 to enable)",
)

# The atoms_store module short-circuits every helper when BRAIN_ATOMS_ENABLED
# is falsy — the live brain-server sets this via its launchd plist, but pytest
# starts in a fresh env. Force it on before the R2 test imports atoms_store.
os.environ["BRAIN_ATOMS_ENABLED"] = "true"

BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")


def _token() -> str:
    if not SECRET_FILE.exists():
        pytest.skip(f"secret missing: {SECRET_FILE}")
    return SECRET_FILE.read_text().strip()


def _post(path: str, body: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        BRAIN_URL + path,
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def _delete(path: str, timeout: int = 10) -> None:
    req = urllib.request.Request(BRAIN_URL + path, method="DELETE")
    req.add_header("Authorization", f"Bearer {_token()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310
            pass
    except Exception:
        pass


def _query_brain_db(sql: str, params: tuple = ()) -> list[tuple]:
    if not BRAIN_DB.exists():
        return []
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── R1: hot-path contradiction auto-detection ─────────────────────────
def test_r1_contradictions_land_in_response_and_audit():
    tag = f"R1{uuid.uuid4().hex[:10]}"
    content_a = f"{tag} the canonical test city is Irvine"
    content_b = f"{tag} the canonical test city is LA"

    resp_a = _post(
        "/memory",
        {"content": content_a, "category": "fact", "agent": "test", "confidence": 0.7},
    )
    time.sleep(0.3)
    resp_b = _post(
        "/memory",
        {"content": content_b, "category": "fact", "agent": "test", "confidence": 0.85},
    )

    cleanup_ids = [resp_a.get("id"), resp_b.get("id")]
    try:
        # The second POST should surface contradictions in metadata
        contradictions = (resp_b.get("metadata") or {}).get("contradictions") or []
        assert contradictions, f"R1 failed — no contradictions in resp_b: {resp_b}"

        # action_audit must carry the predictive_error audit
        rows = _query_brain_db(
            "SELECT COUNT(*) FROM action_audit "
            "WHERE tool='predictive_error' AND query_text LIKE ?",
            (f"%{tag}%",),
        )
        assert rows and rows[0][0] >= 1, "R1 failed — no predictive_error audit row"
    finally:
        for mid in cleanup_ids:
            if mid:
                _delete(f"/memory/{mid}")


# ── R2: mutable Bayesian confidence via ledger ─────────────────────────
def test_r2_confidence_history_accumulates():
    from brain_core.atoms_store import (  # noqa: E402
        derive_atom_id,
        get_confidence_history,
        update_atom_confidence,
        upsert_atom,
    )

    unique = int(time.time())
    chroma_id = f"semantic_memory:R2_probe_{unique}"
    atom_id = upsert_atom(
        text=f"R2 probe atom {unique}",
        chroma_id=chroma_id,
        kind="fact",
        confidence=0.5,
    )
    assert atom_id == derive_atom_id(chroma_id)

    update_atom_confidence(atom_id, "corroborate", 0.5, "R2_ev1")
    update_atom_confidence(atom_id, "corroborate", 0.5, "R2_ev2")
    update_atom_confidence(atom_id, "corroborate", 0.5, "R2_ev3")
    update_atom_confidence(atom_id, "contradict", -1.0, "R2_ev4")

    history = get_confidence_history(atom_id)
    assert len(history) >= 4, f"R2 failed — history len={len(history)}"
    assert history[0]["event_type"] == "contradict"


# ── R4: sleep consolidation completes + logs a cycle row ──────────────
def test_r4_sleep_cycle_runs_and_logs():
    before = _query_brain_db("SELECT COUNT(*) FROM sleep_cycles")
    baseline = before[0][0] if before else 0

    result = _post("/jobs/sleep_consolidate", {}, timeout=120)
    assert result.get("status") in {"queued", "ok"}

    # Poll until a NEW row appears AND its ended_at is non-null (job finished).
    # The cycle row is inserted at start with ended_at NULL; only when the
    # run completes does it get updated.
    deadline = time.time() + 180
    completed = None
    while time.time() < deadline:
        after = _query_brain_db(
            "SELECT id, ended_at, replay_count FROM sleep_cycles "
            "WHERE id > ? ORDER BY id DESC LIMIT 1",
            (baseline,),
        )
        if after and after[0][1]:
            completed = after[0]
            break
        time.sleep(3)

    assert completed is not None, "R4 failed — sleep_consolidate never completed"


# ── R5: provenance wiring is live (upsert_entity + link_atom_entity work) ───
def test_r5_atom_entity_link_path_live():
    """R5 plan target is atom_entity density >= 2.0 over 30 days of real
    usage. On a one-run test we can only verify the WIRING: upsert_entity
    writes to entities, link_atom_entity writes to atom_entity, and both
    idempotent. Production telemetry will confirm the density.
    """
    from brain_core.atoms_store import (  # noqa: E402
        derive_atom_id,
        link_atom_entity,
        upsert_atom,
        upsert_entity,
    )

    unique = int(time.time())
    chroma_id = f"semantic_memory:R5_probe_{unique}"
    atom_id = upsert_atom(
        text=f"R5 probe atom {unique}",
        chroma_id=chroma_id,
        kind="fact",
        confidence=0.5,
    )
    assert atom_id == derive_atom_id(chroma_id)

    eid = upsert_entity(f"R5Entity_{unique}", entity_type="concept")
    assert eid, "R5 failed — upsert_entity returned None"
    linked = link_atom_entity(atom_id, eid, role="subject")
    assert linked, "R5 failed — link_atom_entity returned False"

    rows = _query_brain_db(
        "SELECT COUNT(*) FROM atom_entity WHERE atom_id = ?", (atom_id,)
    )
    assert rows and rows[0][0] == 1


# ── R7: predictive error signal + ledger row both land ────────────────
def test_r7_predictive_error_pair():
    # UUID-scoped prefix so leftover memories from prior test runs can't
    # collapse into NOOP via memory_operations.classify_operation.
    tag = f"R7{uuid.uuid4().hex[:10]}"
    content_a = f"{tag} Chris prefers React for frontend projects"
    content_b = f"{tag} Chris prefers Vue for frontend projects"
    resp_a = _post(
        "/memory",
        {"content": content_a, "category": "preference", "agent": "test", "confidence": 0.6},
    )
    time.sleep(0.3)
    resp_b = _post(
        "/memory",
        {"content": content_b, "category": "preference", "agent": "test", "confidence": 0.9},
    )
    try:
        audit = _query_brain_db(
            "SELECT COUNT(*) FROM action_audit WHERE tool='predictive_error' "
            "AND query_text LIKE ?",
            (f"%{tag}%",),
        )
        assert audit and audit[0][0] >= 1, "R7 failed — no predictive_error audit"

        evidence = _query_brain_db(
            "SELECT COUNT(*) FROM atom_evidence WHERE event_type='contradict' "
            "AND created_at >= datetime('now', '-1 minute')"
        )
        assert evidence and evidence[0][0] >= 1, "R7 failed — no contradict ledger row"
    finally:
        for mid in (resp_a.get("id"), resp_b.get("id")):
            if mid:
                _delete(f"/memory/{mid}")
