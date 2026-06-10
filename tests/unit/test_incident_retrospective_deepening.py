"""Incident/retrospective query class → bounded candidate-pool deepening.

Contract 6: the authoritative incident_resolution/postmortem row often sits
just below the n=5 inner search window (a pool miss, not a ranking miss).
Queries in the closed failure-event class (EN+KO) join the existing
governance-sensitive deepening (bounded inner floor); ordinary blog/doc/
preference queries must NOT trigger it.
"""

from __future__ import annotations

import pytest
from recall_governance.query_analyzer import is_incident_retrospective_query
from starlette.requests import Request

INCIDENT_POSITIVES = [
    "Vite SPA route collision API prefix",
    "qdrant outage postmortem",
    "what was the root cause of the eval regression",
    "nginx crash incident resolution",
    "결제 모듈 장애 원인이 뭐였지?",
    "라우트 충돌 어떻게 해결했지?",
]

INCIDENT_NEGATIVES = [
    "building personal AI assistant blog",
    "docker compose tutorial",
    "brain UI design preferences",
    "what is Chris email address",
    "calendar and reminders tooling choice",
    "어떤 이메일을 6개월 보관해?",
]


@pytest.mark.parametrize("query", INCIDENT_POSITIVES)
def test_incident_class_positive(query):
    assert is_incident_retrospective_query(query) is True, query


@pytest.mark.parametrize("query", INCIDENT_NEGATIVES)
def test_incident_class_negative(query):
    assert is_incident_retrospective_query(query) is False, query


# ── Route level: incident queries deepen the inner search window ─────────


def _run_and_capture_limits(monkeypatch, query):
    import search_unified
    from routes import recall as R

    limits: list[int] = []

    def fake_search_all(q, limit, *, sources=None, **kw):
        limits.append(int(limit))
        return {
            "results": [
                {
                    "id": "r1",
                    "title": "incident_resolution",
                    "content": "Vite dev-server SPA fallback collided with the /api prefix; fixed via proxy.",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "score": 50.0,
                }
            ],
            "total_candidates": 1,
        }

    monkeypatch.setattr(search_unified, "search_all", fake_search_all)
    R._recall_cache.clear()
    fn = getattr(R.recall_v2, "__wrapped__", R.recall_v2)
    req = Request({"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""})
    fn(req, query, n=5, collection=None)
    return limits


def test_incident_query_deepens_inner_window(monkeypatch):
    limits = _run_and_capture_limits(monkeypatch, "Vite SPA route collision API prefix")
    assert limits, "search never ran"
    assert max(limits) >= 40, limits


def test_ordinary_doc_query_keeps_shallow_window(monkeypatch):
    """Negative control: a generic doc/blog query stays at the cheap default
    window — no blanket latency cost."""
    limits = _run_and_capture_limits(monkeypatch, "docker compose tutorial")
    assert limits, "search never ran"
    assert max(limits) <= 15, limits


def test_korean_incident_paraphrase_deepens_inner_window(monkeypatch):
    limits = _run_and_capture_limits(monkeypatch, "라우트 충돌 장애 원인 알려줘")
    assert limits, "search never ran"
    assert max(limits) >= 40, limits
