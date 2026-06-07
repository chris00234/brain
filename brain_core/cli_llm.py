"""Subscription-backed LLM dispatch via Codex CLI with
comprehensive fallback chain and llm_backlog integration.

Replaces legacy profile dispatch for ALL brain mechanical calls. The
stateful agent path can drag large session history into every call. CLI is
stateless: ~5K tokens/call, 3-5x faster, cleaner output (no persona
pollution).

## Fallback chain (worst-case guaranteed catch-up)

    1. codex exec (gpt-5.5, ChatGPT Pro sub) — primary, 2-6s
    2. codex exec -m gpt-5.3-codex-spark — lighter fallback if primary hit quota
    3. hermes --profile <name> chat -q — authenticated profile fallback
    4. llm_backlog.enqueue — if every provider is exhausted, queue the work
       so it catches up automatically when quota resets

Rate-limit detection patterns match the legacy dispatch wrapper so behavior
stays consistent with the existing breaker.

## API compatibility with legacy dispatch wrappers

`CliResult` mirrors `DispatchResult` (ok, text, error, duration_ms,
provider, model) so call-sites can swap with minimal churn. Plus token
accounting for the new llm_daily_spend_usd SLO.

## When to use

- Use `cli_dispatch` for: HyDE, classify, atom compression, entity
  extraction, reflection, synthesis, SLO notifications — anything that
  does not need a Hermes profile persona, session continuity, or skills.
  Claude CLI prompt mode is intentionally not used; stale `backend="claude"`
  hints are treated as Codex gpt-5.5 compatibility aliases.
- Use `cli_dispatch_with_schema` for structured JSON output with retry.
- Keep profile dispatch for: skill-heavy profile turns (imsg/things/obsidian)
  and multi-agent messages.
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
from datetime import UTC, datetime, timedelta
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
CODEX_BIN = os.getenv("BRAIN_CODEX_BIN", "/opt/homebrew/bin/codex")
CODEX_PRIMARY_MODEL = os.getenv("BRAIN_CODEX_PRIMARY_MODEL", "gpt-5.5")
CODEX_FALLBACK_MODEL = os.getenv("BRAIN_CODEX_FALLBACK_MODEL", "gpt-5.3-codex-spark")
HERMES_BIN = os.getenv("BRAIN_HERMES_BIN", os.getenv("HERMES_BIN", "/Users/chrischo/.local/bin/hermes"))
HERMES_FALLBACK_PROFILE = os.getenv("BRAIN_HERMES_FALLBACK_PROFILE", "jenna")
HERMES_HOME = Path(os.getenv("BRAIN_HERMES_HOME", "/Users/chrischo/.hermes"))
HERMES_TIMEOUT_FLOOR_S = max(5, int(os.getenv("BRAIN_HERMES_TIMEOUT_FLOOR_S", "45")))
HERMES_TIMEOUT_CAP_S = max(HERMES_TIMEOUT_FLOOR_S, int(os.getenv("BRAIN_HERMES_TIMEOUT_CAP_S", "90")))
HERMES_FALLBACK_ENABLED = os.getenv("BRAIN_HERMES_FALLBACK_ENABLED", "1").lower() not in {
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
# preserves the original memory contract (cap on simultaneous Codex/Hermes
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


# ── Fallback chain (ordered by preference) ────────────────────
# Each entry: (backend, model, description)
FALLBACK_CHAIN: list[tuple[str, str, str]] = [
    ("codex", CODEX_PRIMARY_MODEL, "ChatGPT Pro primary — frontier quality"),
    ("codex", CODEX_FALLBACK_MODEL, "ChatGPT Pro lightweight — quota fallback"),
    ("hermes", HERMES_FALLBACK_PROFILE, "Hermes authenticated profile fallback"),
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
    input_text: str | None = None,
    use_slot: bool = True,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run a subscription CLI with a process-group timeout.

    `subprocess.run(..., timeout=...)` kills only the direct child. Codex/Hermes
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

    lock_wait_ms = 0
    lock_f = None
    if use_slot:
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
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        try:
            if input_text is None:
                stdout, stderr = proc.communicate(timeout=timeout)
            else:
                stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            new_exc = subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
            new_exc.lock_wait_ms = lock_wait_ms  # type: ignore[attr-defined]
            raise new_exc from exc
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr), lock_wait_ms
    finally:
        if lock_f is not None:
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

FAILURE_TAXONOMY_VERSION = "cli-failure-taxonomy-v1"
_PROVIDER_FAILURE_CLASSES = ("codex", "hermes")
_FAILURE_CLASS_PROBES = {
    "auth": "not logged in",
    "billing": "payment required",
    "model_not_found": "model not found",
    "context_overflow": "context length overflow",
    "rate_limit": "rate limit exceeded",
    "overloaded": "service unavailable 503",
    "unknown": "unclassified provider failure",
}


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


def failure_taxonomy_snapshot() -> dict:
    """Describe dispatch failure classes exposed through `/brain/usage`.

    The probes reuse ``classify_cli_error`` so docs/API/UI can show the same
    taxonomy used by backend cooldowns and fallback decisions. This is static
    and safe; it does not call any CLI backend.
    """

    classes = []
    for reason in FAILOVER_REASONS:
        classified = classify_cli_error(_FAILURE_CLASS_PROBES[reason], "")
        classes.append(
            {
                "reason": reason,
                "retryable": bool(classified["retryable"]),
                "should_fallback": bool(classified["should_fallback"]),
                "should_compress": bool(classified["should_compress"]),
                "should_rotate_credential": bool(classified["should_rotate_credential"]),
                "backend_cooldown_s": int(_BACKEND_COOLDOWN_S.get(reason, 0.0)),
            }
        )
    return {
        "version": FAILURE_TAXONOMY_VERSION,
        "provider_classes": list(_PROVIDER_FAILURE_CLASSES),
        "class_count": len(classes),
        "classes": classes,
        "dashboard_surface": "/brain/usage.llm.failure_taxonomy",
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

    # Compat shim with legacy DispatchResult
    @property
    def provider(self) -> str:
        if self.backend == "codex":
            return "openai-codex"
        if self.backend == "claude":
            return "anthropic"
        if self.backend == "hermes":
            return "hermes"
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
    the CLI dispatches alongside profile-dispatch calls. Cost is 0 because
    these are subscription-backed — token count is the real signal.
    """
    conn = None
    try:
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        # 2026-04-17 fix: audit showed that if the legacy profile-dispatch
        # module had never been
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


