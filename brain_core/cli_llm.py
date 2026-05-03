"""Subscription-backed LLM dispatch via codex CLI + claude CLI with
comprehensive fallback chain and llm_backlog integration.

Replaces openclaw_dispatch.dispatch for ALL brain mechanical calls. The
OpenClaw path drags a 95MB session history into every call (414K tokens
per simple query). CLI is stateless: ~5K tokens/call, 3-5x faster, cleaner
output (no agent persona pollution).

## Fallback chain (worst-case guaranteed catch-up)

    1. codex exec (gpt-5.5, ChatGPT Pro sub) — primary, 2-6s
    2. codex exec -m gpt-5.3-codex-spark — lighter fallback if primary hit quota
    3. claude1 then claude2 via Claude CLI setup-token subscriptions — provider-level fallback
    4. openclaw agent — authenticated, heavier context, emergency fallback
    5. llm_backlog.enqueue — if every provider is exhausted, queue the work
       so it catches up automatically when quota resets

Rate-limit detection patterns match openclaw_dispatch.RATE_LIMIT_PATTERNS
so the behavior is consistent with the existing breaker.

## API compatibility with openclaw_dispatch

`CliResult` mirrors `DispatchResult` (ok, text, error, duration_ms,
provider, model) so call-sites can swap with minimal churn. Plus token
accounting for the new llm_daily_spend_usd SLO.

## When to use

- Use `cli_dispatch` for: HyDE, classify, atom compression, entity
  extraction, reflection, synthesis, SLO notifications — anything that
  doesn't need OpenClaw's agent persona, session continuity, or skills.
- Use `cli_dispatch_with_schema` for structured JSON output with retry.
- Keep `openclaw_dispatch` for: Chris↔Jenna Telegram interactive chat,
  skill-heavy agent turns (imsg/things/obsidian), multi-agent messages.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.cli_llm")

# 2026-04-27 — wire the existing autonomy circuit breaker
# (brain_core/breakers.py) into the dispatch hot path. Today's incident showed
# the breaker has been silently `closed` through 12 hours of 90% failure
# because cli_dispatch never reported outcomes and never consulted state.
# With this wiring: 3 consecutive failures → open 5m → probe at 5m → tier up
# to 15m/1h/4h on repeated probe failure → close on first success. While
# open, dispatches fast-fail to backlog in <1ms instead of burning 30s per
# attempt. Imports are best-effort — if breakers can't load (test envs,
# isolated CLI scripts), cli_dispatch keeps working with the breaker as a
# no-op so we never harden a soft dependency into a hard one.
BREAKER_KIND = "llm.dispatch"
try:
    from breakers import peek_breaker as _peek_breaker
    from breakers import record_result as _record_breaker
    from breakers import try_claim_probe as _try_claim_probe
except ImportError:
    try:
        from brain_core.breakers import peek_breaker as _peek_breaker
        from brain_core.breakers import record_result as _record_breaker
        from brain_core.breakers import try_claim_probe as _try_claim_probe
    except ImportError:

        def _peek_breaker(_kind: str) -> Any:  # type: ignore[misc]
            return None

        def _record_breaker(_kind: str, *, ok: bool = True, error: str = "") -> Any:  # type: ignore[misc]
            return None

        def _try_claim_probe(_kind: str) -> bool:  # type: ignore[misc]
            return True


# 2026-04-17 — first-failure flag so llm_usage write bugs surface once in logs
# rather than silently losing every CLI dispatch's telemetry forever.
_usage_warned = False

LLM_USAGE_DB = Path("/Users/chrischo/server/brain/logs/llm_usage.db")
CLI_LLM_LOCK = Path(os.getenv("BRAIN_CLI_LLM_LOCK_PATH", "/Users/chrischo/server/brain/logs/cli_llm.lock"))
CODEX_BIN = "/opt/homebrew/bin/codex"
CLAUDE_BIN = "/Users/chrischo/.local/bin/claude"
CLAUDE_MODEL = os.getenv("BRAIN_CLAUDE_MODEL", "claude-opus-4-7")
CLAUDE_TOKEN_TARGET_ENV = os.getenv("BRAIN_CLAUDE_TOKEN_TARGET_ENV", "CLAUDE_CODE_OAUTH_TOKEN")
CLAUDE_SHELL_EXPORT_FILES = tuple(
    Path(p).expanduser()
    for p in os.getenv("BRAIN_CLAUDE_TOKEN_EXPORT_FILES", "~/.zshrc").split(":")
    if p.strip()
)
OPENCLAW_BIN = os.getenv("BRAIN_OPENCLAW_BIN", "/Users/chrischo/.local/bin/openclaw")
OPENCLAW_FALLBACK_AGENT = os.getenv("BRAIN_OPENCLAW_FALLBACK_AGENT", "jenna")
OPENCLAW_TIMEOUT_FLOOR_S = max(5, int(os.getenv("BRAIN_OPENCLAW_TIMEOUT_FLOOR_S", "45")))
OPENCLAW_TIMEOUT_CAP_S = max(OPENCLAW_TIMEOUT_FLOOR_S, int(os.getenv("BRAIN_OPENCLAW_TIMEOUT_CAP_S", "90")))
OPENCLAW_FALLBACK_ENABLED = os.getenv("BRAIN_OPENCLAW_FALLBACK_ENABLED", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# 2026-04-27 — bounded concurrency replaces the prior single-flock global
# serializer. One long caller (e.g. post-session distill) was blocking every
# other brain dispatch behind a global flock; the per-call timeout was eating
# into the subprocess budget while waiting for that lock, producing 80-94%
# spurious-timeout failures across thousands of calls. Bounded concurrency
# preserves the original memory contract (cap on simultaneous codex/claude
# helper processes) without single-caller starvation. Lock-wait is tracked
# separately from the subprocess timeout so a slow CLI never bleeds into the
# next dispatch's budget.
MAX_CONCURRENT_CLI = max(1, int(os.getenv("BRAIN_CLI_LLM_CONCURRENCY", "2")))
DEFAULT_LOCK_WAIT_S = max(1.0, float(os.getenv("BRAIN_CLI_LLM_LOCK_WAIT_S", "30")))
LOCK_WAIT_WARN_S = float(os.getenv("BRAIN_CLI_LLM_LOCK_WAIT_WARN_S", "10"))

# Phase 4b cost governor: brain_config_store can override the env-var
# concurrency cap. brain_loop sets BRAIN_CLI_LLM_CONCURRENCY=1 +
# BRAIN_CLI_LLM_CONCURRENCY_UNTIL=<epoch> when an LLM usage spike fires
# and there's no active Chris session — bounding damage from runaway jobs.
# We cache the override for 5 s so dispatch hot-path stays fast.
_CONCURRENCY_OVERRIDE_CACHE: tuple[float, int] | None = None
_CONCURRENCY_OVERRIDE_TTL_S = 5.0
_SHELL_EXPORT_CACHE: dict[str, str | None] = {}


def _effective_concurrency() -> int:
    """Return the live max-concurrent cap. Reads brain_config_store every 5s
    with a TTL cache; falls back to the env-var floor when no override (or
    when the override has expired its UNTIL timestamp).
    """
    global _CONCURRENCY_OVERRIDE_CACHE
    now = time.time()
    if _CONCURRENCY_OVERRIDE_CACHE is not None:
        cached_at, cached_val = _CONCURRENCY_OVERRIDE_CACHE
        if now - cached_at < _CONCURRENCY_OVERRIDE_TTL_S:
            return cached_val
    val = MAX_CONCURRENT_CLI
    try:
        # 2026-04-27 review fix: only insert sys.path entry once. Previously
        # this ran on every 5s cache miss and grew sys.path indefinitely
        # (~17k duplicates/day on a long-running server).
        _brain_core_dir = str(Path(__file__).resolve().parent)
        if _brain_core_dir not in sys.path:
            sys.path.insert(0, _brain_core_dir)
        import brain_config_store

        until_raw = brain_config_store.get("BRAIN_CLI_LLM_CONCURRENCY_UNTIL")
        if until_raw and float(until_raw) > now:
            override_raw = brain_config_store.get("BRAIN_CLI_LLM_CONCURRENCY")
            if override_raw:
                val = max(1, int(override_raw))
    except Exception as exc:
        log.debug("cli_llm concurrency override lookup failed: %s", exc)
    _CONCURRENCY_OVERRIDE_CACHE = (now, val)
    return val


def _read_shell_export_var(name: str) -> str | None:
    """Read simple `export NAME=value` lines without logging or shell eval.

    Launchd services do not inherit interactive zsh exports. Chris stores the
    long-lived Claude Max tokens in shell exports, so the brain server may need
    to read those specific names directly after restart. This parser is narrow
    on purpose: no command substitution, no variable expansion, no sourcing the
    whole shell rc file.
    """

    if name in _SHELL_EXPORT_CACHE:
        return _SHELL_EXPORT_CACHE[name]
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}=(.*)\s*$")
    for path in CLAUDE_SHELL_EXPORT_FILES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            _SHELL_EXPORT_CACHE[name] = value or None
            return _SHELL_EXPORT_CACHE[name]
    _SHELL_EXPORT_CACHE[name] = None
    return None


def _secret_env(name: str) -> str | None:
    return os.getenv(name) or _read_shell_export_var(name)


def _claude_account_specs() -> list[tuple[str, tuple[str, ...]]]:
    raw = os.getenv("BRAIN_CLAUDE_ACCOUNT_ENVS", "").strip()
    if raw:
        specs: list[tuple[str, tuple[str, ...]]] = []
        for idx, item in enumerate(raw.split(","), 1):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                label, envs = item.split(":", 1)
                specs.append(
                    (label.strip() or f"claude{idx}", tuple(e.strip() for e in envs.split("|") if e.strip()))
                )
            else:
                specs.append((f"claude{idx}", (item,)))
        return specs
    return [
        ("claude1", ("CLAUDE_TOKEN_1", "CLAUDE1")),
        ("claude2", ("CLAUDE_TOKEN_2", "CLAUDE2")),
    ]


def _claude_token_for_account(label: str) -> str | None:
    for account_label, env_names in _claude_account_specs():
        if account_label == label:
            for env_name in env_names:
                token = _secret_env(env_name)
                if token:
                    return token
    return None


def _claude_account_labels() -> list[str]:
    # Chris does not use the ambient/default Claude CLI identity for dispatch.
    # Only explicit Max subscription accounts (claude1, then claude2) are valid.
    return [label for label, _envs in _claude_account_specs() if _claude_token_for_account(label)]


def _claude_model_label(model: str, account_label: str) -> str:
    return model if account_label == "default" else f"{model}@{account_label}"


def _split_claude_model_label(model: str) -> tuple[str, str]:
    if "@" not in model:
        return model, "default"
    base_model, account_label = model.rsplit("@", 1)
    return base_model, account_label or "default"


def _claude_chain_entries() -> list[tuple[str, str, str]]:
    return [
        (
            "claude",
            _claude_model_label(CLAUDE_MODEL, label),
            f"Claude Max account {label} — provider fallback",
        )
        for label in _claude_account_labels()
    ]


# ── Fallback chain (ordered by preference) ────────────────────
# Each entry: (backend, model, description)
FALLBACK_CHAIN: list[tuple[str, str, str]] = [
    ("codex", "gpt-5.5", "ChatGPT Pro primary — frontier quality"),
    ("codex", "gpt-5.3-codex-spark", "ChatGPT Pro lightweight — quota fallback"),
    *_claude_chain_entries(),
    ("openclaw", OPENCLAW_FALLBACK_AGENT, "OpenClaw authenticated emergency fallback"),
]

_BACKEND_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
_BACKEND_COOLDOWN_S = {
    "auth": 3600.0,
    "billing": 3600.0,
    "rate_limit": 3600.0,
    "timeout": 600.0,
    "overloaded": 300.0,
}


def _slot_paths() -> list[Path]:
    """Per-slot lockfile paths derived from CLI_LLM_LOCK. Exactly
    `_effective_concurrency()` lockfiles per call; each caller holds one,
    so up to that many subprocesses run in parallel. The set is recomputed
    each call so a brain_config_store override (Phase 4b cost governor)
    takes effect on the next dispatch.
    """
    base = CLI_LLM_LOCK
    return [base.with_name(f"{base.stem}.slot{i}{base.suffix}") for i in range(_effective_concurrency())]


def _acquire_slot(lock_wait_s: float) -> tuple[int, Any]:
    """Grab one free slot lockfile within lock_wait_s. Returns (slot_idx,
    open file handle). Caller must close the handle to release the slot.
    Raises subprocess.TimeoutExpired if every slot stays busy past the cap.
    """
    slots = _slot_paths()
    slots[0].parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, lock_wait_s)
    while True:
        for idx, p in enumerate(slots):
            f = p.open("a+")
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.close()
                continue
            except OSError:
                # ENOLCK, EIO on NFS, etc. — close the handle so we don't leak
                # an fd per dispatch on long-running brain-server processes.
                f.close()
                raise
            return idx, f
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(
                cmd=["cli-slot-acquire"],
                timeout=lock_wait_s,
                output="",
                stderr=f"all {_effective_concurrency()} CLI slots busy after {lock_wait_s:.1f}s",
            )
        time.sleep(0.05)


def _run_cli_process(
    cmd: list[str],
    timeout: int,
    *,
    lock_wait_s: float | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run a subscription CLI with a process-group timeout.

    `subprocess.run(..., timeout=...)` kills only the direct child. Codex/Claude
    CLIs can leave helper descendants behind when the parent wedges, which is
    how stale "brain synthesis drive" processes survived for hours. A new
    session lets us terminate the whole group deterministically.

    Bounded concurrency (MAX_CONCURRENT_CLI slot lockfiles) caps the simultaneous
    helper-process count without globally serializing every dispatch. The
    `timeout` budget is reserved for the subprocess itself; slot acquisition
    has its own `lock_wait_s` cap, so contention can never silently steal
    time from the LLM call. If no slot frees up within lock_wait_s, raises
    TimeoutExpired so the caller falls through to the backlog quickly
    instead of waiting for the subprocess timeout to expire on a never-
    started call.

    Returns (CompletedProcess, lock_wait_ms) so callers can record and SLO
    on lock contention separately from real LLM latency.
    """
    if lock_wait_s is None:
        lock_wait_s = DEFAULT_LOCK_WAIT_S

    lock_t0 = time.monotonic()
    try:
        _slot_idx, lock_f = _acquire_slot(lock_wait_s)
    except subprocess.TimeoutExpired as exc:
        # Lock-wait timeout: callers want lock_wait_ms in their telemetry.
        # Stamp it on the exception so _single_* unwrap and propagate it.
        exc.lock_wait_ms = int((time.monotonic() - lock_t0) * 1000)  # type: ignore[attr-defined]
        raise
    lock_wait_ms = int((time.monotonic() - lock_t0) * 1000)
    if lock_wait_ms >= LOCK_WAIT_WARN_S * 1000:
        log.warning(
            "cli_llm slot wait %dms (concurrency=%d, cap=%.1fs) — sustained "
            "high waits indicate a slow caller is holding a slot",
            lock_wait_ms,
            _effective_concurrency(),
            lock_wait_s,
        )
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            new_exc = subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
            new_exc.lock_wait_ms = lock_wait_ms  # type: ignore[attr-defined]
            raise new_exc from exc
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr), lock_wait_ms
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            lock_f.close()


