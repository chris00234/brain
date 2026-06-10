"""Contract tests for /recall/feedback side effects and the compound
feedback op → /recall/feedback payload mapping.

Pins two fixes from the 2026-06 audit:
  - compound feedback (target_type="recall") must forward wrong_answer and
    expected, otherwise the /recall/feedback eval auto-growth path
    (req.wrong_answer and req.expected → insert_proposal) can never fire
    for compound callers.
  - /recall/feedback best-effort side effects (reinforce_memory,
    insert_proposal) must log a warning on unexpected failure instead of
    silently swallowing it — the response stays "recorded" either way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


class _FakeLog:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args, **kw):
        self.warnings.append(msg % args if args else str(msg))


# ── compound feedback op → /recall/feedback mapping ──────────────────────


def _run_compound_feedback(monkeypatch, args: dict) -> list[tuple[str, str, dict]]:
    """Run brain_ops_compound with one recall-feedback op; capture loopback
    HTTP calls as (method, url, body) without real network or DB IO."""
    import urllib.request

    import atoms_store
    from routes import recall as R
    from starlette.requests import Request

    calls: list[tuple[str, str, dict]] = []

    class _Resp:
        def read(self):
            return b'{"status": "recorded"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else {}
        calls.append((req.get_method(), req.full_url, body))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(atoms_store, "insert_action_audit", lambda **kw: None)

    http_req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/brain/ops/compound",
            "headers": [],
            "query_string": b"",
        }
    )
    compound_req = R.CompoundRequest.model_validate(
        {"ops": [{"op": "feedback", "args": args}], "actor": "tester"}
    )
    fn = getattr(R.brain_ops_compound, "__wrapped__", R.brain_ops_compound)
    out = fn(http_req, compound_req)
    assert out["results"] and out["results"][0]["ok"], out
    return calls


def test_compound_feedback_recall_forwards_wrong_answer_and_expected(monkeypatch):
    calls = _run_compound_feedback(
        monkeypatch,
        {
            "target_id": "res1",
            "target_type": "recall",
            "success": False,
            "query": "what port does brain listen on",
            "result_source": "semantic_memory",
            "wrong_answer": True,
            "expected": "brain listens on port 8791",
        },
    )
    assert len(calls) == 1, calls
    method, url, body = calls[0]
    assert method == "POST"
    assert url.endswith("/recall/feedback")
    assert body["wrong_answer"] is True
    assert body["expected"] == "brain listens on port 8791"
    assert body["useful"] is False
    assert body["result_id"] == "res1"
    # The forwarded payload must still satisfy the endpoint schema.
    from recall_models import SearchFeedbackRequest

    SearchFeedbackRequest.model_validate(body)


def test_compound_feedback_recall_defaults_stay_schema_valid(monkeypatch):
    """Backward compat: callers that never send wrong_answer/expected get
    the schema defaults (False / ""), not a 422."""
    calls = _run_compound_feedback(
        monkeypatch,
        {"target_id": "res2", "target_type": "recall", "success": True},
    )
    _, _, body = calls[0]
    assert body["wrong_answer"] is False
    assert body["expected"] == ""
    from recall_models import SearchFeedbackRequest

    SearchFeedbackRequest.model_validate(body)


def test_compound_feedback_recall_truncates_expected_to_schema_limit(monkeypatch):
    """SearchFeedbackRequest caps expected at 2000 chars — the compound
    mapping must truncate, not forward an over-length value that 422s."""
    calls = _run_compound_feedback(
        monkeypatch,
        {
            "target_id": "res3",
            "target_type": "recall",
            "success": False,
            "wrong_answer": True,
            "expected": "x" * 3000,
        },
    )
    _, _, body = calls[0]
    assert len(body["expected"]) == 2000
    from recall_models import SearchFeedbackRequest

    SearchFeedbackRequest.model_validate(body)


# ── /recall/feedback best-effort side effects ─────────────────────────────


def _call_feedback(monkeypatch, tmp_path, **fields):
    from routes import recall as R

    monkeypatch.setattr(R, "BRAIN_DIR", tmp_path)
    fake_log = _FakeLog()
    monkeypatch.setattr(R, "log", fake_log)
    req = R.SearchFeedbackRequest.model_validate({"query": "q", "result_id": "rid", "useful": True, **fields})
    out = R.search_feedback(req)
    return out, fake_log


def test_feedback_reinforce_failure_logs_warning_and_still_records(monkeypatch, tmp_path):
    import entity_graph

    def _boom(result_id, success):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(entity_graph, "reinforce_memory", _boom)
    out, fake_log = _call_feedback(monkeypatch, tmp_path, result_source="semantic_memory")
    assert out == {"status": "recorded", "eval_proposal_id": None}
    assert any("reinforce_memory" in w for w in fake_log.warnings), fake_log.warnings
    # The feedback line itself was still written.
    assert (tmp_path / "logs" / "search-feedback.jsonl").exists()


def test_feedback_eval_proposal_failure_logs_warning_and_still_records(monkeypatch, tmp_path):
    import eval_proposals

    def _boom(**kw):
        raise RuntimeError("proposals db locked")

    monkeypatch.setattr(eval_proposals, "insert_proposal", _boom)
    out, fake_log = _call_feedback(monkeypatch, tmp_path, wrong_answer=True, expected="the right answer")
    assert out == {"status": "recorded", "eval_proposal_id": None}
    assert any("eval proposal" in w for w in fake_log.warnings), fake_log.warnings


def test_feedback_eval_proposal_success_returns_id_without_warning(monkeypatch, tmp_path):
    """Positive control: the happy path is unchanged by the warning fix."""
    import eval_proposals

    monkeypatch.setattr(eval_proposals, "insert_proposal", lambda **kw: "prop_123")
    out, fake_log = _call_feedback(monkeypatch, tmp_path, wrong_answer=True, expected="the right answer")
    assert out == {"status": "recorded", "eval_proposal_id": "prop_123"}
    assert fake_log.warnings == []