def get_usage_stats(days: int = 30) -> dict:
    """Return rolling CLI-first LLM usage stats for the last N days.

    This intentionally lives in ``cli_llm`` so `/brain/usage` reports the
    current mechanical-dispatch surface instead of the legacy profile wrapper.
    """
    try:
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS llm_usage ("
                "timestamp TEXT, agent TEXT, duration_ms INTEGER, ok INTEGER, "
                "prompt_tokens INTEGER, response_tokens INTEGER, skipped_cb INTEGER, "
                "provider TEXT, model TEXT, cache_read_tokens INTEGER, "
                "cache_write_tokens INTEGER, cost_usd REAL)"
            )
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            total = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0",
                (cutoff,),
            ).fetchone()[0]
            today_cutoff = datetime.now(UTC).strftime("%Y-%m-%d")
            today_count = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0",
                (today_cutoff,),
            ).fetchone()[0]
            per_agent = dict(
                conn.execute(
                    "SELECT agent, COUNT(*) FROM llm_usage "
                    "WHERE timestamp >= ? AND skipped_cb = 0 GROUP BY agent",
                    (cutoff,),
                ).fetchall()
            )
            per_backend = dict(
                conn.execute(
                    "SELECT provider, COUNT(*) FROM llm_usage "
                    "WHERE timestamp >= ? AND skipped_cb = 0 GROUP BY provider",
                    (cutoff,),
                ).fetchall()
            )
            cb_skipped = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 1",
                (cutoff,),
            ).fetchone()[0]
            tokens = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens + response_tokens + cache_read_tokens + cache_write_tokens), 0) "
                "FROM llm_usage WHERE timestamp >= ?",
                (cutoff,),
            ).fetchone()[0]
            return {
                "total": int(total),
                "today": int(today_count),
                "per_agent": per_agent,
                "per_backend": per_backend,
                "cb_skipped": int(cb_skipped),
                "tokens": int(tokens or 0),
                "days": days,
                "source": "cli_llm",
                "primary_model": FALLBACK_CHAIN[0][1],
                "failure_taxonomy": failure_taxonomy_snapshot(),
            }
        finally:
            conn.close()
    except Exception as exc:
        log.debug("cli_llm get_usage_stats failed: %s", exc)
        return {
            "error": str(exc)[:200],
            "total": 0,
            "today": 0,
            "per_agent": {},
            "per_backend": {},
            "cb_skipped": 0,
            "tokens": 0,
            "days": days,
            "source": "cli_llm",
            "primary_model": FALLBACK_CHAIN[0][1],
            "failure_taxonomy": failure_taxonomy_snapshot(),
        }


