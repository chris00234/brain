"""Unit tests for brain_loop's self-modification dispatcher.

Pins the byte-equal contract of _apply_self_modification and its four
per-handler helpers extracted on 2026-05-12:
  - _self_mod_drain_llm_backlog
  - _self_mod_engage_cost_governor
  - _self_mod_incremental_canonical_index
  - _self_mod_write_proposal
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── _self_mod_drain_llm_backlog ──────────────────────────────────────


def test_drain_llm_backlog_success(monkeypatch):
    """Happy path: llm_backlog.drain returns a counts dict, helper logs
    and returns True."""
    from brain_loop import _self_mod_drain_llm_backlog

    stub = types.ModuleType("llm_backlog")
    stub.drain = lambda limit, abort_on_breaker: {"drained": 7, "failed": 1, "abandoned": 0}
    monkeypatch.setitem(sys.modules, "llm_backlog", stub)

    assert _self_mod_drain_llm_backlog({"modification": "drain_llm_backlog"}) is True


def test_drain_llm_backlog_import_failure(monkeypatch):
    """If llm_backlog cannot be imported, return False without raising."""
    from brain_loop import _self_mod_drain_llm_backlog

    monkeypatch.setitem(sys.modules, "llm_backlog", None)
    assert _self_mod_drain_llm_backlog({"modification": "drain_llm_backlog"}) is False


def test_drain_llm_backlog_runtime_failure(monkeypatch):
    """A drain() exception is swallowed → False."""
    from brain_loop import _self_mod_drain_llm_backlog

    stub = types.ModuleType("llm_backlog")

    def _boom(**kw):
        raise RuntimeError("backlog db down")

    stub.drain = _boom
    monkeypatch.setitem(sys.modules, "llm_backlog", stub)

    assert _self_mod_drain_llm_backlog({"modification": "drain_llm_backlog"}) is False


def test_drain_llm_backlog_passes_caps(monkeypatch):
    """drain() must be called with limit=100 and abort_on_breaker=True
    (event-driven catch-up, not unbounded)."""
    from brain_loop import _self_mod_drain_llm_backlog

    captured: list = []
    stub = types.ModuleType("llm_backlog")

    def _spy(limit, abort_on_breaker):
        captured.append((limit, abort_on_breaker))
        return {"drained": 0, "failed": 0, "abandoned": 0}

    stub.drain = _spy
    monkeypatch.setitem(sys.modules, "llm_backlog", stub)

    _self_mod_drain_llm_backlog({})
    assert captured == [(100, True)]


# ── _self_mod_engage_cost_governor ───────────────────────────────────


class _FakeConfigStore:
    """Captures all set() calls; get() returns whatever the test seeds."""

    def __init__(self, get_map: dict | None = None):
        self.set_calls: list[tuple[str, str, str]] = []
        self.get_map = get_map or {}

    def set(self, key, value, *, updated_by):
        self.set_calls.append((key, value, updated_by))

    def get(self, key):
        return self.get_map.get(key)


def _install_config_store(monkeypatch, store: _FakeConfigStore):
    stub = types.ModuleType("brain_config_store")
    stub.set = store.set
    stub.get = store.get
    monkeypatch.setitem(sys.modules, "brain_config_store", stub)


def test_cost_governor_uses_payload_concurrency(monkeypatch):
    """payload['concurrency'] wins over env and config_store."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore()
    _install_config_store(monkeypatch, store)
    monkeypatch.delenv("BRAIN_CLI_LLM_COST_GOVERNOR_CONCURRENCY", raising=False)

    payload = {"concurrency": 3, "ttl_s": 600, "ratio": 0.8, "hourly": 100, "baseline": 50}
    assert _self_mod_engage_cost_governor(payload) is True

    keys_set = {k for (k, _v, _ub) in store.set_calls}
    assert keys_set == {"BRAIN_CLI_LLM_CONCURRENCY", "BRAIN_CLI_LLM_CONCURRENCY_UNTIL"}
    # Cap clamps to [1,4], so 3 → "3"
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "3"
    assert cap_call[2] == "brain_loop.cost_governor"


