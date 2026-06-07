from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import cli_llm


def test_run_cli_process_returns_completed_process(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_llm, "CLI_LLM_LOCK", tmp_path / "cli_llm.lock")

    proc, lock_wait_ms = cli_llm._run_cli_process([sys.executable, "-c", "print('ok')"], timeout=5)

    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"
    assert lock_wait_ms >= 0


def test_run_cli_process_can_write_stdin(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_llm, "CLI_LLM_LOCK", tmp_path / "cli_llm.lock")

    proc, _lock_wait_ms = cli_llm._run_cli_process(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        timeout=5,
        input_text="hello stdin",
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == "hello stdin"


def test_single_codex_sends_prompt_via_stdin(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout, **kwargs):
        captured["cmd"] = cmd
        captured["input_text"] = kwargs.get("input_text")
        return subprocess.CompletedProcess(cmd, 0, "OK\n", "tokens used: 12"), 0

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_codex("do the task", "gpt-test", timeout=5)

    assert r.ok is True
    assert r.text == "OK"
    assert captured["cmd"][-1] == "-"
    assert "do the task" not in captured["cmd"]
    assert captured["input_text"] == "do the task"


def test_run_cli_process_kills_process_group_on_timeout(monkeypatch, tmp_path):
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli_llm, "CLI_LLM_LOCK", tmp_path / "cli_llm.lock")

    class FakeProc:
        pid = 4242
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["codex"], timeout)
            self.returncode = -9
            return "", "timed out"

    fake_proc = FakeProc()
    monkeypatch.setattr(cli_llm.subprocess, "Popen", lambda *args, **kwargs: fake_proc)
    monkeypatch.setattr(cli_llm.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    try:
        cli_llm._run_cli_process(["codex"], timeout=1)
    except subprocess.TimeoutExpired as exc:
        assert exc.stderr == "timed out"
    else:
        raise AssertionError("expected TimeoutExpired")

    assert killed == [(4242, cli_llm.signal.SIGKILL)]


def test_run_cli_process_concurrency_allows_max_parallel(monkeypatch, tmp_path):
    """N=2 slots → two callers run in parallel, third waits and times out fast
    on lock_wait, never starting a subprocess.
    """
    import threading
    import time as _t

    monkeypatch.setattr(cli_llm, "CLI_LLM_LOCK", tmp_path / "cli_llm.lock")
    monkeypatch.setattr(cli_llm, "MAX_CONCURRENT_CLI", 2)
    cli_llm._CONCURRENCY_OVERRIDE_CACHE = None
    monkeypatch.setattr(cli_llm, "_effective_concurrency", lambda: 2)

    sleeper = [sys.executable, "-c", "import time; time.sleep(0.4); print('ok')"]
    barrier = threading.Barrier(2)
    results: list[int] = []

    def worker():
        barrier.wait()
        proc, _ = cli_llm._run_cli_process(sleeper, timeout=5, lock_wait_s=2.0)
        results.append(proc.returncode)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    start = _t.monotonic()
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    elapsed = _t.monotonic() - start

    # Two 0.4s sleepers running in parallel should finish well under the 0.8s
    # serial baseline. Headroom of 1.2s tolerates CI/process-startup jitter
    # while still proving parallelism — anything ≥0.8s indicates serialization.
    assert results == [0, 0]
    assert elapsed < 1.2, f"two slots ran serially in {elapsed:.2f}s — concurrency broken"


def test_run_cli_process_lock_wait_timeout_raises_fast(monkeypatch, tmp_path):
    """When all slots are held, acquisition fails within lock_wait_s rather
    than blocking on the subprocess timeout.
    """
    import threading
    import time as _t

    monkeypatch.setattr(cli_llm, "CLI_LLM_LOCK", tmp_path / "cli_llm.lock")
    monkeypatch.setattr(cli_llm, "MAX_CONCURRENT_CLI", 1)
    # Phase 4b: bypass the brain_config_store override cache so this test
    # consistently sees the env-default cap regardless of previous test
    # state (cache TTL is 5 s).
    cli_llm._CONCURRENCY_OVERRIDE_CACHE = None
    monkeypatch.setattr(cli_llm, "_effective_concurrency", lambda: 1)

    holder = [sys.executable, "-c", "import time; time.sleep(2.0)"]
    started = threading.Event()

    def hog():
        started.set()
        cli_llm._run_cli_process(holder, timeout=5)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    started.wait()
    _t.sleep(0.1)  # let hog acquire its slot

    t0 = _t.monotonic()
    raised = False
    try:
        cli_llm._run_cli_process([sys.executable, "-c", "print('x')"], timeout=10, lock_wait_s=0.5)
    except subprocess.TimeoutExpired as exc:
        raised = True
        assert "slots busy" in (exc.stderr or "")
    elapsed = _t.monotonic() - t0
    t.join(timeout=5)

    assert raised, "expected TimeoutExpired on slot exhaustion"
    assert elapsed < 1.0, f"lock_wait_s=0.5 should fail fast, took {elapsed:.2f}s"


def _fake_open_snapshot(state: str = "open", remaining: float = 120.0):
    class FakeSnap:
        def __init__(self):
            self.state = state
            self.is_open = state == "open"
            self.is_half_open = state == "half_open"
            self.is_probing = state == "half_open_probing"
            self.is_closed = state == "closed"
            self.blocks_new_callers = state in ("open", "half_open_probing")
            self.remaining_cooldown_s = remaining

    return FakeSnap()


def test_cli_dispatch_breaker_open_fast_fails(monkeypatch):
    """When breaker is open, cli_dispatch must NOT spawn any subprocess and
    must enqueue the work to backlog. This is the load-shedding contract:
    during upstream outage we burn <1ms per call instead of 30s x 3.
    """
    import time as _t

    spawned: list[list[str]] = []
    monkeypatch.setattr(cli_llm, "_run_cli_process", lambda *a, **k: spawned.append(a) or None)
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("open", 120.0))
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)
    enqueued: list[tuple[str, dict]] = []

    def fake_enqueue(kind, payload):
        enqueued.append((kind, payload))
        return 1

    import sys as _sys

    fake_mod = type(_sys)("llm_backlog")
    fake_mod.enqueue = fake_enqueue
    monkeypatch.setitem(_sys.modules, "llm_backlog", fake_mod)

    t0 = _t.monotonic()
    r = cli_llm.cli_dispatch(
        "anything",
        timeout=30,
        backlog_kind="classify",
        backlog_payload={"prompt": "x"},
    )
    elapsed = _t.monotonic() - t0

    assert r.ok is False
    assert "breaker" in r.error
    assert r.backlogged is True, "open breaker must enqueue to backlog instead of dropping"
    assert spawned == [], "open breaker must NOT spawn any subprocess"
    assert enqueued and enqueued[0][0] == "classify"
    assert elapsed < 0.05, f"breaker fast-fail should be <50ms, took {elapsed*1000:.0f}ms"