def _is_quota_error(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}"
    return any(p.search(blob) for p in _QUOTA_PATTERNS)


def _cooldown_reason(result: CliResult) -> str:
    if _is_local_capacity_error(result.error):
        return "local_capacity"
    if result.rate_limited:
        return "rate_limit"
    if "timeout" in (result.error or "").lower():
        return "timeout"
    classified = classify_cli_error(result.error, "")
    return str(classified.get("reason") or "unknown")


def _is_local_capacity_error(error: str | None) -> bool:
    err = (error or "").lower()
    return "cli slots busy" in err or ("slot" in err and "busy" in err)


def _is_transient_throttle_error(error: str | None) -> bool:
    """Errors that already have a local/provider retry path.

    These should not trip the coarse global ``llm.dispatch`` breaker. Backend
    cooldowns, task deferral, and Hermes profile recovery handle them more
    precisely. Counting them globally was reopening the breaker from local
    gateway/timeouts and making Chris see "automation under the hood" that was
    only waiting on a broad cooldown.
    """
    err = (error or "").lower()
    return (
        _is_local_capacity_error(err)
        or "backend_cooldown" in err
        or "rate limit" in err
        or "rate_limit" in err
        or "rate-limited" in err
        or "usage limit" in err
        or "timeout" in err
        or "gatewaytransporterror" in err
        or "gateway closed" in err
        or "gateway crashed" in err
        or "process timeout" in err
    )


def _timeout_error(exc: subprocess.TimeoutExpired, timeout: int) -> str:
    """Return a truthful timeout error even when the CLI wrote a startup banner.

    Codex writes session/model metadata to stderr before any real answer. When
    a process timed out, storing only that stderr banner made breaker reasons
    look like a normal Codex invocation instead of the real failure.
    """
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    stdout = exc.output if isinstance(exc.output, str) else ""
    detail = (stderr or stdout or "").strip()
    if detail:
        return f"timeout after {timeout}s: {detail[:450]}"
    return f"timeout after {timeout}s"