def test_cost_governor_falls_back_to_env(monkeypatch):
    """When payload.concurrency is None, the env var is consulted next."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore()
    _install_config_store(monkeypatch, store)
    monkeypatch.setenv("BRAIN_CLI_LLM_COST_GOVERNOR_CONCURRENCY", "2")

    _self_mod_engage_cost_governor({})
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "2"


def test_cost_governor_falls_back_to_config_store(monkeypatch):
    """When payload + env both miss, brain_config_store.get is the last
    resort before defaulting to 1."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore(get_map={"BRAIN_CLI_LLM_COST_GOVERNOR_CONCURRENCY": "4"})
    _install_config_store(monkeypatch, store)
    monkeypatch.delenv("BRAIN_CLI_LLM_COST_GOVERNOR_CONCURRENCY", raising=False)

    _self_mod_engage_cost_governor({})
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "4"


def test_cost_governor_default_is_one(monkeypatch):
    """All sources empty → cap defaults to 1."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore()
    _install_config_store(monkeypatch, store)
    monkeypatch.delenv("BRAIN_CLI_LLM_COST_GOVERNOR_CONCURRENCY", raising=False)

    _self_mod_engage_cost_governor({})
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "1"


def test_cost_governor_clamps_to_4_max(monkeypatch):
    """Even when caller passes concurrency=99, helper clamps to 4."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore()
    _install_config_store(monkeypatch, store)

    _self_mod_engage_cost_governor({"concurrency": 99})
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "4"