def test_cli_dispatch_records_failure_to_breaker(monkeypatch):
    """When all backends fail, cli_dispatch must call record_result(ok=False)
    so the breaker can trip on consecutive failures.
    """
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    recorded: list[dict] = []
    monkeypatch.setattr(
        cli_llm,
        "_record_breaker",
        lambda kind, **kw: recorded.append({"kind": kind, **kw}),
    )

    monkeypatch.setattr(
        cli_llm,
        "_try_backend",
        lambda backend, model, prompt, timeout: cli_llm.CliResult(
            ok=False, error="fake", backend=backend, model=model
        ),
    )

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is False
    assert any(
        rec.get("ok") is False and rec.get("kind") == cli_llm.BREAKER_KIND for rec in recorded
    ), "expected record_result(ok=False) on total dispatch failure"


def test_cli_dispatch_records_success_to_breaker(monkeypatch):
    """A successful call must call record_result(ok=True) so the breaker can
    close after recovery.
    """
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    recorded: list[dict] = []
    monkeypatch.setattr(
        cli_llm,
        "_record_breaker",
        lambda kind, **kw: recorded.append({"kind": kind, **kw}),
    )

    def fake_first_success(backend, model, prompt, timeout):
        return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_first_success)

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is True
    assert recorded == [{"kind": cli_llm.BREAKER_KIND, "ok": True}]