# Rate-limit / quota-exhausted patterns in stderr/stdout
_QUOTA_PATTERNS = [
    re.compile(r"rate[_ ]?limit", re.IGNORECASE),
    re.compile(r"quota.*exceed", re.IGNORECASE),
    re.compile(r"out of.*usage", re.IGNORECASE),
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"cooldown", re.IGNORECASE),
    re.compile(r"all profiles unavailable", re.IGNORECASE),
    re.compile(r"not logged in", re.IGNORECASE),
]


# ── Failover classification (hermes-agent `agent/error_classifier.py`) ──
#
# Ad-hoc string matching worked while brain had one error class (rate
# limit). As fallback chains grow, structured classification lets the
# caller pick the *right* recovery: retry, rotate credential, compress
# context, or give up — instead of blindly looping.
_AUTH_PATTERNS = [
    re.compile(r"(?i)unauthori[sz]ed"),
    re.compile(r"(?i)authentication"),
    re.compile(r"(?i)not logged in"),
    re.compile(r"(?i)invalid.*api.?key"),
    re.compile(r"(?i)\b401\b"),
]
_BILLING_PATTERNS = [
    re.compile(r"(?i)billing"),
    re.compile(r"(?i)insufficient.*balance"),
    re.compile(r"(?i)payment required"),
    re.compile(r"(?i)\b402\b"),
]
_OVERLOAD_PATTERNS = [
    re.compile(r"(?i)overloaded"),
    re.compile(r"(?i)service unavailable"),
    re.compile(r"(?i)\b(502|503|504)\b"),
    re.compile(r"(?i)gateway"),
]
_CONTEXT_PATTERNS = [
    re.compile(r"(?i)context (length|window|overflow)"),
    re.compile(r"(?i)input (too long|length)"),
    re.compile(r"(?i)token.*limit"),
    re.compile(r"(?i)maximum context"),
]
_MODEL_MISSING_PATTERNS = [
    re.compile(r"(?i)model.*not found"),
    re.compile(r"(?i)no such model"),
    re.compile(r"(?i)unknown model"),
]