def test_cost_governor_clamps_to_1_min(monkeypatch):
    """Floor is 1 — concurrency=0 or negative becomes 1."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore()
    _install_config_store(monkeypatch, store)

    _self_mod_engage_cost_governor({"concurrency": 0})
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "1"


def test_cost_governor_invalid_raw_falls_back_to_one(monkeypatch):
    """TypeError/ValueError parsing the raw → cap defaults to 1, not crash."""
    from brain_loop import _self_mod_engage_cost_governor

    store = _FakeConfigStore()
    _install_config_store(monkeypatch, store)

    assert _self_mod_engage_cost_governor({"concurrency": "not_a_number"}) is True
    cap_call = next(c for c in store.set_calls if c[0] == "BRAIN_CLI_LLM_CONCURRENCY")
    assert cap_call[1] == "1"


def test_cost_governor_failure_swallowed(monkeypatch):
    """If brain_config_store.set raises, helper returns False not raises."""
    from brain_loop import _self_mod_engage_cost_governor

    stub = types.ModuleType("brain_config_store")

    def _boom(*a, **k):
        raise RuntimeError("config db down")

    stub.set = _boom
    stub.get = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "brain_config_store", stub)

    assert _self_mod_engage_cost_governor({}) is False


# ── _self_mod_incremental_canonical_index ────────────────────────────


def test_incremental_canonical_index_filters_doc_types(monkeypatch):
    """collect_canonical returns mixed types; helper must split into
    canonical-note and distilled-note before calling add_documents."""
    import brain_loop

    captured: list = []

    stub = types.ModuleType("indexer")
    stub.ensure_collection = lambda name: None

    def _add(name, docs, **kw):
        captured.append((name, list(docs), kw))
        return len(docs)

    stub.add_documents = _add
    stub.collect_canonical = lambda: [
        {"type": "canonical-note", "id": "c1"},
        {"type": "distilled-note", "id": "d1"},
        {"type": "canonical-note", "id": "c2"},
        {"type": "obsidian-note", "id": "o1"},  # skipped
    ]
    monkeypatch.setitem(sys.modules, "indexer", stub)
    monkeypatch.setattr(brain_loop, "_set_incremental_last_ts", lambda ts: None)

    assert brain_loop._self_mod_incremental_canonical_index({}) is True

    canonical_call = next(c for c in captured if c[0] == "canonical")
    distilled_call = next(c for c in captured if c[0] == "distilled")
    assert [d["id"] for d in canonical_call[1]] == ["c1", "c2"]
    assert [d["id"] for d in distilled_call[1]] == ["d1"]
    # add_documents must be invoked with skip_stale_cleanup=True + force_incremental=True
    assert canonical_call[2]["skip_stale_cleanup"] is True
    assert canonical_call[2]["force_incremental"] is True


def test_incremental_canonical_index_records_last_ts(monkeypatch):
    """_set_incremental_last_ts is called with a recent epoch on success."""
    import time

    import brain_loop

    stub = types.ModuleType("indexer")
    stub.ensure_collection = lambda name: None
    stub.add_documents = lambda *a, **k: 0
    stub.collect_canonical = lambda: []
    monkeypatch.setitem(sys.modules, "indexer", stub)

    seen: list = []
    monkeypatch.setattr(brain_loop, "_set_incremental_last_ts", lambda ts: seen.append(ts))

    before = time.time()
    brain_loop._self_mod_incremental_canonical_index({})
    after = time.time()
    assert len(seen) == 1
    assert before <= seen[0] <= after + 0.5


def test_incremental_canonical_index_failure_returns_false(monkeypatch):
    """A runtime error inside indexer must not propagate — returns False."""
    import brain_loop

    stub = types.ModuleType("indexer")
    stub.ensure_collection = lambda name: None

    def _boom(*a, **k):
        raise RuntimeError("qdrant unreachable")

    stub.add_documents = _boom
    stub.collect_canonical = lambda: [{"type": "canonical-note"}]
    monkeypatch.setitem(sys.modules, "indexer", stub)
    monkeypatch.setattr(brain_loop, "_set_incremental_last_ts", lambda ts: None)

    assert brain_loop._self_mod_incremental_canonical_index({}) is False


# ── _self_mod_write_proposal ─────────────────────────────────────────


@pytest.fixture
def _isolated_autonomy_conn(monkeypatch):
    """Patch _connect_autonomy to return an in-memory sqlite3 connection
    with the eval_proposals table created. Yields the connection so tests
    can read what got written."""
    import sqlite3

    import brain_loop

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE eval_proposals ("
        " id TEXT PRIMARY KEY, query TEXT, expected TEXT, expected_sources TEXT,"
        " source_event TEXT, status TEXT, confidence REAL, created_at TEXT)"
    )

    class _CtxConn:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(brain_loop, "_connect_autonomy", lambda: _CtxConn())
    yield conn
    conn.close()


def test_write_proposal_writes_candidate_row(_isolated_autonomy_conn):
    """Default modification → candidate row in eval_proposals."""
    from brain_loop import _self_mod_write_proposal

    payload = {
        "modification": "experimental_thing",
        "domain": "infra",
        "confidence": 0.55,
    }
    assert _self_mod_write_proposal(payload) is True

    row = _isolated_autonomy_conn.execute(
        "SELECT query, status, confidence, source_event FROM eval_proposals"
    ).fetchone()
    assert row is not None
    query, status, confidence, source_event = row
    assert query == "self_modify:experimental_thing:infra"
    assert status == "candidate"
    assert confidence == 0.55
    assert source_event == "brain_loop_self_modify"


def test_write_proposal_default_confidence(_isolated_autonomy_conn):
    """When payload.confidence is absent, default 0.7 is used."""
    from brain_loop import _self_mod_write_proposal

    _self_mod_write_proposal({"modification": "x"})
    row = _isolated_autonomy_conn.execute("SELECT confidence FROM eval_proposals").fetchone()
    assert row[0] == 0.7


def test_write_proposal_falls_back_to_subject_when_domain_missing(_isolated_autonomy_conn):
    """If payload has no 'domain', the query suffix falls back to
    payload['subject'] (then empty)."""
    from brain_loop import _self_mod_write_proposal

    _self_mod_write_proposal({"modification": "x", "subject": "openclaw"})
    row = _isolated_autonomy_conn.execute("SELECT query FROM eval_proposals").fetchone()
    assert row[0] == "self_modify:x:openclaw"


def test_write_proposal_handles_sqlite_error(monkeypatch):
    """sqlite3.Error during insert returns False instead of raising."""
    import sqlite3

    import brain_loop
    from brain_loop import _self_mod_write_proposal

    class _BadConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def execute(self, *a, **k):
            raise sqlite3.Error("disk full")

        def commit(self):
            pass

    monkeypatch.setattr(brain_loop, "_connect_autonomy", lambda: _BadConn())
    assert _self_mod_write_proposal({"modification": "x"}) is False


# ── _apply_self_modification (dispatcher) ────────────────────────────


def test_dispatcher_routes_drain_llm_backlog(monkeypatch):
    import brain_loop

    seen: list = []
    monkeypatch.setattr(
        brain_loop,
        "_SELF_MOD_HANDLERS",
        {
            "drain_llm_backlog": lambda p: seen.append(("drain", p)) or True,
            "engage_llm_cost_governor": lambda p: seen.append(("gov", p)) or True,
            "incremental_canonical_index": lambda p: seen.append(("inc", p)) or True,
        },
    )
    monkeypatch.setattr(brain_loop, "_self_mod_write_proposal", lambda p: seen.append(("prop", p)) or True)

    assert brain_loop._apply_self_modification({"modification": "drain_llm_backlog", "x": 1}) is True
    assert seen == [("drain", {"modification": "drain_llm_backlog", "x": 1})]


def test_dispatcher_routes_engage_cost_governor(monkeypatch):
    import brain_loop

    seen: list = []
    monkeypatch.setattr(
        brain_loop,
        "_SELF_MOD_HANDLERS",
        {
            "drain_llm_backlog": lambda p: seen.append(("drain", p)) or True,
            "engage_llm_cost_governor": lambda p: seen.append(("gov", p)) or True,
        },
    )
    monkeypatch.setattr(brain_loop, "_self_mod_write_proposal", lambda p: seen.append(("prop", p)) or False)

    brain_loop._apply_self_modification({"modification": "engage_llm_cost_governor"})
    assert seen[0][0] == "gov"


def test_dispatcher_routes_incremental_canonical_index(monkeypatch):
    import brain_loop

    seen: list = []
    monkeypatch.setattr(
        brain_loop,
        "_SELF_MOD_HANDLERS",
        {"incremental_canonical_index": lambda p: seen.append(("inc", p)) or True},
    )

    brain_loop._apply_self_modification({"modification": "incremental_canonical_index"})
    assert seen[0][0] == "inc"


def test_dispatcher_unknown_modification_writes_proposal(monkeypatch):
    """An unknown modification key falls through to _self_mod_write_proposal."""
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_SELF_MOD_HANDLERS", {})
    monkeypatch.setattr(brain_loop, "_self_mod_write_proposal", lambda p: captured.append(p) or True)

    brain_loop._apply_self_modification({"modification": "exotic_kind", "data": 1})
    assert captured == [{"modification": "exotic_kind", "data": 1}]


def test_dispatcher_no_modification_key_writes_proposal(monkeypatch):
    """payload missing 'modification' → dispatcher defaults to 'unknown'
    which is not in the handler table → falls through to write_proposal."""
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_SELF_MOD_HANDLERS", {})
    monkeypatch.setattr(brain_loop, "_self_mod_write_proposal", lambda p: captured.append(p) or True)

    brain_loop._apply_self_modification({"data": 99})
    assert captured == [{"data": 99}]


# ── _execute_decision_action + _write_decision_audit (from _act split) ─


def _make_decision(kind, payload=None, obs_kind="stalled_goal", obs_subject="goal_1"):
    """Build a Decision with a minimal Observation."""
    from brain_loop import Decision, Observation

    obs = Observation(kind=obs_kind, subject=obs_subject)
    return Decision(observation=obs, kind=kind, action_payload=payload or {})


def test_execute_observe_only():
    import brain_loop

    d = _make_decision(brain_loop.DecisionKind.OBSERVE_ONLY)
    assert brain_loop._execute_decision_action(d) == {"status": "observed"}


def test_execute_propose_success(monkeypatch):
    import brain_loop

    calls: list = []
    monkeypatch.setattr(brain_loop, "_write_eval_proposal", lambda p: calls.append(p) or True)

    d = _make_decision(brain_loop.DecisionKind.PROPOSE, {"x": 1})
    assert brain_loop._execute_decision_action(d) == {"status": "proposed"}
    assert calls == [{"x": 1}]


def test_execute_propose_failure(monkeypatch):
    import brain_loop

    monkeypatch.setattr(brain_loop, "_write_eval_proposal", lambda p: False)

    d = _make_decision(brain_loop.DecisionKind.PROPOSE, {"x": 1})
    assert brain_loop._execute_decision_action(d) == {"status": "propose_failed"}


def test_execute_dispatch_agent_uses_defaults(monkeypatch):
    """When payload omits agent/message, defaults are agent='jenna', message=''."""
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_dispatch_agent", lambda a, m: captured.append((a, m)) or True)

    d = _make_decision(brain_loop.DecisionKind.DISPATCH_AGENT, {})
    assert brain_loop._execute_decision_action(d) == {"status": "dispatched"}
    assert captured == [("jenna", "")]


def test_execute_dispatch_agent_passes_explicit_args(monkeypatch):
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_dispatch_agent", lambda a, m: captured.append((a, m)) or True)

    d = _make_decision(
        brain_loop.DecisionKind.DISPATCH_AGENT,
        {"agent": "ellie", "message": "check infra"},
    )
    brain_loop._execute_decision_action(d)
    assert captured == [("ellie", "check infra")]


def test_execute_dispatch_agent_failure(monkeypatch):
    import brain_loop

    monkeypatch.setattr(brain_loop, "_dispatch_agent", lambda a, m: False)
    d = _make_decision(brain_loop.DecisionKind.DISPATCH_AGENT, {"agent": "x", "message": "y"})
    assert brain_loop._execute_decision_action(d) == {"status": "dispatch_failed"}


def test_execute_push_to_claude_defaults(monkeypatch):
    """Doorbell args fall back: session='', title='', content='',
    priority='medium', source='brain_loop'."""
    import brain_loop

    captured: list = []
    monkeypatch.setattr(
        brain_loop,
        "_write_doorbell",
        lambda sid, title, content, prio, src: captured.append((sid, title, content, prio, src)) or True,
    )

    d = _make_decision(brain_loop.DecisionKind.PUSH_TO_CLAUDE, {})
    assert brain_loop._execute_decision_action(d) == {"status": "doorbell_written"}
    assert captured == [("", "", "", "medium", "brain_loop")]


def test_execute_push_to_claude_full_payload(monkeypatch):
    import brain_loop

    captured: list = []
    monkeypatch.setattr(
        brain_loop,
        "_write_doorbell",
        lambda sid, title, content, prio, src: captured.append((sid, title, content, prio, src)) or True,
    )

    payload = {
        "session_id": "sess_abc",
        "title": "T",
        "content": "C",
        "priority": "high",
        "source": "brain_loop.goal_monitor",
    }
    d = _make_decision(brain_loop.DecisionKind.PUSH_TO_CLAUDE, payload)
    brain_loop._execute_decision_action(d)
    assert captured == [("sess_abc", "T", "C", "high", "brain_loop.goal_monitor")]


def test_execute_push_to_claude_failure(monkeypatch):
    import brain_loop

    monkeypatch.setattr(brain_loop, "_write_doorbell", lambda *a, **k: False)
    d = _make_decision(brain_loop.DecisionKind.PUSH_TO_CLAUDE, {})
    assert brain_loop._execute_decision_action(d) == {"status": "doorbell_failed"}


def test_execute_telegram_alert(monkeypatch):
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_telegram_alert", lambda body: captured.append(body) or True)

    d = _make_decision(brain_loop.DecisionKind.TELEGRAM_ALERT, {"body": "ALERT"})
    assert brain_loop._execute_decision_action(d) == {"status": "telegram_sent"}
    assert captured == ["ALERT"]


def test_execute_telegram_alert_missing_body(monkeypatch):
    """Missing body → empty string passed."""
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_telegram_alert", lambda body: captured.append(body) or True)

    d = _make_decision(brain_loop.DecisionKind.TELEGRAM_ALERT, {})
    brain_loop._execute_decision_action(d)
    assert captured == [""]


def test_execute_telegram_alert_failure(monkeypatch):
    import brain_loop

    monkeypatch.setattr(brain_loop, "_telegram_alert", lambda body: False)
    d = _make_decision(brain_loop.DecisionKind.TELEGRAM_ALERT, {"body": "x"})
    assert brain_loop._execute_decision_action(d) == {"status": "telegram_failed"}


def test_execute_self_modify(monkeypatch):
    import brain_loop

    captured: list = []
    monkeypatch.setattr(brain_loop, "_apply_self_modification", lambda p: captured.append(p) or True)

    payload = {"modification": "drain_llm_backlog"}
    d = _make_decision(brain_loop.DecisionKind.SELF_MODIFY, payload)
    assert brain_loop._execute_decision_action(d) == {"status": "self_mod_queued"}
    assert captured == [payload]


def test_execute_self_modify_failure(monkeypatch):
    import brain_loop

    monkeypatch.setattr(brain_loop, "_apply_self_modification", lambda p: False)
    d = _make_decision(brain_loop.DecisionKind.SELF_MODIFY, {})
    assert brain_loop._execute_decision_action(d) == {"status": "self_mod_failed"}


# ── _write_decision_audit ────────────────────────────────────────────


def test_write_decision_audit_invokes_insert(monkeypatch):
    """Happy path: _insert_action_audit is called with the route formed
    from kind.value and a 2000-char-capped query_text from
    `<observation.kind>:<observation.subject>`."""
    import brain_loop

    captured: dict = {}
    monkeypatch.setattr(
        brain_loop,
        "_insert_action_audit",
        lambda **kw: captured.update(kw) or 42,
    )

    d = _make_decision(
        brain_loop.DecisionKind.PUSH_TO_CLAUDE,
        {"session_id": "sess_xyz"},
        obs_kind="recall_miss",
        obs_subject="sess_user_a",
    )
    audit_id = brain_loop._write_decision_audit(d)
    assert audit_id == 42
    assert captured["route"] == "brain_loop/push_to_claude"
    assert captured["query_text"] == "recall_miss:sess_user_a"
    assert captured["tool"] == "brain_loop"
    assert captured["actor"] == "brain_loop"
    assert captured["session_id"] == "sess_xyz"


def test_write_decision_audit_truncates_query_text(monkeypatch):
    """query_text is capped at 2000 chars (kind + subject concat could be huge)."""
    import brain_loop

    captured: dict = {}
    monkeypatch.setattr(brain_loop, "_insert_action_audit", lambda **kw: captured.update(kw) or 1)

    long_subject = "x" * 5000
    d = _make_decision(
        brain_loop.DecisionKind.OBSERVE_ONLY,
        {},
        obs_kind="k",
        obs_subject=long_subject,
    )
    brain_loop._write_decision_audit(d)
    assert len(captured["query_text"]) == 2000


def test_write_decision_audit_none_when_atoms_store_unavailable(monkeypatch):
    """If _insert_action_audit is None (atoms_store import failed at module
    load), helper returns None and never raises."""
    import brain_loop

    monkeypatch.setattr(brain_loop, "_insert_action_audit", None)
    d = _make_decision(brain_loop.DecisionKind.OBSERVE_ONLY)
    assert brain_loop._write_decision_audit(d) is None


def test_write_decision_audit_swallows_exceptions(monkeypatch):
    """If _insert_action_audit raises, the audit failure is swallowed and
    None is returned — caller continues with action_audit_id=None."""
    import brain_loop

    def _boom(**kw):
        raise RuntimeError("audit db locked")

    monkeypatch.setattr(brain_loop, "_insert_action_audit", _boom)
    d = _make_decision(brain_loop.DecisionKind.OBSERVE_ONLY)
    assert brain_loop._write_decision_audit(d) is None