def test_cli_dispatch_uses_hermes_final_fallback_and_closes_breaker(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    recorded: list[dict] = []
    monkeypatch.setattr(
        cli_llm,
        "_record_breaker",
        lambda kind, **kw: recorded.append({"kind": kind, **kw}),
    )

    def fake_backend(backend, model, prompt, timeout):
        if backend == "hermes":
            return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)
        return cli_llm.CliResult(ok=False, error="not logged in", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)

    r = cli_llm.cli_dispatch("hi", timeout=5, openclaw_agent="sage")

    assert r.ok is True
    assert r.backend == "hermes"
    assert r.model == "sage"
    assert recorded == [{"kind": cli_llm.BREAKER_KIND, "ok": True}]


def test_cli_dispatch_can_disable_hermes_fallback(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)
    calls: list[str] = []

    def fake_backend(backend, model, prompt, timeout):
        calls.append(backend)
        return cli_llm.CliResult(ok=False, error="not available", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)

    r = cli_llm.cli_dispatch("hi", timeout=5, allow_openclaw_fallback=False)

    assert r.ok is False
    assert "hermes" not in calls
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_cli_dispatch_skips_backend_on_cooldown(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()

    calls: list[str] = []

    def fake_backend(backend, model, prompt, timeout):
        calls.append(f"{backend}/{model}")
        if backend == "codex" and model == "gpt-5.5":
            return cli_llm.CliResult(ok=False, error="timeout", backend=backend, model=model)
        if backend == "hermes":
            return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)
        return cli_llm.CliResult(ok=False, error="not logged in", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)

    first = cli_llm.cli_dispatch("hi", timeout=5)
    second = cli_llm.cli_dispatch("hi", timeout=5)

    assert first.ok and second.ok
    assert calls.count("codex/gpt-5.5") == 1
    assert calls[-1] == "hermes/jenna"
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_cli_dispatch_cooldown_only_does_not_trip_global_breaker(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    recorded: list[dict] = []
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda kind, **kw: recorded.append({"kind": kind, **kw}))
    monkeypatch.setattr(
        cli_llm,
        "_try_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cooldown should skip subprocess")),
    )

    now = cli_llm.time.time()
    for backend, model, _desc in cli_llm.FALLBACK_CHAIN:
        cli_llm._BACKEND_COOLDOWN_UNTIL[(backend, model)] = now + 60

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is False
    assert "backend_cooldown" in r.error
    assert recorded == []
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_cli_dispatch_half_open_cooldown_only_resolves_probe(monkeypatch, tmp_path):
    breakers = __import__("breakers")

    monkeypatch.setattr(breakers, "AUTONOMY_DB", tmp_path / "autonomy.db")
    monkeypatch.setattr(breakers, "_initialized", False)
    breakers._snapshot_cache.clear()
    monkeypatch.setattr(cli_llm, "_peek_breaker", breakers.peek_breaker)
    monkeypatch.setattr(cli_llm, "_try_claim_probe", breakers.try_claim_probe)
    monkeypatch.setattr(cli_llm, "_record_breaker", breakers.record_result)
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(
        cli_llm,
        "_try_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cooldown should skip subprocess")),
    )

    for _ in range(3):
        breakers.record_result(cli_llm.BREAKER_KIND, ok=False, error="initial outage")
    conn = sqlite3.connect(str(breakers.AUTONOMY_DB))
    conn.execute("UPDATE heal_breakers SET state='half_open' WHERE kind=?", (cli_llm.BREAKER_KIND,))
    conn.commit()
    conn.close()
    breakers._snapshot_cache.clear()

    now = cli_llm.time.time()
    for backend, model, _desc in cli_llm.FALLBACK_CHAIN:
        cli_llm._BACKEND_COOLDOWN_UNTIL[(backend, model)] = now + 60

    r = cli_llm.cli_dispatch("hi", timeout=5)
    snap = breakers.peek_breaker(cli_llm.BREAKER_KIND)

    assert r.ok is False
    assert "backend_cooldown" in r.error
    assert snap.state == "open"
    assert snap.reason == "half_open_probe_blocked_by_backend_cooldown"
    assert snap.remaining_cooldown_s > 0
    assert snap.trip_count == 2
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_cli_dispatch_successful_half_open_probe_closes_breaker(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("half_open", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    claimed: list[str] = []
    recorded: list[dict] = []
    monkeypatch.setattr(cli_llm, "_try_claim_probe", lambda kind: claimed.append(kind) or True)
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda kind, **kw: recorded.append({"kind": kind, **kw}))
    monkeypatch.setattr(
        cli_llm,
        "_try_backend",
        lambda backend, model, prompt, timeout: cli_llm.CliResult(
            ok=True, text="OK", backend=backend, model=model
        ),
    )

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is True
    assert claimed == [cli_llm.BREAKER_KIND]
    assert recorded == [{"kind": cli_llm.BREAKER_KIND, "ok": True}]
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_cli_dispatch_slot_capacity_does_not_trip_global_breaker(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    recorded: list[dict] = []
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda kind, **kw: recorded.append({"kind": kind, **kw}))
    monkeypatch.setattr(
        cli_llm,
        "FALLBACK_CHAIN",
        [("codex", "gpt-test", "Codex test")],
    )
    monkeypatch.setattr(
        cli_llm,
        "_try_backend",
        lambda *args, **kwargs: cli_llm.CliResult(
            ok=False,
            error="all 2 CLI slots busy after 30.0s",
            backend="codex",
            model="gpt-test",
        ),
    )

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is False
    assert "slots busy" in r.error
    assert recorded == []
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_cli_dispatch_timeout_does_not_trip_global_breaker(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    recorded: list[dict] = []
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda kind, **kw: recorded.append({"kind": kind, **kw}))
    monkeypatch.setattr(
        cli_llm,
        "FALLBACK_CHAIN",
        [("codex", "gpt-test", "Codex test")],
    )
    monkeypatch.setattr(
        cli_llm,
        "_try_backend",
        lambda *args, **kwargs: cli_llm.CliResult(
            ok=False,
            error="timeout after 30s: OpenAI Codex startup banner",
            backend="codex",
            model="gpt-test",
        ),
    )

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is False
    assert "timeout after" in r.error
    assert recorded == []
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_single_codex_timeout_error_names_timeout_before_banner(monkeypatch):
    def fake_run(cmd, timeout, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output="",
            stderr="OpenAI Codex v0.128.0 (research preview)\nmodel: gpt-test",
        )

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_codex("do the task", "gpt-test", timeout=5)

    assert r.ok is False
    assert r.error.startswith("timeout after 5s:")
    assert "OpenAI Codex" in r.error


def test_dispatch_compat_can_prefer_hermes_backend(monkeypatch):
    captured: dict = {}

    def fake_cli_dispatch(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return cli_llm.CliResult(ok=True, text="OK", backend="hermes", model="ellie")

    monkeypatch.setattr(cli_llm, "cli_dispatch", fake_cli_dispatch)

    r = cli_llm.dispatch_compat(
        agent="ellie",
        message="task",
        backend="hermes",
        max_backends=1,
        timeout=120,
    )

    assert r.ok is True
    assert captured["backend"] == "hermes"
    assert captured["hermes_profile"] == "ellie"
    assert captured["max_backends"] == 1


def test_cli_dispatch_respects_max_backends(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)
    calls: list[str] = []

    def fake_backend(backend, model, prompt, timeout):
        calls.append(f"{backend}/{model}")
        return cli_llm.CliResult(ok=False, error="timeout", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)

    r = cli_llm.cli_dispatch("hi", timeout=5, max_backends=2)

    assert r.ok is False
    assert calls == ["codex/gpt-5.5", "codex/gpt-5.3-codex-spark"]
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_hermes_fallback_uses_timeout_floor(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["lock_wait_s"] = kwargs.get("lock_wait_s")
        captured["use_slot"] = kwargs.get("use_slot")
        return (
            subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {"result": {"payloads": [{"text": "OK"}], "meta": {"agentMeta": {"usage": {"total": 1}}}}}
                ),
                "",
            ),
            0,
        )

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_hermes("hi", "jenna", timeout=5)

    assert r.ok is True
    assert captured["cmd"][:3] == [cli_llm.HERMES_BIN, "--profile", "jenna"]
    assert "--source" in captured["cmd"]
    assert captured["timeout"] >= cli_llm.HERMES_TIMEOUT_FLOOR_S + 10
    assert captured["lock_wait_s"] >= cli_llm.DEFAULT_LOCK_WAIT_S
    assert captured["use_slot"] is False


def test_hermes_fallback_parses_top_level_payload(monkeypatch):
    def fake_run(cmd, timeout, **kwargs):
        return (
            subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"payloads": [{"text": "OK"}], "meta": {"agentMeta": {"usage": {"total": 7}}}}),
                "EMBEDDED FALLBACK: Gateway agent failed",
            ),
            0,
        )

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_hermes("hi", "jenna", timeout=5)

    assert r.ok is True
    assert r.text == "OK"
    assert r.tokens == 7
    assert r.rate_limited is False