# Priority matters: a single blob can match multiple classes (e.g. auth
# errors often mention "rate" too). First-match-wins ordered by recovery
# impact — auth/billing require human action, context-overflow is caller-
# recoverable, rate-limit is just a retry.
FAILOVER_REASONS = (
    "auth",
    "billing",
    "model_not_found",
    "context_overflow",
    "rate_limit",
    "overloaded",
    "unknown",
)


def classify_cli_error(stderr: str, stdout: str) -> dict:
    """Return ``{"reason": str, "retryable": bool, "should_fallback": bool,
    "should_compress": bool, "should_rotate_credential": bool}``.

    Callers (``cli_dispatch`` and its schema wrapper) use this to decide
    between retry, switch to next FALLBACK_CHAIN entry, or surface to the
    user. All decisions are local — no remote state needed.
    """
    blob = f"{stderr}\n{stdout}"
    if any(p.search(blob) for p in _AUTH_PATTERNS):
        return {
            "reason": "auth",
            "retryable": False,
            "should_fallback": True,
            "should_compress": False,
            "should_rotate_credential": True,
        }
    if any(p.search(blob) for p in _BILLING_PATTERNS):
        return {
            "reason": "billing",
            "retryable": False,
            "should_fallback": True,
            "should_compress": False,
            "should_rotate_credential": False,
        }
    if any(p.search(blob) for p in _MODEL_MISSING_PATTERNS):
        return {
            "reason": "model_not_found",
            "retryable": False,
            "should_fallback": True,
            "should_compress": False,
            "should_rotate_credential": False,
        }
    if any(p.search(blob) for p in _CONTEXT_PATTERNS):
        return {
            "reason": "context_overflow",
            "retryable": True,
            "should_fallback": False,
            "should_compress": True,
            "should_rotate_credential": False,
        }
    if any(p.search(blob) for p in _QUOTA_PATTERNS):
        return {
            "reason": "rate_limit",
            "retryable": True,
            "should_fallback": True,
            "should_compress": False,
            "should_rotate_credential": False,
        }
    if any(p.search(blob) for p in _OVERLOAD_PATTERNS):
        return {
            "reason": "overloaded",
            "retryable": True,
            "should_fallback": True,
            "should_compress": False,
            "should_rotate_credential": False,
        }
    return {
        "reason": "unknown",
        "retryable": False,
        "should_fallback": True,
        "should_compress": False,
        "should_rotate_credential": False,
    }


