"""Subscription-backed LLM dispatch via codex CLI + claude CLI with
comprehensive fallback chain and llm_backlog integration.

Replaces openclaw_dispatch.dispatch for ALL brain mechanical calls. The
OpenClaw path drags a 95MB session history into every call (414K tokens
per simple query). CLI is stateless: ~5K tokens/call, 3-5x faster, cleaner
output (no agent persona pollution).

## Fallback chain (worst-case guaranteed catch-up)

    1. codex exec (gpt-5.4, ChatGPT Pro sub) — primary, 2-6s
    2. codex exec -m gpt-5.3-codex-spark — lighter fallback if primary hit quota
    3. claude -p --model sonnet (Claude Max x20 sub) — provider-level fallback
    4. llm_backlog.enqueue — if every provider is exhausted, queue the work
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

import json
import logging
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.cli_llm")

# 2026-04-17 — first-failure flag so llm_usage write bugs surface once in logs
# rather than silently losing every CLI dispatch's telemetry forever.
_usage_warned = False

LLM_USAGE_DB = Path("/Users/chrischo/server/brain/logs/llm_usage.db")
CODEX_BIN = "/opt/homebrew/bin/codex"
CLAUDE_BIN = "/Users/chrischo/.local/bin/claude"

# ── Fallback chain (ordered by preference) ────────────────────
# Each entry: (backend, model, description)
FALLBACK_CHAIN: list[tuple[str, str, str]] = [
    ("codex", "gpt-5.4", "ChatGPT Pro primary — frontier quality"),
    ("codex", "gpt-5.3-codex-spark", "ChatGPT Pro lightweight — quota fallback"),
    ("claude", "sonnet", "Claude Max x20 — provider fallback"),
]

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

    # Compat shim with openclaw_dispatch.DispatchResult
    @property
    def provider(self) -> str:
        return "openai-codex" if self.backend == "codex" else "anthropic"


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
                datetime.now().isoformat(),
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
        conn.close()
    except sqlite3.Error as exc:
        global _usage_warned
        if not _usage_warned:
            log.warning("llm_usage write failed (suppressing further): %s", exc)
            _usage_warned = True


def _is_quota_error(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}"
    return any(p.search(blob) for p in _QUOTA_PATTERNS)


def _parse_codex(stdout: str, stderr: str) -> tuple[str, int]:
    """Non-TTY codex separates output cleanly:
    - stdout: response text only
    - stderr: metadata (session id, model, tokens used)
    """
    text = stdout.strip()
    tokens = 0
    m = _CODEX_TOKEN_RE.search(stderr or "") or _CODEX_TOKEN_RE.search(stdout)
    if m:
        try:
            tokens = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        dur = int((time.time() - t0) * 1000)
        _record_usage("codex", model, 0, dur, False)
        return CliResult(ok=False, error="timeout", duration_ms=dur, backend="codex", model=model)
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
    )


def _single_claude(prompt: str, model: str, timeout: int) -> CliResult:
    """One claude -p attempt with a specific model (haiku/sonnet/opus)."""
    t0 = time.time()
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", model, "--no-session-persistence"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        dur = int((time.time() - t0) * 1000)
        _record_usage("claude", model, 0, dur, False)
        return CliResult(ok=False, error="timeout", duration_ms=dur, backend="claude", model=model)
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
    )


def _try_backend(backend: str, model: str, prompt: str, timeout: int) -> CliResult:
    if backend == "codex":
        return _single_codex(prompt, model, timeout)
    return _single_claude(prompt, model, timeout)


def cli_dispatch(
    prompt: str,
    *,
    timeout: int = 60,
    backend: str | None = None,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
    **_ignored,
) -> CliResult:
    """Dispatch a stateless LLM call with full fallback chain.

    Walks FALLBACK_CHAIN in order, stopping at the first success. If every
    backend fails (or is rate-limited), optionally enqueues to llm_backlog
    so the work catches up when quota returns.

    Parameters
    ----------
    prompt          : raw user message (system prompt + task)
    timeout         : per-attempt seconds
    backend         : hint to PREFER a specific backend ('codex' | 'claude')
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
    # Build attempt order — optionally front-load a preferred backend
    chain = list(FALLBACK_CHAIN)
    if backend:
        preferred = [(b, m, d) for (b, m, d) in chain if b == backend]
        rest = [(b, m, d) for (b, m, d) in chain if b != backend]
        chain = preferred + rest

    result: CliResult | None = None
    tried: list[tuple[str, str]] = []

    for backend, model, desc in chain:
        r = _try_backend(backend, model, prompt, timeout)
        tried.append((backend, model))
        r.tried = tried
        r.attempts = len(tried)
        if r.ok:
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
    timeout: int = 60,
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
    for attempt in range(max_parse_retries + 1):
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
    timeout: int = 60,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
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
        backlog_kind=backlog_kind,
        backlog_payload=backlog_payload,
    )


# ── Drop-in aliases for minimal-churn migration ────────────
# Call-sites using `from openclaw_dispatch import dispatch` can swap to
# `from cli_llm import dispatch` with zero other changes. `agent=` and
# `thinking=` are accepted but ignored (CLI doesn't need persona).
dispatch = dispatch_compat
dispatch_with_schema = cli_dispatch_with_schema
DispatchResult = CliResult  # for call-sites that import the type


if __name__ == "__main__":
    import sys

    test_prompt = sys.argv[1] if len(sys.argv) > 1 else "Answer in one word: capital of Japan"
    r = cli_dispatch(test_prompt)
    print(
        f"backend={r.backend} model={r.model} ok={r.ok} tokens={r.tokens} "
        f"dur_ms={r.duration_ms} attempts={r.attempts} tried={r.tried}"
    )
    print(f"text: {r.text[:300]}")
    if r.backlogged:
        print("(backlogged for later catch-up)")
    if not r.ok:
        print(f"error: {r.error[:300]}")