def test_hermes_fallback_accepts_legacy_session_id(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout, **kwargs):
        captured["cmd"] = cmd
        return (
            subprocess.CompletedProcess(cmd, 0, json.dumps({"payloads": [{"text": "OK"}], "meta": {}}), ""),
            0,
        )

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_hermes("hi", "jenna", timeout=5, session_id="ragas-test")

    assert r.ok is True
    assert "--session-id" not in captured["cmd"]
    assert captured["cmd"][:3] == [cli_llm.HERMES_BIN, "--profile", "jenna"]


def test_single_hermes_empty_rate_limited_response_carries_error(monkeypatch):
    def fake_run(cmd, timeout, **kwargs):
        return (
            subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"result": {"payloads": [], "meta": {}}}),
                "rate limit reached",
            ),
            0,
        )

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_hermes("hi", "sage", timeout=5)

    assert r.ok is False
    assert r.rate_limited is True
    assert "rate limit" in r.error.lower()


def _write_hermes_state(home: Path, profile: str, session_id: str, assistant_content: str | None) -> None:
    state_db = home / "profiles" / profile / "state.db"
    state_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', 'prompt')", (session_id,)
        )
        if assistant_content is not None:
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                (session_id, assistant_content),
            )
        conn.commit()
    finally:
        conn.close()


