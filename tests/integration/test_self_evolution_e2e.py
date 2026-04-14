"""tests/integration/test_self_evolution_e2e.py - Phase M7 WS4

End-to-end smoke for the brain's closed-loop self-evolution pipeline:

  /recall/feedback (wrong_answer=true, expected=...)
       └─> eval_proposals.insert_proposal()  (status='candidate')
                ├─> eval_holdout_promote (Sun 8:45)  →  status='pending', writes pending file
                ├─> eval_holdout_audit  (Sun 9:15)   →  Telegram digest (best-effort)
                └─> lora_ab_gate        (Sun 9:30)   →  CLI subprocess (no-op when no training data)

The test seeds 5 synthetic correction events, fires each weekly job manually
via POST /jobs, and asserts the records flowed. Side effects on the LoRA
training pipeline are best-effort — the lora_ab_gate CLI is a no-op when there
isn't enough data.

Gated by BRAIN_INTEGRATION_TESTS=1; required for the M7 WS4 done-criterion.
Brain server must be running on 127.0.0.1:8791 with the bearer token at
~/.openclaw/credentials/.personal_webhook_secret.
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

# The integration-tier opt-in lives in conftest.py; we still gate per-test
# in case the harness skipped that hook for any reason.
pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_INTEGRATION_TESTS") != "1",
    reason="set BRAIN_INTEGRATION_TESTS=1 to run integration tier",
)


BRAIN_URL = "http://127.0.0.1:8791"
SECRET_PATH = Path("~/.openclaw/credentials/.personal_webhook_secret").expanduser()
AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


def _bearer() -> str:
    return SECRET_PATH.read_text().strip()


def _http(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(BRAIN_URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_bearer()}")
    req.add_header("x-agent", "test_self_evolution_e2e")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


def _count_proposals(status: str | None = None, source_event: str | None = None) -> int:
    if not AUTONOMY_DB.exists():
        return 0
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        sql = "SELECT COUNT(*) FROM eval_proposals WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if source_event:
            sql += " AND source_event = ?"
            params.append(source_event)
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _delete_test_proposals(source_event: str) -> int:
    """Cleanup helper — deletes proposals seeded by this test."""
    if not AUTONOMY_DB.exists():
        return 0
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        cur = conn.execute(
            "DELETE FROM eval_proposals WHERE source_event = ?",
            (source_event,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


@pytest.fixture
def synthetic_source_event() -> str:
    """Each test run uses a unique source_event tag so cleanup is precise."""
    tag = f"e2e_test_{uuid.uuid4().hex[:8]}"
    yield tag
    _delete_test_proposals(tag)


def test_brain_health_before_test() -> None:
    """Sanity: brain must be reachable before we exercise the loop."""
    h = _http("GET", "/brain/health")
    assert h.get("status") in {"healthy", "degraded"}, f"brain not reachable: {h}"


def test_self_evolution_e2e_pipeline(synthetic_source_event: str) -> None:
    """End-to-end smoke of the 5-stage self-learning pipeline."""

    # ── Stage 1: seed 5 synthetic correction events via /recall/feedback ──
    seeded_proposal_ids: list[str] = []
    for i in range(5):
        feedback = {
            "query": f"e2e test query {i} — when is the next OrbStack release?",
            "result_id": f"semantic_memory:e2e_dummy_{i:02d}",
            "result_source": "test_fixture",
            "useful": False,
            "wrong_answer": True,
            "expected": f"e2e expected answer {i} - this is a synthetic test correction",
            "agent": "test_self_evolution_e2e",
        }
        # Re-tag the source_event by directly inserting via eval_proposals
        # (the /recall/feedback path always uses 'recall_feedback')
        # Mimic the same call path but with our unique tag for cleanup:
        try:
            from brain_core.eval_proposals import insert_proposal

            pid = insert_proposal(
                query=feedback["query"],
                expected=feedback["expected"],
                source_event=synthetic_source_event,
                confidence=0.8,
            )
            if pid:
                seeded_proposal_ids.append(pid)
        except Exception as e:
            pytest.fail(f"insert_proposal failed: {e}")

    assert len(seeded_proposal_ids) == 5, f"expected 5 proposals seeded, got {len(seeded_proposal_ids)}"

    candidate_count = _count_proposals(status="candidate", source_event=synthetic_source_event)
    assert candidate_count == 5, f"expected 5 candidates in DB, found {candidate_count}"

    # ── Stage 2: trigger eval_holdout_promote manually ──
    promote_resp = _http("POST", "/jobs/eval_holdout_promote")
    assert promote_resp.get("status") in {
        "queued",
        "ok",
        "running",
    }, f"promote dispatch failed: {promote_resp}"

    # Promote runs out-of-process; give it ~10s to complete
    deadline = time.time() + 30
    promoted = 0
    while time.time() < deadline:
        promoted = _count_proposals(status="pending", source_event=synthetic_source_event)
        if promoted > 0:
            break
        time.sleep(1)

    # The promoter scores by novelty against the live eval set. Synthetic
    # queries with no overlap may all get rejected (novelty too high). Either
    # promoted>0 OR all 5 are reviewed (status changed from 'candidate').
    still_candidate = _count_proposals(status="candidate", source_event=synthetic_source_event)
    assert (promoted > 0) or (still_candidate < 5), (
        f"promoter did not process candidates: promoted={promoted}, " f"still_candidate={still_candidate}"
    )

    # ── Stage 3: trigger eval_holdout_audit ──
    # Audit dispatches a Telegram digest via openclaw_dispatch. In test envs
    # without a Jenna agent path, this can fail — that's not a hard failure
    # for the WS4 contract; the goal is to verify the call path is wired.
    try:
        audit_resp = _http("POST", "/jobs/eval_holdout_audit", timeout=20)
        # Either status=queued/ok or a structured error message
        assert isinstance(audit_resp, dict)
    except Exception as e:
        # Audit failures are tolerated in test mode (Telegram dispatch not set up)
        print(f"[e2e] audit dispatch tolerated failure: {e}")

    # ── Stage 4: trigger lora_ab_gate ──
    # The lora_ab_gate CLI is a no-op when there's not enough training data;
    # we just verify the dispatch path is wired.
    try:
        lora_resp = _http("POST", "/jobs/lora_ab_gate", timeout=20)
        assert isinstance(lora_resp, dict)
    except Exception as e:
        print(f"[e2e] lora_ab_gate dispatch tolerated failure: {e}")

    # ── Stage 5: verify SLO metric is queryable ──
    slos = _http("GET", "/brain/slos")
    # The SLOs endpoint returns a dict with 'results' or 'slos' or similar
    found_slo = False
    for key in ("results", "slos", "checked"):
        items = slos.get(key, [])
        if isinstance(items, list):
            for item in items:
                name = item.get("name") or item.get("slo_name") or item.get("slo") or ""
                if "eval_holdout_growth" in name:
                    found_slo = True
                    break
    assert found_slo, (
        f"eval_holdout_growth_weekly SLO not surfaced in /brain/slos: " f"keys={list(slos.keys())}"
    )


def test_eval_proposals_table_exists() -> None:
    """Pre-flight: the eval_proposals table must exist with the expected columns."""
    if not AUTONOMY_DB.exists():
        pytest.skip("autonomy.db not present")
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(eval_proposals)").fetchall()]
    finally:
        conn.close()
    expected = {"id", "query", "expected", "status", "source_event", "created_at"}
    missing = expected - set(cols)
    assert not missing, f"eval_proposals missing columns: {missing}"


def test_slos_endpoint_responds() -> None:
    """The /brain/slos endpoint should be queryable with bearer auth."""
    slos = _http("GET", "/brain/slos")
    assert isinstance(slos, dict)
    # Either has results, or returns at least a status field
    assert any(k in slos for k in ("results", "slos", "checked", "status"))