# codex metadata parsing (non-TTY stderr format)
_CODEX_TOKEN_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)")


@dataclass
class CliResult:
    ok: bool
    text: str = ""
    error: str = ""
    tokens: int = 0
    duration_ms: int = 0
    backend: str = ""
    model: str = ""
    attempts: int = 0
    rate_limited: bool = False
    backlogged: bool = False  # True when dispatch failed and work was queued
    tried: list[tuple[str, str]] = field(default_factory=list)  # [(backend, model)...]
    lock_wait_ms: int = 0  # time spent waiting for a free CLI slot (separate from duration_ms)

    # Compat shim with openclaw_dispatch.DispatchResult
    @property
    def provider(self) -> str:
        if self.backend == "codex":
            return "openai-codex"
        if self.backend == "claude":
            return "anthropic"
        if self.backend == "openclaw":
            return "openclaw"
        return self.backend


def _record_usage(
    backend: str,
    model: str,
    tokens: int,
    duration_ms: int,
    ok: bool,
    rate_limited: bool = False,
) -> None:
    """Append to llm_usage.db so the llm_daily_spend_usd SLO and /metrics see
    the CLI dispatches alongside openclaw_dispatch calls. Cost is 0 because
    these are subscription-backed — token count is the real signal.
    """
    conn = None
    try:
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        # 2026-04-17 fix: audit showed that if openclaw_dispatch had never been
        # loaded in this process, the llm_usage table doesn't exist and every
        # CLI dispatch silently lost its telemetry. Create-if-missing covers
        # the standalone case (CLI scripts, cold worker, etc).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_usage ("
            "timestamp TEXT, agent TEXT, duration_ms INTEGER, ok INTEGER, "
            "prompt_tokens INTEGER, response_tokens INTEGER, skipped_cb INTEGER, "
            "provider TEXT, model TEXT, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, cost_usd REAL)"
        )
        conn.execute(
            "INSERT INTO llm_usage "
            "(timestamp, agent, duration_ms, ok, prompt_tokens, response_tokens, "
            "skipped_cb, provider, model, cache_read_tokens, cache_write_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0.0)",
            (
                datetime.now(UTC).isoformat(),
                f"cli:{backend}",
                duration_ms,
                1 if ok else 0,
                tokens,
                0,
                1 if rate_limited else 0,
                backend,
                model,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        global _usage_warned
        if not _usage_warned:
            log.warning("llm_usage write failed (suppressing further): %s", exc)
            _usage_warned = True
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def _is_quota_error(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}"
    return any(p.search(blob) for p in _QUOTA_PATTERNS)


def _cooldown_reason(result: CliResult) -> str:
    if result.rate_limited:
        return "rate_limit"
    if "timeout" in (result.error or "").lower():
        return "timeout"
    classified = classify_cli_error(result.error, "")
    return str(classified.get("reason") or "unknown")


def _backend_cooldown_remaining(backend: str, model: str) -> float:
    until = _BACKEND_COOLDOWN_UNTIL.get((backend, model), 0.0)
    return max(0.0, until - time.time())


def _record_backend_outcome(result: CliResult) -> None:
    key = (result.backend, result.model)
    if result.ok:
        _BACKEND_COOLDOWN_UNTIL.pop(key, None)
        return
    reason = _cooldown_reason(result)
    cooldown_s = _BACKEND_COOLDOWN_S.get(reason)
    if cooldown_s:
        _BACKEND_COOLDOWN_UNTIL[key] = time.time() + cooldown_s
        log.warning(
            "cli_dispatch backend cooldown: %s/%s reason=%s cooldown=%ss",
            result.backend,
            result.model,
            reason,
            int(cooldown_s),
        )


def _parse_codex(stdout: str, stderr: str) -> tuple[str, int]:
    """Non-TTY codex separates output cleanly:
    - stdout: response text only
    - stderr: metadata (session id, model, tokens used)
    """
    text = stdout.strip()
    tokens = 0
    m = _CODEX_TOKEN_RE.search(stderr or "") or _CODEX_TOKEN_RE.search(stdout)
    if m:
        with contextlib.suppress(ValueError):
            tokens = int(m.group(1).replace(",", ""))
    return text, tokens


def _single_codex(prompt: str, model: str, timeout: int) -> CliResult:
    """One codex exec attempt with a specific model.

    Uses --skip-git-repo-check because brain's CWD is not a git repo and
    codex refuses to run outside trusted repos by default. Without this
    flag the subprocess fails in ~10ms with 'Not inside a trusted
    directory', which was causing every dispatch from brain-server to
    fall through to claude fallback.
    """
    t0 = time.time()
    cmd = [CODEX_BIN, "exec", "--skip-git-repo-check", "-m", model, prompt]
    try:
        proc, lock_wait_ms = _run_cli_process(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        dur = int((time.time() - t0) * 1000)
        _record_usage("codex", model, 0, dur, False)
        err = (exc.stderr or "timeout")[:500] if isinstance(exc.stderr, str) else "timeout"
        return CliResult(
            ok=False,
            error=err,
            duration_ms=dur,
            backend="codex",
            model=model,
            lock_wait_ms=getattr(exc, "lock_wait_ms", 0),
        )
    dur = int((time.time() - t0) * 1000)
    rate_limited = _is_quota_error(proc.stderr, proc.stdout)
    if proc.returncode != 0:
        _record_usage("codex", model, 0, dur, False, rate_limited)
        return CliResult(
            ok=False,
            error=(proc.stderr or proc.stdout)[:500],
            duration_ms=dur,
            backend="codex",
            model=model,
            rate_limited=rate_limited,
            lock_wait_ms=lock_wait_ms,
        )
    text, tokens = _parse_codex(proc.stdout, proc.stderr)
    _record_usage("codex", model, tokens, dur, bool(text), rate_limited)
    return CliResult(
        ok=bool(text),
        text=text,
        tokens=tokens,
        duration_ms=dur,
        backend="codex",
        model=model,
        rate_limited=rate_limited,
        lock_wait_ms=lock_wait_ms,
    )


def _single_claude(prompt: str, model: str, timeout: int) -> CliResult:
    """One claude -p attempt with a specific model/account label.

    Model labels may be `claude-opus-4-7@claude1` or
    `claude-opus-4-7@claude2`. The account suffix selects Chris's exported
    long-lived Claude Max token. Plain model names are treated as claude1 so
    brain dispatch never uses the ambient/default Claude CLI identity.
    """
    t0 = time.time()
    base_model, account_label = _split_claude_model_label(model)
    if account_label == "default":
        account_label = "claude1"
    token = _claude_token_for_account(account_label)
    if not token:
        return CliResult(
            ok=False,
            error=f"claude token missing for {account_label}",
            duration_ms=0,
            backend="claude",
            model=model,
        )
    env = os.environ.copy()
    env[CLAUDE_TOKEN_TARGET_ENV] = token
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", base_model, "--no-session-persistence"]
    try:
        proc, lock_wait_ms = _run_cli_process(cmd, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        dur = int((time.time() - t0) * 1000)
        _record_usage("claude", model, 0, dur, False)
        err = (exc.stderr or "timeout")[:500] if isinstance(exc.stderr, str) else "timeout"
        return CliResult(
            ok=False,
            error=err,
            duration_ms=dur,
            backend="claude",
            model=model,
            lock_wait_ms=getattr(exc, "lock_wait_ms", 0),
        )
    dur = int((time.time() - t0) * 1000)
    rate_limited = _is_quota_error(proc.stderr, proc.stdout)
    if proc.returncode != 0:
        _record_usage("claude", model, 0, dur, False, rate_limited)
        return CliResult(
            ok=False,
            error=(proc.stderr or proc.stdout)[:500],
            duration_ms=dur,
            backend="claude",
            model=model,
            rate_limited=rate_limited,
            lock_wait_ms=lock_wait_ms,
        )
    text = proc.stdout.strip()
    _record_usage("claude", model, 0, dur, bool(text), rate_limited)
    return CliResult(
        ok=bool(text),
        text=text,
        duration_ms=dur,
        backend="claude",
        model=model,
        rate_limited=rate_limited,
        lock_wait_ms=lock_wait_ms,
    )


def _parse_openclaw_payload(stdout: str) -> tuple[str, int]:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip(), 0
    payloads = ((envelope.get("result") or {}).get("payloads")) or []
    text = "\n".join(str(p.get("text") or "").strip() for p in payloads if p.get("text")).strip()
    usage = (((envelope.get("result") or {}).get("meta") or {}).get("agentMeta") or {}).get("usage") or {}
    tokens = int(usage.get("total") or usage.get("input") or 0)
    return text, tokens


def _single_openclaw(prompt: str, model: str, timeout: int) -> CliResult:
    """Emergency fallback through OpenClaw's authenticated agent path.

    This is intentionally last in the chain because OpenClaw carries large
    agent context, but it is better than opening the global breaker when the
    stateless CLIs are logged out or quota-exhausted.
    """

    if not OPENCLAW_FALLBACK_ENABLED:
        return CliResult(ok=False, error="openclaw fallback disabled", backend="openclaw", model=model)
    t0 = time.time()
    openclaw_timeout = max(OPENCLAW_TIMEOUT_FLOOR_S, min(timeout, OPENCLAW_TIMEOUT_CAP_S))
    cmd = [
        OPENCLAW_BIN,
        "agent",
        "--agent",
        model or OPENCLAW_FALLBACK_AGENT,
        "--message",
        prompt,
        "--json",
        "--thinking",
        "off",
        "--timeout",
        str(openclaw_timeout),
    ]
    try:
        proc, lock_wait_ms = _run_cli_process(cmd, timeout=max(openclaw_timeout + 10, 20))
    except subprocess.TimeoutExpired as exc:
        dur = int((time.time() - t0) * 1000)
        _record_usage("openclaw", model, 0, dur, False)
        err = (exc.stderr or "timeout")[:500] if isinstance(exc.stderr, str) else "timeout"
        return CliResult(
            ok=False,
            error=err,
            duration_ms=dur,
            backend="openclaw",
            model=model,
            lock_wait_ms=getattr(exc, "lock_wait_ms", 0),
        )
    dur = int((time.time() - t0) * 1000)
    rate_limited = _is_quota_error(proc.stderr, proc.stdout)
    if proc.returncode != 0:
        _record_usage("openclaw", model, 0, dur, False, rate_limited)
        return CliResult(
            ok=False,
            error=(proc.stderr or proc.stdout)[:500],
            duration_ms=dur,
            backend="openclaw",
            model=model,
            rate_limited=rate_limited,
            lock_wait_ms=lock_wait_ms,
        )
    text, tokens = _parse_openclaw_payload(proc.stdout)
    _record_usage("openclaw", model, tokens, dur, bool(text), rate_limited)
    return CliResult(
        ok=bool(text),
        text=text,
        tokens=tokens,
        duration_ms=dur,
        backend="openclaw",
        model=model,
        rate_limited=rate_limited,
        lock_wait_ms=lock_wait_ms,
    )


def _try_backend(backend: str, model: str, prompt: str, timeout: int) -> CliResult:
    if backend == "codex":
        return _single_codex(prompt, model, timeout)
    if backend == "claude":
        return _single_claude(prompt, model, timeout)
    if backend == "openclaw":
        return _single_openclaw(prompt, model, timeout)
    return CliResult(ok=False, error=f"unknown backend {backend}", backend=backend, model=model)


def cli_dispatch(
    prompt: str,
    *,
    timeout: int = 30,
    backend: str | None = None,
    openclaw_agent: str | None = None,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
    max_backends: int | None = None,
    **_ignored: Any,
) -> CliResult:
    """Dispatch a stateless LLM call with full fallback chain.

    Walks FALLBACK_CHAIN in order, stopping at the first success. If every
    backend fails (or is rate-limited), optionally enqueues to llm_backlog
    so the work catches up when quota returns.

    Parameters
    ----------
    prompt          : raw user message (system prompt + task)
    timeout         : per-attempt seconds
    backend         : hint to PREFER a specific backend ('codex' | 'claude' | 'openclaw')
                      as the first attempt. Fallback chain still fires on
                      failure. None = use FALLBACK_CHAIN default order.
    backlog_kind    : optional llm_backlog kind. When provided, a total
                      failure enqueues the work with this kind for later
                      catch-up. One of: classify | entities | distill |
                      synthesis | proactive | telegram | reflect.
    backlog_payload : dict with enough context for the backlog handler to
                      re-run the work. Typically includes 'prompt' plus
                      any domain-specific fields.
    """
    # Pre-flight breaker check. If open, fast-fail to backlog instead of
    # burning the full chain timeout on a known-bad upstream. half_open allows
    # exactly one probe at a time; non-probing callers under half_open behave
    # the same as `open` (fast-fail) so we don't stampede the recovery probe.
    snapshot = _peek_breaker(BREAKER_KIND)
    if snapshot is not None and snapshot.blocks_new_callers:
        skipped = CliResult(
            ok=False,
            error=f"breaker_{snapshot.state} (cooldown {snapshot.remaining_cooldown_s:.0f}s)",
            backend="",
            model="",
            tried=[],
            attempts=0,
        )
        if backlog_kind:
            try:
                try:
                    from llm_backlog import enqueue as _backlog_enqueue
                except ModuleNotFoundError:
                    from brain_core.llm_backlog import enqueue as _backlog_enqueue
                final_payload = dict(backlog_payload or {})
                final_payload.setdefault("prompt", prompt)
                final_payload.setdefault("failure_reason", "breaker_open")
                if _backlog_enqueue(backlog_kind, final_payload):
                    skipped.backlogged = True
            except Exception as exc:
                log.warning("backlog enqueue failed during breaker fast-fail: %s", exc)
        return skipped

    # Half-open: claim the single-flight probe. If we don't get it, treat the
    # same as open — another caller is already probing.
    if snapshot is not None and snapshot.is_half_open and not _try_claim_probe(BREAKER_KIND):
        skipped = CliResult(
            ok=False,
            error="breaker_half_open_probe_in_flight",
            backend="",
            model="",
            tried=[],
            attempts=0,
        )
        if backlog_kind:
            try:
                try:
                    from llm_backlog import enqueue as _backlog_enqueue
                except ModuleNotFoundError:
                    from brain_core.llm_backlog import enqueue as _backlog_enqueue
                final_payload = dict(backlog_payload or {})
                final_payload.setdefault("prompt", prompt)
                final_payload.setdefault("failure_reason", "probe_in_flight")
                if _backlog_enqueue(backlog_kind, final_payload):
                    skipped.backlogged = True
            except Exception as exc:
                log.warning("backlog enqueue failed during probe-skip: %s", exc)
        return skipped

    # Build attempt order — optionally front-load a preferred backend
    chain = [(b, (openclaw_agent or m) if b == "openclaw" else m, d) for (b, m, d) in FALLBACK_CHAIN]
    if backend:
        preferred = [(b, m, d) for (b, m, d) in chain if b == backend]
        rest = [(b, m, d) for (b, m, d) in chain if b != backend]
        chain = preferred + rest
    if max_backends is not None:
        chain = chain[: max(1, int(max_backends))]

    result: CliResult | None = None
    last_real_result: CliResult | None = None
    tried: list[tuple[str, str]] = []
    real_attempts = 0
    cooldown_skips = 0

    for backend, model, _desc in chain:
        cooldown_remaining = _backend_cooldown_remaining(backend, model)
        if cooldown_remaining > 0:
            cooldown_skips += 1
            r = CliResult(
                ok=False,
                error=f"backend_cooldown {cooldown_remaining:.0f}s",
                backend=backend,
                model=model,
            )
        else:
            real_attempts += 1
            r = _try_backend(backend, model, prompt, timeout)
            _record_backend_outcome(r)
            last_real_result = r
        tried.append((backend, model))
        r.tried = tried
        r.attempts = len(tried)
        if r.ok:
            # Tell the breaker we're healthy. After an outage this is what
            # actually closes the breaker so other callers stop fast-failing.
            try:
                _record_breaker(BREAKER_KIND, ok=True)
            except Exception as exc:
                log.warning("breaker record_result(ok=True) failed: %s", exc)
            if len(tried) > 1:
                log.info(
                    "cli_dispatch succeeded on fallback %s/%s after %d attempts", backend, model, len(tried)
                )
            return r
        # Non-quota errors on primary backend: try next in chain anyway — a
        # transient CLI bug shouldn't block the entire dispatch.
        result = r
        if r.rate_limited:
            log.warning("cli_dispatch %s/%s rate-limited, trying next backend", backend, model)
        else:
            log.info("cli_dispatch %s/%s failed: %s — trying next backend", backend, model, r.error[:100])

    # All backends failed — tell the breaker so it can trip after threshold.
    # On a half-open probe, any failure escalates to the next backoff tier
    # (handled inside breakers.record_result).
    if real_attempts > 0:
        try:
            last_err = (
                (last_real_result or result).error if (last_real_result or result) else "all backends failed"
            )[:200]
            _record_breaker(BREAKER_KIND, ok=False, error=last_err)
        except Exception as exc:
            log.warning("breaker record_result(ok=False) failed: %s", exc)
    elif cooldown_skips:
        # Cooldown skips are protective throttling, not fresh upstream
        # failures. Counting them against the global breaker caused a loop:
        # backend cooldown -> synthetic dispatch failures -> breaker_open_count
        # SLO breach, even though no provider was actually called. Return a
        # backfillable failure to the caller, but do not open llm.dispatch.
        log.info("cli_dispatch skipped all %d backends because provider cooldowns are active", cooldown_skips)

    # All backends failed — enqueue to backlog if requested
    if result is None:
        result = CliResult(ok=False, error="no backends tried", tried=tried, attempts=0)

    if backlog_kind:
        try:
            # 2026-04-17 fix: callers from cli/ or pipeline/ use different
            # sys.path — bare `from llm_backlog` only works inside brain-server.
            # Try the in-server path first, fall back to the package path.
            try:
                from llm_backlog import enqueue as _backlog_enqueue
            except ModuleNotFoundError:
                from brain_core.llm_backlog import enqueue as _backlog_enqueue
            final_payload = dict(backlog_payload or {})
            final_payload.setdefault("prompt", prompt)
            final_payload.setdefault(
                "failure_reason",
                "rate_limited" if result.rate_limited else "exhausted",
            )
            if _backlog_enqueue(backlog_kind, final_payload):
                result.backlogged = True
                log.warning("all CLI backends exhausted — enqueued %s for catch-up", backlog_kind)
        except Exception as exc:
            log.warning("backlog enqueue failed: %s", exc)

    return result


def cli_dispatch_with_schema(
    prompt: str,
    schema_description: str,
    *,
    timeout: int = 30,
    max_parse_retries: int = 2,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
) -> dict | None:
    """Dispatch with strict-JSON retry logic. Returns parsed dict or None.

    Mirrors openclaw_dispatch.dispatch_with_schema API so call-sites in
    synthesis/*.py, pipeline/skill_extractor.py, etc. can swap in place.
    """
    schema_instruction = (
        f"\n\nYou MUST respond with strict JSON matching this schema:\n"
        f"{schema_description}\n\nNo prose. No markdown fences. Just the JSON object."
    )
    error_suffix = ""
    for _attempt in range(max_parse_retries + 1):
        full_message = prompt + schema_instruction + error_suffix
        result = cli_dispatch(
            full_message,
            timeout=timeout,
            backlog_kind=backlog_kind,
            backlog_payload=backlog_payload,
        )
        if not result.ok:
            return None
        text = result.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            error_suffix = (
                f"\n\nPrevious attempt failed with: JSON parse error: {e}. "
                "Respond again, strictly matching the schema. No prose. No fences."
            )
    return None


# ── Back-compat shim for call-sites using openclaw DispatchResult API ──
def dispatch_compat(
    agent: str,
    message: str,
    *,
    thinking: str = "low",
    timeout: int = 30,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
    max_backends: int | None = None,
    **_: Any,
) -> CliResult:
    """Drop-in replacement for openclaw_dispatch.dispatch. Ignores `agent`
    and `thinking` (CLI path doesn't need persona injection). Returns a
    CliResult which exposes the same .ok/.text/.error/.duration_ms/
    .provider/.model fields as DispatchResult.

    Migration pattern:
        from openclaw_dispatch import dispatch
        result = dispatch(agent="jenna", message=prompt, thinking="low", timeout=60)
    becomes:
        from cli_llm import dispatch_compat as dispatch
        result = dispatch(agent="jenna", message=prompt, thinking="low", timeout=60)

    (Or more cleanly: `from cli_llm import cli_dispatch`; drop the agent/thinking args.)
    """
    return cli_dispatch(
        message,
        timeout=timeout,
        openclaw_agent=agent,
        backlog_kind=backlog_kind,
        backlog_payload=backlog_payload,
        max_backends=max_backends,
    )


# ── Drop-in aliases for minimal-churn migration ────────────
# Call-sites using `from openclaw_dispatch import dispatch` can swap to
# `from cli_llm import dispatch` with zero other changes. `agent=` and
# `thinking=` are accepted but ignored (CLI doesn't need persona).
dispatch = dispatch_compat
DispatchResult = CliResult  # for call-sites that import the type


def dispatch_with_schema_compat(
    *args: Any,
    **kwargs: Any,
) -> dict | None:
    """Back-compat shim for callers using the openclaw signature.

    The original openclaw API was
    ``dispatch_with_schema(agent, message, schema_description, thinking,
    timeout, max_retries, backlog_kind, backlog_payload)``.
    The CLI path doesn't need ``agent`` or ``thinking`` (no persona
    routing), and renamed ``max_retries`` → ``max_parse_retries``. This
    wrapper accepts both shapes so migrated call-sites in synthesis/*.py
    and pipeline/*.py don't have to be touched individually.
    """
    # Extract prompt + schema_description from either positional or kwarg.
    if len(args) >= 2 and not {"prompt", "message", "schema_description"} & set(kwargs):
        # cli-native signature: (prompt, schema_description, ...)
        prompt = args[0]
        schema_description = args[1]
        extra_args = args[2:]
    else:
        prompt = kwargs.pop("message", None) or kwargs.pop("prompt", None)
        schema_description = kwargs.pop("schema_description", None)
        extra_args = args

    if prompt is None or schema_description is None:
        raise TypeError("dispatch_with_schema requires prompt/message and schema_description")

    # Silently drop openclaw-only kwargs.
    kwargs.pop("agent", None)
    kwargs.pop("thinking", None)
    # Translate legacy retry name.
    if "max_retries" in kwargs and "max_parse_retries" not in kwargs:
        kwargs["max_parse_retries"] = kwargs.pop("max_retries")
    else:
        kwargs.pop("max_retries", None)

    return cli_dispatch_with_schema(prompt, schema_description, *extra_args, **kwargs)


dispatch_with_schema = dispatch_with_schema_compat


if __name__ == "__main__":
    import sys

    test_prompt = sys.argv[1] if len(sys.argv) > 1 else "Answer in one word: capital of Japan"
    r = cli_dispatch(test_prompt)
    print(  # noqa: T201 — CLI debug entry point
        f"backend={r.backend} model={r.model} ok={r.ok} tokens={r.tokens} "
        f"dur_ms={r.duration_ms} attempts={r.attempts} tried={r.tried}"
    )
    print(f"text: {r.text[:300]}")  # noqa: T201
    if r.backlogged:
        print("(backlogged for later catch-up)")  # noqa: T201
    if not r.ok:
        print(f"error: {r.error[:300]}")  # noqa: T201