def test_hermes_fallback_recovers_empty_stdout_from_persisted_assistant(monkeypatch, tmp_path):
    session_id = "20260607_063318_c79542"
    hermes_home = tmp_path / ".hermes"
    _write_hermes_state(hermes_home, "sage", session_id, '{"entities":[]}')

    def fake_run(cmd, timeout, **kwargs):
        return (
            subprocess.CompletedProcess(cmd, 0, "", f"session_id: {session_id}\n"),
            0,
        )

    monkeypatch.setattr(cli_llm, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_hermes("hi", "sage", timeout=5)

    assert r.ok is True
    assert r.text == '{"entities":[]}'
    assert r.error == ""
    assert r.rate_limited is False


def test_hermes_fallback_empty_stdout_without_persisted_assistant_remains_failure(monkeypatch, tmp_path):
    session_id = "20260607_065457_cc6d23"
    hermes_home = tmp_path / ".hermes"
    _write_hermes_state(hermes_home, "sage", session_id, None)

    def fake_run(cmd, timeout, **kwargs):
        return (
            subprocess.CompletedProcess(cmd, 0, "", f"session_id: {session_id}\n"),
            0,
        )

    monkeypatch.setattr(cli_llm, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_hermes("hi", "sage", timeout=5)

    assert r.ok is False
    assert r.text == ""
    assert "hermes returned empty response" in r.error


def test_cli_dispatch_records_recovered_hermes_response_as_success(monkeypatch, tmp_path):
    session_id = "20260607_070000_recovered"
    hermes_home = tmp_path / ".hermes"
    _write_hermes_state(hermes_home, "sage", session_id, "OK")
    recorded: list[dict] = []

    def fake_run(cmd, timeout, **kwargs):
        return (
            subprocess.CompletedProcess(cmd, 0, "", f"session_id: {session_id}\n"),
            0,
        )

    monkeypatch.setattr(cli_llm, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(cli_llm, "FALLBACK_CHAIN", [("hermes", "sage", "Hermes test")])
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda kind, **kw: recorded.append({"kind": kind, **kw}))
    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm.cli_dispatch("hi", timeout=5)

    assert r.ok is True
    assert r.text == "OK"
    assert recorded == [{"kind": cli_llm.BREAKER_KIND, "ok": True}]
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_legacy_claude_backend_compat_routes_to_codex_primary(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout, **kwargs):
        captured["cmd"] = cmd
        captured["input_text"] = kwargs.get("input_text")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "OK\n", ""), 0

    monkeypatch.setattr(cli_llm, "CODEX_PRIMARY_MODEL", "gpt-5.5")
    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._legacy_claude_backend_via_codex("hi", "ignored-legacy-claude-model", timeout=5)

    assert r.ok is True
    assert r.text == "OK"
    assert r.backend == "codex"
    assert r.model == "gpt-5.5"
    assert captured["cmd"][0:2] == [cli_llm.CODEX_BIN, "exec"]
    assert captured["cmd"][captured["cmd"].index("-m") + 1] == "gpt-5.5"
    assert captured["input_text"] == "hi"
    assert captured["env"] is None


def test_cli_dispatch_normalizes_claude_backend_hint_to_codex(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_llm,
        "FALLBACK_CHAIN",
        [
            ("codex", "gpt-5.5", "Codex primary"),
            ("hermes", "jenna", "Emergency fallback"),
        ],
    )
    calls: list[tuple[str, str]] = []

    def fake_backend(backend, model, prompt, timeout):
        calls.append((backend, model))
        return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)

    r = cli_llm.cli_dispatch("hi", backend="claude", timeout=5, max_backends=1)

    assert r.ok is True
    assert r.backend == "codex"
    assert r.model == "gpt-5.5"
    assert calls == [("codex", "gpt-5.5")]
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()


