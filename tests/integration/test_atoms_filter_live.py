"""Integration test: BRAIN_ATOMS_READ filter actually filters /recall/v2 results.

Requires a running brain server with BRAIN_ATOMS_ENABLED=true and BRAIN_ATOMS_READ=true.
Skipped by default. Run with: BRAIN_INTEGRATION_TESTS=1 pytest tests/integration/test_atoms_filter_live.py

Phase H1 — Brain v2 production hardening.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = Path("/Users/chrischo/.brain/credentials/.personal_webhook_secret")
BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")


def _token() -> str:
    if not SECRET_FILE.exists():
        pytest.skip(f"secret file missing: {SECRET_FILE}")
    return SECRET_FILE.read_text().strip()


def _brain_get(path: str) -> dict:
    req = urllib.request.Request(f"{BRAIN_URL}{path}")
    req.add_header("Authorization", f"Bearer {_token()}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _brain_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BRAIN_URL}{path}",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def test_brain_health_ok():
    health = _brain_get("/brain/health")
    assert health["status"] == "healthy", f"brain status not healthy: {health.get('alerts')}"
    assert health["alerts"] == []


def test_atoms_filter_excludes_obsolete():
    """If BRAIN_ATOMS_READ=true, atoms tier='obsolete' should be filtered out of /recall/v2."""
    if not BRAIN_DB.exists():
        pytest.skip("brain.db missing — atoms layer not deployed")

    # Find an atom with tier='obsolete' to use as a probe target
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        row = conn.execute("SELECT chroma_id, text FROM atoms WHERE tier='obsolete' LIMIT 1").fetchone()
    finally:
        conn.close()

    if not row:
        pytest.skip("no obsolete atoms to probe — filter behavior cannot be verified")

    chroma_id, text = row
    # Query for the obsolete atom's content via /recall/v2
    query = (text or "")[:80].strip()
    if not query:
        pytest.skip("obsolete atom has no queryable text")

    response = _brain_get(f"/recall/v2?q={urllib.parse.quote_plus(query)}&n=20")
    result_ids = [r.get("id") for r in response.get("results", []) if isinstance(r, dict)]

    assert chroma_id not in result_ids, (
        f"Obsolete atom {chroma_id} appeared in /recall/v2 results — filter not working. "
        f"results={result_ids[:5]}"
    )


def test_recall_v2_latency_within_slo():
    """Verify /recall/v2 p95 stays under the production SLO (1000ms)."""
    metrics = _brain_get("/metrics")
    routes = metrics.get("routes", {})
    recall_v2 = routes.get("/recall/v2", {})
    p95 = recall_v2.get("p95_ms")
    if p95 is None or recall_v2.get("count", 0) < 5:
        pytest.skip("not enough /recall/v2 samples to assert latency")
    assert p95 <= 1000, f"/recall/v2 p95 too high: {p95}ms"


def test_atoms_stats_reachable():
    stats = _brain_get("/brain/atoms/stats")
    assert stats.get("enabled") == 1
    assert stats.get("atoms_total", 0) > 0


def test_breakers_endpoint_reachable():
    response = _brain_get("/brain/breakers")
    assert "breakers" in response


def test_slos_endpoint_reachable():
    response = _brain_get("/brain/slos")
    # 2026-04-16 — SLO count floor, not equality. Growth is expected as
    # new watchers land (11 as of Tier 1/3 ship); test prevents accidental
    # removals but allows additions.
    assert response["checked"] >= 6
    assert "results" in response or "items" in response


def test_autonomy_levels_reachable():
    response = _brain_get("/brain/autonomy")
    assert "levels" in response
    assert response["levels"].get("heal.log_rotate") == "L3"
    assert response["levels"].get("write.canonical") == "L0"
