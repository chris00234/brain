"""Unit tests for the slowapi rate limiter wired into server.py (Phase M5).

Runs against a TestClient — does NOT require a running brain server.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT))


@pytest.fixture
def client(monkeypatch):
    """Spin up an isolated TestClient with a fresh slowapi limiter state.

    Sets BRAIN_RATE_LIMIT_DISABLED=0 to force the limiter on regardless of
    operator overrides. We re-import the server module so the limiter sees
    the env var on construction.
    """
    monkeypatch.setenv("BRAIN_RATE_LIMIT_DISABLED", "0")
    # Force a clean import so module-level limiter() picks up env var
    for mod in [
        m
        for m in list(sys.modules)
        if m.startswith("server") or m in {"rate_limit", "api_deps"} or m.startswith("routes.")
    ]:
        del sys.modules[mod]
    from fastapi.testclient import TestClient

    import server

    # Reset the limiter's in-memory storage between tests
    server.limiter.reset()
    return TestClient(server.app), server


def _bearer_headers():
    secret_path = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
    if not secret_path.exists():
        pytest.skip("bearer secret missing")
    return {"Authorization": f"Bearer {secret_path.read_text().strip()}"}


def test_limiter_module_level_state(client):
    _, server = client
    assert hasattr(server.app.state, "limiter")
    assert server.limiter.enabled is True


def test_limited_routes_registered(client):
    """The 5 rate-limited routes (Phase M5) are bound to the limiter with
    the expected limit strings. Per-route binding is the only contract the
    test can assert deterministically — the actual 429 dispatch needs a
    live server with working ChromaDB, which integration tests cover."""
    _, server = client
    routes = server.limiter._route_limits
    # M7-WS7 + M8 follow-up: /recall and /recall/v2 raised again from 600 →
    # 3000/min. Read path is non-LLM-billable (Ollama only) and back-to-back
    # eval runs (1212 calls/run on 606-query extended set) burst-throttled
    # under 600. Write paths stay tight at 10-30/min because they DO fire
    # billable LLM dispatches.
    expected = {
        "routes.recall.recall": "3000 per 1 minute",
        "routes.recall.recall_v2": "3000 per 1 minute",
        "routes.learn.learn_route": "10 per 1 minute",
        "routes.memory.create_memory": "30 per 1 minute",
        "routes.memory.create_memory_batch": "10 per 1 minute",
    }
    for route_name, expected_limit in expected.items():
        assert route_name in routes, f"{route_name} not registered with limiter"
        limits = routes[route_name]
        assert len(limits) >= 1
        # slowapi Limit objects carry .limit attribute → str of RateLimitItem
        actual = str(limits[0].limit)
        assert expected_limit in actual or actual.replace(" ", "") in expected_limit.replace(
            " ", ""
        ), f"{route_name}: expected {expected_limit!r}, got {actual!r}"


def test_disabled_env_var_skips_limiting(monkeypatch):
    """When BRAIN_RATE_LIMIT_DISABLED=1 the limiter is constructed in disabled mode."""
    monkeypatch.setenv("BRAIN_RATE_LIMIT_DISABLED", "1")
    for mod in [
        m
        for m in list(sys.modules)
        if m.startswith("server") or m in {"rate_limit", "api_deps"} or m.startswith("routes.")
    ]:
        del sys.modules[mod]
    import server

    assert server.limiter.enabled is False
