from __future__ import annotations

import json
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


def test_cli_dispatch_uses_openclaw_final_fallback_and_closes_breaker(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    recorded: list[dict] = []
    monkeypatch.setattr(
        cli_llm,
        "_record_breaker",
        lambda kind, **kw: recorded.append({"kind": kind, **kw}),
    )

    def fake_backend(backend, model, prompt, timeout):
        if backend == "openclaw":
            return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)
        return cli_llm.CliResult(ok=False, error="not logged in", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)

    r = cli_llm.cli_dispatch("hi", timeout=5, openclaw_agent="sage")

    assert r.ok is True
    assert r.backend == "openclaw"
    assert r.model == "sage"
    assert recorded == [{"kind": cli_llm.BREAKER_KIND, "ok": True}]


def test_cli_dispatch_skips_backend_on_cooldown(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()

    calls: list[str] = []

    def fake_backend(backend, model, prompt, timeout):
        calls.append(f"{backend}/{model}")
        if backend == "codex" and model == "gpt-5.5":
            return cli_llm.CliResult(ok=False, error="timeout", backend=backend, model=model)
        if backend == "openclaw":
            return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)
        return cli_llm.CliResult(ok=False, error="not logged in", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)

    first = cli_llm.cli_dispatch("hi", timeout=5)
    second = cli_llm.cli_dispatch("hi", timeout=5)

    assert first.ok and second.ok
    assert calls.count("codex/gpt-5.5") == 1
    assert calls[-1] == "openclaw/jenna"
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


def test_openclaw_fallback_uses_timeout_floor(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
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

    r = cli_llm._single_openclaw("hi", "jenna", timeout=5)

    assert r.ok is True
    timeout_arg = captured["cmd"][captured["cmd"].index("--timeout") + 1]
    assert int(timeout_arg) >= cli_llm.OPENCLAW_TIMEOUT_FLOOR_S
    assert captured["timeout"] >= cli_llm.OPENCLAW_TIMEOUT_FLOOR_S + 10


def test_single_claude_uses_account_token_env(monkeypatch):
    captured: dict = {}

    monkeypatch.setenv("CLAUDE_TOKEN_1", "token-one")
    cli_llm._SHELL_EXPORT_CACHE.clear()

    def fake_run(cmd, timeout, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(cmd, 0, "OK\n", ""), 0

    monkeypatch.setattr(cli_llm, "_run_cli_process", fake_run)
    monkeypatch.setattr(cli_llm, "_record_usage", lambda *a, **k: None)

    r = cli_llm._single_claude("hi", "claude-opus-4-7@claude1", timeout=5)

    assert r.ok is True
    assert r.text == "OK"
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-opus-4-7"
    assert captured["env"][cli_llm.CLAUDE_TOKEN_TARGET_ENV] == "token-one"


def test_cli_dispatch_tries_claude2_after_claude1_quota(monkeypatch):
    monkeypatch.setattr(cli_llm, "_peek_breaker", lambda kind: _fake_open_snapshot("closed", 0.0))
    cli_llm._BACKEND_COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(cli_llm, "_record_breaker", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_llm,
        "FALLBACK_CHAIN",
        [
            ("claude", "claude-opus-4-7@claude1", "Claude account 1"),
            ("claude", "claude-opus-4-7@claude2", "Claude account 2"),
        ],
    )
    calls: list[str] = []

    def fake_backend(backend, model, prompt, timeout):
        calls.append(model)
        if model.endswith("@claude1"):
            return cli_llm.CliResult(
                ok=False,
                error="usage limit reached",
                backend=backend,
                model=model,
                rate_limited=True,
            )
        return cli_llm.CliResult(ok=True, text="OK", backend=backend, model=model)

    monkeypatch.setattr(cli_llm, "_try_backend", fake_backend)

    r = cli_llm.cli_dispatch("hi", backend="claude", timeout=5)

    assert r.ok is True
    assert r.model == "claude-opus-4-7@claude2"
    assert calls == ["claude-opus-4-7@claude1", "claude-opus-4-7@claude2"]
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
