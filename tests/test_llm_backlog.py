"""tests/test_llm_backlog.py — unit tests for the LLM catch-up queue.

Verifies enqueue dedupe, drain handler dispatch, TTL abandonment, and the
breaker-aware abort path. Uses in-memory sqlite so tests don't touch the
real autonomy.db.

Run:
  .venv/bin/python -m pytest tests/test_llm_backlog.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

import llm_backlog


@pytest.fixture(autouse=True)
def _redirect_autonomy_db(tmp_path, monkeypatch):
    """Point llm_backlog writes at a temp sqlite so tests don't pollute the
    real autonomy.db. Also reset the module-level schema flag so the new
    temp DB gets the DDL applied."""
    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(llm_backlog, "AUTONOMY_DB", fake_db)
    llm_backlog._schema_ready = False
    llm_backlog._schema_lock = None
    # Drop any previously registered handlers so each test gets a clean slate
    llm_backlog._handlers.clear()
    llm_backlog._handlers_wired = False
    yield


# ── Enqueue + dedupe ──────────────────────────────────────


def test_enqueue_returns_id_and_dedupes():
    rid1 = llm_backlog.enqueue("classify", {"content": "hello", "atom_id": "a1"})
    rid2 = llm_backlog.enqueue("classify", {"content": "hello", "atom_id": "a1"})
    assert rid1 == rid2, "dedupe should return same row id"
    assert llm_backlog.pending_count() == 1


def test_enqueue_invalid_kind_returns_none():
    assert llm_backlog.enqueue("garbage_kind", {}) is None
    assert llm_backlog.pending_count() == 0


def test_enqueue_different_kinds_distinct():
    llm_backlog.enqueue("classify", {"content": "x"})
    llm_backlog.enqueue("entities", {"text": "x", "chroma_id": "c1"})
    llm_backlog.enqueue("telegram", {"body": "alert"})
    assert llm_backlog.pending_count() == 3


# ── Drain with mock handlers ──────────────────────────────


def test_drain_calls_registered_handler():
    calls = []

    def _handler(payload):
        calls.append(payload)
        return True

    llm_backlog.register_handler("classify", _handler)
    llm_backlog._handlers_wired = True  # skip default wiring
    llm_backlog.enqueue("classify", {"content": "test1"})
    llm_backlog.enqueue("classify", {"content": "test2"})

    result = llm_backlog.drain(abort_on_breaker=False)
    assert result["drained"] == 2
    assert len(calls) == 2
    assert llm_backlog.pending_count() == 0


def test_drain_increments_retry_on_failure():
    def _failing(_payload):
        return False

    llm_backlog.register_handler("classify", _failing)
    llm_backlog._handlers_wired = True
    llm_backlog.enqueue("classify", {"content": "retry_me"})

    for _ in range(3):
        llm_backlog.drain(abort_on_breaker=False)
        # Still pending after 1-3 failures
    assert llm_backlog.pending_count() == 1

    # Fourth and fifth retries should drive it to failed
    llm_backlog.drain(abort_on_breaker=False)
    llm_backlog.drain(abort_on_breaker=False)
    assert llm_backlog.pending_count() == 0  # moved to failed


def test_drain_aborts_when_breaker_open(monkeypatch):
    def _always_open():
        return True

    monkeypatch.setattr(llm_backlog, "_breaker_open", _always_open)

    def _should_not_run(_payload):
        raise AssertionError("handler should not be called when breaker open")

    llm_backlog.register_handler("classify", _should_not_run)
    llm_backlog._handlers_wired = True
    llm_backlog.enqueue("classify", {"content": "x"})

    result = llm_backlog.drain(abort_on_breaker=True)
    assert result["status"] == "skipped_breaker"
    assert result["drained"] == 0
    assert llm_backlog.pending_count() == 1


def test_drain_ignores_breaker_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(llm_backlog, "_breaker_open", lambda: True)

    calls = []

    def _handler(payload):
        calls.append(payload)
        return True

    llm_backlog.register_handler("classify", _handler)
    llm_backlog._handlers_wired = True
    llm_backlog.enqueue("classify", {"content": "force"})

    result = llm_backlog.drain(abort_on_breaker=False)
    assert result["drained"] == 1
    assert len(calls) == 1


# ── TTL abandonment ───────────────────────────────────────


def test_drain_abandons_past_ttl_entries(monkeypatch):
    # Craft a telegram entry backdated past the 6h TTL
    rid = llm_backlog.enqueue("telegram", {"body": "stale"})
    assert rid is not None

    import sqlite3

    old_ts = "2020-01-01T00:00:00+00:00"
    with sqlite3.connect(str(llm_backlog.AUTONOMY_DB)) as conn:
        conn.execute(
            "UPDATE llm_backlog SET created_at=? WHERE id=?",
            (old_ts, rid),
        )
        conn.commit()

    called = []

    def _handler(_payload):
        called.append(True)
        return True

    llm_backlog.register_handler("telegram", _handler)
    llm_backlog._handlers_wired = True

    result = llm_backlog.drain(abort_on_breaker=False)
    assert result["abandoned"] >= 1
    assert called == [], "abandoned entries should not invoke handler"
    assert llm_backlog.pending_count() == 0


def test_drain_abandons_stale_session_summary_distill():
    rid = llm_backlog.enqueue("distill", {"purpose": "session_summary", "prompt": "summarize me"})
    assert rid is not None

    import sqlite3

    old_ts = "2020-01-01T00:00:00+00:00"
    with sqlite3.connect(str(llm_backlog.AUTONOMY_DB)) as conn:
        conn.execute("UPDATE llm_backlog SET created_at=? WHERE id=?", (old_ts, rid))
        conn.commit()

    called = []
    llm_backlog.register_handler("distill", lambda payload: called.append(payload) or True)
    llm_backlog._handlers_wired = True

    result = llm_backlog.drain(abort_on_breaker=False)

    assert result["abandoned"] >= 1
    assert called == []
    assert llm_backlog.pending_count() == 0


def test_drain_bounds_large_prompt_before_handler(monkeypatch):
    monkeypatch.setattr(llm_backlog, "MAX_PROMPT_CHARS", 100)
    monkeypatch.setattr(llm_backlog, "PROMPT_HEAD_CHARS", 30)
    captured = []
    huge_prompt = "a" * 80 + "TAIL" * 30

    def _handler(payload):
        captured.append(payload)
        return True

    llm_backlog.register_handler("distill", _handler)
    llm_backlog._handlers_wired = True
    llm_backlog.enqueue("distill", {"purpose": "other", "prompt": huge_prompt, "timeout": 90})

    result = llm_backlog.drain(abort_on_breaker=False, wall_time_s=60)

    assert result["drained"] == 1
    assert len(captured[0]["prompt"]) < len(huge_prompt)
    assert "backlog prompt truncated" in captured[0]["prompt"]
    assert captured[0]["prompt"].endswith("TAIL" * 17)
    assert captured[0]["max_backends"] == llm_backlog.DEFAULT_MAX_BACKENDS


def test_default_telegram_handler_uses_direct_alert_not_llm(monkeypatch):
    sent = []

    def _send_chris_telegram(body, source, severity, *, bypass_rate_limit, queue_on_failure):
        sent.append(
            {
                "body": body,
                "source": source,
                "severity": severity,
                "bypass_rate_limit": bypass_rate_limit,
                "queue_on_failure": queue_on_failure,
            }
        )
        return True

    def _llm_dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("telegram backlog replay must not use cli_llm.dispatch")

    monkeypatch.setitem(
        sys.modules,
        "telegram_alert",
        SimpleNamespace(send_chris_telegram=_send_chris_telegram),
    )
    monkeypatch.setitem(
        sys.modules,
        "cli_llm",
        SimpleNamespace(dispatch=_llm_dispatch_should_not_run),
    )

    llm_backlog.enqueue(
        "telegram",
        {"body": "alert", "source": "brain_loop", "severity": "urgent"},
    )

    result = llm_backlog.drain(abort_on_breaker=False)

    assert result["drained"] == 1
    assert sent == [
        {
            "body": "[DELAYED URGENT] alert",
            "source": "brain_loop",
            "severity": "urgent",
            "bypass_rate_limit": True,
            "queue_on_failure": False,
        }
    ]


# ── Stats + helpers ───────────────────────────────────────


def test_pending_count_and_oldest_age():
    llm_backlog.enqueue("classify", {"content": "a"})
    llm_backlog.enqueue("entities", {"text": "b", "chroma_id": "c1"})
    assert llm_backlog.pending_count() == 2
    assert llm_backlog.oldest_pending_age_seconds() >= 0


def test_stats_groups_by_kind_and_status():
    llm_backlog.enqueue("classify", {"content": "x"})
    llm_backlog.enqueue("entities", {"text": "y", "chroma_id": "c"})
    s = llm_backlog.stats()
    assert "classify" in s
    assert "entities" in s
    assert s["_totals"]["pending"] == 2


def test_run_entry_point_returns_stats():
    result = llm_backlog.run()
    assert "status" in result
    assert "pending_after" in result
    assert "oldest_age_s" in result


# ── Dedup by stable content_hash ─────────────────────────


def test_content_hash_stable_regardless_of_payload_key_order():
    # Same payload with different key order should dedupe
    rid1 = llm_backlog.enqueue("classify", {"a": 1, "b": 2, "c": 3})
    rid2 = llm_backlog.enqueue("classify", {"c": 3, "a": 1, "b": 2})
    assert rid1 == rid2
    assert llm_backlog.pending_count() == 1
