"""Unit tests for brain_core.eval_holdout_audit (Phase C2 Telegram digest)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    """Wire eval_holdout_audit at a tmp pending file with a mocked telegram dispatch."""
    if "eval_holdout_audit" in sys.modules:
        del sys.modules["eval_holdout_audit"]
    import eval_holdout_audit

    pending = tmp_path / "eval_holdout_pending.json"
    monkeypatch.setattr(eval_holdout_audit, "PENDING_PATH", pending)
    yield eval_holdout_audit, pending
    importlib.reload(eval_holdout_audit)


def test_run_no_pending_file(isolated_audit):
    audit, _ = isolated_audit
    result = audit.run()
    assert result["sent"] is False
    assert result["items"] == 0
    assert result["reason"] == "no pending file"


def test_run_handles_corrupt_pending(isolated_audit):
    audit, pending = isolated_audit
    pending.write_text("not valid json {")
    result = audit.run()
    assert result["sent"] is False
    assert "error" in result


def test_build_digest_empty(isolated_audit):
    audit, _ = isolated_audit
    text = audit._build_digest([])
    assert "No pending eval holdout candidates" in text


def test_build_digest_includes_approve_reject_urls(isolated_audit):
    audit, _ = isolated_audit
    items = [
        {
            "id": "prop_abc",
            "query": "what's the auth flow?",
            "expected": "bearer + JWT",
            "novelty": 0.82,
        },
        {
            "id": "prop_def",
            "query": "korean query test",
            "expected": "expected text",
            "novelty": 0.55,
        },
    ]
    text = audit._build_digest(items)
    assert "2 pending eval candidate" in text
    assert "prop_abc" in text
    assert "prop_def" in text
    assert "/brain/eval-proposals/prop_abc/approve" in text
    assert "/brain/eval-proposals/prop_abc/reject" in text
    assert "novelty 0.82" in text


def test_send_telegram_uses_direct_alert_module(isolated_audit, monkeypatch):
    audit, _ = isolated_audit
    calls = []

    monkeypatch.setitem(
        sys.modules,
        "telegram_alert",
        type(
            "_TelegramAlert",
            (),
            {
                "send_chris_telegram": staticmethod(
                    lambda message, source, severity: calls.append(
                        {"message": message, "source": source, "severity": severity}
                    )
                    or True
                )
            },
        ),
    )

    assert audit._send_telegram("review candidate") is True
    assert calls == [
        {
            "message": "review candidate",
            "source": "eval_holdout_audit",
            "severity": "info",
        }
    ]


def test_run_sends_when_pending_present(isolated_audit, monkeypatch):
    # Phase N3: audit only dispatches Telegram for candidates stuck >= 14 days.
    # The fresh-pending path is handled silently by auto_graduate. This test
    # stubs stuck_candidates so the candidate qualifies as "stuck" and we
    # still exercise the send path.
    audit, pending = isolated_audit
    pending.write_text(json.dumps([{"id": "prop_1", "query": "q1", "expected": "e1", "novelty": 0.7}]))

    sent_messages: list[str] = []

    def fake_send(message: str) -> bool:
        sent_messages.append(message)
        return True

    monkeypatch.setattr(audit, "_send_telegram", fake_send)
    # Force the candidate to look stuck so N3's >=14d gate opens
    import eval_holdout_promote as ehp

    monkeypatch.setattr(ehp, "stuck_candidates", lambda *a, **k: [{"candidate_id": "prop_1"}])
    result = audit.run()
    assert result["sent"] is True
    assert result["items"] == 1
    assert len(sent_messages) == 1
    assert "prop_1" in sent_messages[0]