def test_run_cli_process_closes_stdin(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_llm, "CLI_LLM_LOCK", tmp_path / "cli_llm.lock")
    seen = {}

    class FakeProc:
        pid = 4243
        returncode = 0

        def communicate(self, timeout=None):
            return "ok\n", ""

    def fake_popen(*args, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(cli_llm.subprocess, "Popen", fake_popen)

    proc, _ = cli_llm._run_cli_process([sys.executable, "-c", "print('ok')"], timeout=5)

    assert proc.returncode == 0
    assert seen["stdin"] is subprocess.DEVNULL


def test_failure_taxonomy_snapshot_covers_all_provider_classes_without_cli_calls(monkeypatch):
    spawned = []
    monkeypatch.setattr(cli_llm, "_run_cli_process", lambda *args, **kwargs: spawned.append(args))

    snapshot = cli_llm.failure_taxonomy_snapshot()

    assert snapshot["version"] == "cli-failure-taxonomy-v1"
    assert snapshot["dashboard_surface"] == "/brain/usage.llm.failure_taxonomy"
    assert set(snapshot["provider_classes"]) == {"codex", "hermes"}
    assert [row["reason"] for row in snapshot["classes"]] == list(cli_llm.FAILOVER_REASONS)
    assert {row["reason"] for row in snapshot["classes"]} >= {
        "auth",
        "billing",
        "model_not_found",
        "context_overflow",
        "rate_limit",
        "overloaded",
        "unknown",
    }
    assert (
        next(row for row in snapshot["classes"] if row["reason"] == "context_overflow")["should_compress"]
        is True
    )
    assert (
        next(row for row in snapshot["classes"] if row["reason"] == "auth")["should_rotate_credential"]
        is True
    )
    assert next(row for row in snapshot["classes"] if row["reason"] == "rate_limit")["backend_cooldown_s"] > 0
    assert spawned == []


def test_usage_stats_exposes_failure_taxonomy(monkeypatch, tmp_path):
    usage_db = tmp_path / "llm_usage.db"
    monkeypatch.setattr(cli_llm, "LLM_USAGE_DB", usage_db)

    cli_llm._record_usage("codex", "gpt-5.5", tokens=3, duration_ms=4, ok=True)

    stats = cli_llm.get_usage_stats(days=1)

    assert stats["source"] == "cli_llm"
    assert stats["failure_taxonomy"]["version"] == "cli-failure-taxonomy-v1"
    assert stats["failure_taxonomy"]["provider_classes"] == ["codex", "hermes"]