def _empty_response_error(backend: str, stderr: str, stdout: str, *, rate_limited: bool) -> str:
    detail = (stderr or stdout or "").strip()
    if detail:
        base = f"{backend} returned empty response: {detail[:450]}"
    else:
        base = f"{backend} returned empty response"
    if rate_limited and "rate" not in base.lower() and "usage limit" not in base.lower():
        return f"rate_limited: {base}"
    return base


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
    fall through to the next Codex/Hermes fallback.
    """
    t0 = time.time()
    # Feed the prompt through stdin (`-`) instead of argv. Codex v0.128 can
    # return only its "Reading additional input from stdin..." banner when a
    # long prompt is passed positionally from a non-TTY launchd process. Stdin
    # is also safer for large task prompts.
    cmd = [CODEX_BIN, "exec", "--skip-git-repo-check", "-m", model, "-"]
    try:
        proc, lock_wait_ms = _run_cli_process(cmd, timeout=timeout, input_text=prompt)
    except subprocess.TimeoutExpired as exc:
        dur = int((time.time() - t0) * 1000)
        _record_usage("codex", model, 0, dur, False)
        err = _timeout_error(exc, timeout)[:500]
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
        error=(
            ""
            if text
            else _empty_response_error("codex", proc.stderr, proc.stdout, rate_limited=rate_limited)
        ),
        tokens=tokens,
        duration_ms=dur,
        backend="codex",
        model=model,
        rate_limited=rate_limited,
        lock_wait_ms=lock_wait_ms,
    )


def _legacy_claude_backend_via_codex(prompt: str, model: str, timeout: int) -> CliResult:
    """Compatibility shim for stale callers that still request Claude.

    Chris moved Claude prompt-mode CLI execution to Codex so the
    brain can use gpt-5.5 consistently.  Do not invoke Claude CLI here; route
    the work through the Codex primary model and let the returned result report
    its real backend/model.
    """

    return _single_codex(prompt, CODEX_PRIMARY_MODEL, timeout)


def _parse_hermes_payload(stdout: str) -> tuple[str, int]:
    raw = (stdout or "").strip()
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        # Hermes may print a fallback banner before the JSON envelope.
        # Parse the first plausible envelope instead of treating the banner as
        # the model answer.
        start = raw.find('{\n  "payloads"')
        if start < 0:
            start = raw.find('{"payloads"')
        if start >= 0:
            try:
                envelope = json.loads(raw[start:])
            except json.JSONDecodeError:
                return raw, 0
        else:
            return raw, 0
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
    payloads = result.get("payloads") or []
    text = "\n".join(str(p.get("text") or "").strip() for p in payloads if p.get("text")).strip()
    usage = (((result.get("meta") or {}).get("agentMeta") or {}).get("usage")) or {}
    tokens = int(usage.get("total") or usage.get("input") or 0)
    return text, tokens


_HERMES_SESSION_ID_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")


def _hermes_profile_state_db(profile: str) -> Path:
    return HERMES_HOME / "profiles" / profile / "state.db"


def _hermes_session_id_from_stderr(stderr: str) -> str | None:
    match = _HERMES_SESSION_ID_RE.search(stderr or "")
    return match.group(1) if match else None


def _recover_hermes_assistant_from_state(profile: str, session_id: str) -> str:
    """Read the saved Hermes assistant answer for a completed one-shot session.

    Hermes sometimes persists the final assistant message to the profile state
    DB while the parent process receives empty stdout. Recovery is intentionally
    narrow: exact profile + exact session id + latest non-empty assistant row.
    Missing DB/schema/row returns empty so the original failure path remains.
    """

    state_db = _hermes_profile_state_db(profile)
    if not state_db.exists():
        return ""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=1.0)
        row = conn.execute(
            """
            SELECT content
            FROM messages
            WHERE session_id = ?
              AND role = 'assistant'
              AND content IS NOT NULL
              AND length(trim(content)) > 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        log.debug(
            "hermes state recovery failed for profile=%s session_id=%s: %s",
            profile,
            session_id,
            exc,
        )
        return ""
    finally:
        if conn is not None:
            conn.close()
    return str(row[0]).strip() if row and row[0] else ""


def _single_hermes(prompt: str, model: str, timeout: int, *, session_id: str | None = None) -> CliResult:
    """Emergency fallback through a Hermes authenticated profile path.

    Last in the chain: Codex stays the stateless primary; Hermes profile chat is
    available when subscription CLIs are logged out or quota-exhausted.
    """

    if not HERMES_FALLBACK_ENABLED:
        return CliResult(ok=False, error="hermes fallback disabled", backend="hermes", model=model)
    t0 = time.time()
    hermes_timeout = max(HERMES_TIMEOUT_FLOOR_S, min(timeout, HERMES_TIMEOUT_CAP_S))
    cmd = [
        HERMES_BIN,
        "--profile",
        model or HERMES_FALLBACK_PROFILE,
        "chat",
        "-q",
        prompt,
        "--quiet",
        "--source",
        "brain-cli-llm",
    ]
    if session_id:
        # Current Hermes one-shot chat is stateless; keep the compatibility
        # argument accepted but intentionally unused.
        pass
    try:
        # Foreground profile tasks must not lose to background learning jobs
        # that briefly hold the shared CLI slot. Hermes has its own timeout
        # budget; allow a longer lock wait so queued Liz/Ellie/Sage work waits
        # instead of becoming a false task failure/defer.
        hermes_lock_wait_s = max(DEFAULT_LOCK_WAIT_S, min(float(timeout), 180.0))
        proc, lock_wait_ms = _run_cli_process(
            cmd,
            timeout=max(hermes_timeout + 10, 20),
            lock_wait_s=hermes_lock_wait_s,
            use_slot=False,
        )
    except subprocess.TimeoutExpired as exc:
        dur = int((time.time() - t0) * 1000)
        _record_usage("hermes", model, 0, dur, False)
        err = _timeout_error(exc, max(hermes_timeout + 10, 20))[:500]
        return CliResult(
            ok=False,
            error=err,
            duration_ms=dur,
            backend="hermes",
            model=model,
            lock_wait_ms=getattr(exc, "lock_wait_ms", 0),
        )
    dur = int((time.time() - t0) * 1000)
    rate_limited = _is_quota_error(proc.stderr, proc.stdout)
    if proc.returncode != 0:
        _record_usage("hermes", model, 0, dur, False, rate_limited)
        return CliResult(
            ok=False,
            error=(proc.stderr or proc.stdout)[:500],
            duration_ms=dur,
            backend="hermes",
            model=model,
            rate_limited=rate_limited,
            lock_wait_ms=lock_wait_ms,
        )
    text, tokens = _parse_hermes_payload(proc.stdout)
    recovered_from_state = False
    if not text:
        recovered_session_id = _hermes_session_id_from_stderr(proc.stderr)
        if recovered_session_id:
            text = _recover_hermes_assistant_from_state(
                model or HERMES_FALLBACK_PROFILE, recovered_session_id
            )
            recovered_from_state = bool(text)
    if text:
        # Hermes can print a fallback warning while still returning a
        # valid embedded-agent answer. A successful answer must not poison the
        # backend cooldown state as a rate limit.
        rate_limited = False
        if recovered_from_state:
            log.info("recovered hermes assistant response from profile state db")
    _record_usage("hermes", model, tokens, dur, bool(text), rate_limited)
    return CliResult(
        ok=bool(text),
        text=text,
        error=(
            ""
            if text
            else _empty_response_error("hermes", proc.stderr, proc.stdout, rate_limited=rate_limited)
        ),
        tokens=tokens,
        duration_ms=dur,
        backend="hermes",
        model=model,
        rate_limited=rate_limited,
        lock_wait_ms=lock_wait_ms,
    )


def _try_backend(
    backend: str,
    model: str,
    prompt: str,
    timeout: int,
    *,
    hermes_session_id: str | None = None,
) -> CliResult:
    if backend == "codex":
        return _single_codex(prompt, model, timeout)
    if backend == "claude":
        return _legacy_claude_backend_via_codex(prompt, model, timeout)
    if backend == "hermes":
        return _single_hermes(prompt, model, timeout, session_id=hermes_session_id)
    return CliResult(ok=False, error=f"unknown backend {backend}", backend=backend, model=model)


def cli_dispatch(
    prompt: str,
    *,
    timeout: int = 30,
    backend: str | None = None,
    allow_hermes_fallback: bool = True,
    hermes_profile: str | None = None,
    hermes_session_id: str | None = None,
    allow_openclaw_fallback: bool | None = None,
    openclaw_agent: str | None = None,
    openclaw_session_id: str | None = None,
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
    backend         : hint to PREFER a specific backend ('codex' | 'hermes').
                      Legacy 'claude' hints are normalized to Codex gpt-5.5.
                      Fallback chain still fires on
                      failure. None = use FALLBACK_CHAIN default order.
    allow_hermes_fallback
                    : when False, constrain the fallback chain to stateless
                      subscription CLI path (Codex) and never invoke the
                      persona/session-heavy Hermes emergency lane.
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
    half_open_probe_claimed = False
    if snapshot is not None and snapshot.is_half_open:
        half_open_probe_claimed = _try_claim_probe(BREAKER_KIND)
    if snapshot is not None and snapshot.is_half_open and not half_open_probe_claimed:
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

    # Build attempt order — optionally front-load a preferred backend.
    # Compatibility: old callers may still pass backend="claude"; route those
    # through Codex instead of spawning Claude prompt-mode CLI.
    if backend == "claude":
        backend = "codex"
    if allow_openclaw_fallback is not None:
        allow_hermes_fallback = allow_openclaw_fallback
    if openclaw_agent and not hermes_profile:
        hermes_profile = openclaw_agent
    if openclaw_session_id and not hermes_session_id:
        hermes_session_id = openclaw_session_id

    chain = [(b, (hermes_profile or m) if b == "hermes" else m, d) for (b, m, d) in FALLBACK_CHAIN]
    if not allow_hermes_fallback:
        chain = [(b, m, d) for (b, m, d) in chain if b != "hermes"]
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
            if hermes_session_id:
                r = _try_backend(
                    backend,
                    model,
                    prompt,
                    timeout,
                    hermes_session_id=hermes_session_id,
                )
            else:
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
            if _is_transient_throttle_error(last_err):
                log.info(
                    "cli_dispatch transient/provider throttle (%s); not tripping global LLM breaker",
                    last_err[:100],
                )
            else:
                _record_breaker(BREAKER_KIND, ok=False, error=last_err)
        except Exception as exc:
            log.warning("breaker record_result(ok=False) failed: %s", exc)
    elif cooldown_skips:
        # Cooldown skips are protective throttling, not fresh upstream
        # failures. Counting them against the global breaker caused a loop:
        # backend cooldown -> synthetic dispatch failures -> breaker_open_count
        # SLO breach, even though no provider was actually called. Return a
        # backfillable failure to the caller, but do not open llm.dispatch.
        # Exception: once this caller has claimed a half-open probe, it must
        # resolve the single-flight state. If every backend is skipped by
        # cooldown, the recovery probe could not run, so re-open with bounded
        # cooldown instead of stranding half_open_probing until stale cleanup.
        if half_open_probe_claimed:
            try:
                _record_breaker(
                    BREAKER_KIND,
                    ok=False,
                    error="half_open_probe_blocked_by_backend_cooldown",
                )
            except Exception as exc:
                log.warning("breaker record_result(ok=False) failed after cooldown-only probe: %s", exc)
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

    Mirrors the legacy dispatch_with_schema API so call-sites in
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


# ── Back-compat shim for call-sites using legacy DispatchResult API ──
def dispatch_compat(
    agent: str,
    message: str,
    *,
    backend: str | None = None,
    thinking: str = "low",
    timeout: int = 30,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
    max_backends: int | None = None,
    openclaw_session_id: str | None = None,
    **_: Any,
) -> CliResult:
    """Drop-in replacement for legacy profile dispatch. Ignores `thinking`.
    Uses `agent` as a Hermes profile hint when profile fallback is reached. Returns a
    CliResult which exposes the same .ok/.text/.error/.duration_ms/
    .provider/.model fields as DispatchResult.

    Migration pattern: legacy callers should import this shim as ``dispatch``
    instead, or more cleanly use ``cli_dispatch`` and drop persona-only args.
    """
    return cli_dispatch(
        message,
        timeout=timeout,
        backend=backend,
        hermes_profile=agent,
        hermes_session_id=openclaw_session_id,
        backlog_kind=backlog_kind,
        backlog_payload=backlog_payload,
        max_backends=max_backends,
    )


# ── Drop-in aliases for minimal-churn migration ────────────
# Legacy profile-dispatch call sites can swap to this module with zero other
# changes. `agent=` and `thinking=` are accepted but ignored (CLI does not
# need persona).
dispatch = dispatch_compat
DispatchResult = CliResult  # for call-sites that import the type


def dispatch_with_schema_compat(
    *args: Any,
    **kwargs: Any,
) -> dict | None:
    """Back-compat shim for callers using the legacy profile-dispatch signature.

    The original profile-dispatch API was
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

    # Silently drop profile-dispatch-only kwargs.
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
