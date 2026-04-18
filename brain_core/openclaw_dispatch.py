"""brain_core/openclaw_dispatch.py — resilient wrapper around `openclaw agent`.

Every LLM call in the brain goes through this module. Features:

  1. Exponential backoff retry (3 attempts: 30s, 60s, 120s)
  2. Rate-limit detection from stderr/stdout patterns
  3. Degraded-mode fallback — callers get a structured placeholder
     reply so scheduled jobs never "lose a day" because of a single
     rate-limit cascade
  4. Structured failure log to brain/logs/dispatch-failures.jsonl
  5. Consistent OpenClaw JSON envelope parsing

Usage:
    from openclaw_dispatch import dispatch, DispatchResult

    result = dispatch(
        agent="jenna",
        message=prompt,
        thinking="low",
        timeout=60,
    )
    if result.ok:
        use(result.text)
    else:
        log(f"dispatch failed: {result.error}")
        # result.degraded is a best-effort placeholder the caller can persist
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import sys
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.dispatch")

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT CACHING CONVENTION (for OpenAI prompt caching via OpenClaw gateway)
# ─────────────────────────────────────────────────────────────────────────────
# OpenAI offers 50% discount on cached prompt prefixes (>=1024 tokens).
# To maximize cache hits, structure dispatch messages so stable content comes
# first (system prompt, agent identity, fixed rules) and variable content last
# (the user query / task).
#
# Convention for brain dispatches:
#   1. Agent identity + banned behaviors (from SOUL.md) — stable, cacheable
#   2. Tool definitions (from TOOLS.md) — stable, cacheable
#   3. Working context (task-specific) — variable, NOT cached
#   4. The user query — variable, NOT cached
#
# The actual prompt caching happens in the OpenClaw gateway (not brain).
# This convention ensures the brain's messages are compatible with gateway-side
# caching. See: https://platform.openai.com/docs/guides/prompt-caching
#
# For schema-based dispatches via dispatch_with_schema():
#   - schema_instruction (stable across calls) comes AFTER the query, so it
#     doesn't benefit from caching. This is acceptable because schema_instruction
#     is short (~100 tokens).
# ─────────────────────────────────────────────────────────────────────────────

try:
    from config import BRAIN_DISPATCH_CACHE_ENABLED, BRAIN_LOGS_DIR, OPENCLAW_BIN

    FAILURE_LOG = BRAIN_LOGS_DIR / "dispatch-failures.jsonl"
except ImportError:
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
    FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/dispatch-failures.jsonl")
    BRAIN_DISPATCH_CACHE_ENABLED = False

LLM_USAGE_DB = Path("/Users/chrischo/server/brain/logs/llm_usage.db")


def _ensure_usage_schema(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            timestamp TEXT NOT NULL,
            agent TEXT NOT NULL,
            duration_ms INTEGER,
            ok INTEGER,
            prompt_tokens INTEGER,
            response_tokens INTEGER,
            skipped_cb INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON llm_usage(timestamp)")
    # Idempotent ALTER TABLE for columns added post-initial schema.
    # Each ADD COLUMN is in its own try so one pre-existing column doesn't
    # block later ones. v3 (2026-04-14) adds the provider/model/cache/cost
    # columns needed for real metering.
    for col, col_type in (
        ("skipped_cb", "INTEGER DEFAULT 0"),
        ("provider", "TEXT NOT NULL DEFAULT ''"),
        ("model", "TEXT NOT NULL DEFAULT ''"),
        ("cache_read_tokens", "INTEGER DEFAULT 0"),
        ("cache_write_tokens", "INTEGER DEFAULT 0"),
        ("cost_usd", "REAL DEFAULT 0.0"),
    ):
        try:
            conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {col_type}")
        except Exception:
            pass  # column already exists


def _record_usage(
    agent: str,
    duration_ms: int,
    ok: bool,
    prompt_tokens: int = 0,
    response_tokens: int = 0,
    skipped_cb: bool = False,
    *,
    provider: str = "",
    model: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
):
    """Record a dispatch to SQLite for budget monitoring.

    v3 (2026-04-14): was recording tokens=0 and no cost because callers
    didn't pass usage from the envelope. Now extracts from envelope.result
    .meta.agentMeta.usage and computes cost via _estimate_cost_usd. Adds
    provider/model/cache_read_tokens/cache_write_tokens/cost_usd columns
    via idempotent ALTER TABLE.
    """
    try:
        LLM_USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        try:
            _ensure_usage_schema(conn)
            conn.execute(
                "INSERT INTO llm_usage (timestamp, agent, duration_ms, ok, "
                "prompt_tokens, response_tokens, skipped_cb, "
                "provider, model, cache_read_tokens, cache_write_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _dt.now().isoformat(),
                    agent,
                    duration_ms,
                    1 if ok else 0,
                    prompt_tokens,
                    response_tokens,
                    1 if skipped_cb else 0,
                    provider,
                    model,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("_record_usage failed: %s", e)


def get_usage_stats(days: int = 30) -> dict:
    """Return rolling usage stats for the last N days."""
    try:
        LLM_USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        try:
            _ensure_usage_schema(conn)
            cutoff = (_dt.now() - timedelta(days=days)).isoformat()
            total = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0", (cutoff,)
            ).fetchone()[0]
            per_agent = dict(
                conn.execute(
                    "SELECT agent, COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0 GROUP BY agent",
                    (cutoff,),
                ).fetchall()
            )
            today_cutoff = _dt.now().strftime("%Y-%m-%d")
            today_count = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0", (today_cutoff,)
            ).fetchone()[0]
            cb_skipped = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 1", (cutoff,)
            ).fetchone()[0]
            return {
                "total": total,
                "today": today_count,
                "per_agent": per_agent,
                "cb_skipped": cb_skipped,
                "days": days,
            }
        finally:
            conn.close()
    except Exception as e:
        log.debug("get_usage_stats failed: %s", e)
        return {"error": str(e), "total": 0, "today": 0, "per_agent": {}, "cb_skipped": 0, "days": days}


def purge_old_usage(days: int = 90) -> int:
    """Delete usage records older than N days. Returns number of rows deleted."""
    try:
        LLM_USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        try:
            _ensure_usage_schema(conn)
            cutoff = (_dt.now() - timedelta(days=days)).isoformat()
            cur = conn.execute("DELETE FROM llm_usage WHERE timestamp < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        log.debug("purge_old_usage failed: %s", e)
        return 0


# ── Circuit breaker ──────────────────────────────────────
_cb_failures = 0
_cb_open_until = 0.0
_CB_THRESHOLD = 3
_CB_COOLDOWN = 300  # 5 minutes
_cb_lock = threading.Lock()

# ── Semantic dispatch cache (opt-in via BRAIN_DISPATCH_CACHE_ENABLED) ─────
# Cache LLM responses by prompt embedding similarity — when a near-identical
# prompt is dispatched within 5 minutes, return the cached response instead
# of calling Jenna/OpenAI. OFF by default; enable after testing.
_dispatch_cache: list[tuple[float, list[float], str, str]] = []  # (ts, embedding, prompt, response_text)
_DISPATCH_CACHE_TTL = 300  # 5 min
_DISPATCH_CACHE_MAX = 100
_DISPATCH_SIM_THRESHOLD = 0.95
_dispatch_cache_lock = threading.Lock()


def _dispatch_cache_embed(message: str) -> list[float] | None:
    """Compute a query-prefix embedding for the dispatch message. Returns None on failure."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from search import get_embedding

        return get_embedding(message[:500])
    except Exception:
        return None


def _dispatch_cache_lookup(message: str) -> str | None:
    """Check semantic cache for near-identical prompts."""
    emb = _dispatch_cache_embed(message)
    if not emb:
        return None
    import math

    now = _time.monotonic()
    with _dispatch_cache_lock:
        # Evict expired
        _dispatch_cache[:] = [e for e in _dispatch_cache if now - e[0] < _DISPATCH_CACHE_TTL]
        for _ts, cached_emb, _cached_msg, resp in _dispatch_cache:
            dot = sum(a * b for a, b in zip(emb, cached_emb, strict=False))
            na = math.sqrt(sum(x * x for x in emb))
            nb = math.sqrt(sum(x * x for x in cached_emb))
            sim = dot / (na * nb) if na and nb else 0.0
            if sim > _DISPATCH_SIM_THRESHOLD:
                return resp
    return None


def _dispatch_cache_put(message: str, response_text: str) -> None:
    """Store response in semantic cache for future near-duplicate prompts."""
    if not response_text:
        return
    emb = _dispatch_cache_embed(message)
    if not emb:
        return
    with _dispatch_cache_lock:
        _dispatch_cache.append((_time.monotonic(), emb, message, response_text))
        if len(_dispatch_cache) > _DISPATCH_CACHE_MAX:
            _dispatch_cache.pop(0)


RETRY_DELAYS_SECONDS = (15, 30)  # 2 retries (was 3) — keeps total wall time under 3 min
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS) + 1  # 3 attempts total
MAX_TOTAL_SECONDS = 180  # hard cap — abort if total time exceeds 3 minutes

# Rate-limit + auth-failure patterns we recognize.
RATE_LIMIT_PATTERNS = [
    re.compile(r"rate[_ ]?limit", re.IGNORECASE),
    re.compile(r"quota.*exceed", re.IGNORECASE),
    re.compile(r"out of.*usage", re.IGNORECASE),
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
]
AUTH_PATTERNS = [
    re.compile(r"refresh_token_reused", re.IGNORECASE),
    re.compile(r"invalid.*token", re.IGNORECASE),
    re.compile(r"401.*unauthorized", re.IGNORECASE),
    re.compile(r"missing scope", re.IGNORECASE),
]

# ── Active learning: per-agent struggle detection ────────
_agent_stats: dict[str, dict] = {}
_agent_stats_lock = threading.Lock()


def _update_agent_stats(agent: str, duration_ms: int, ok: bool, attempts: int):
    """Track per-agent dispatch patterns for active learning."""
    with _agent_stats_lock:
        s = _agent_stats.setdefault(
            agent,
            {
                "durations": [],
                "failures": 0,
                "total": 0,
                "last_struggle_logged": 0.0,
            },
        )
        s["total"] += 1
        s["durations"].append(duration_ms)
        if len(s["durations"]) > 50:
            s["durations"] = s["durations"][-50:]
        if not ok:
            s["failures"] += 1


def _check_struggle(agent: str, message: str, duration_ms: int, ok: bool, attempts: int):
    """Detect struggle patterns and record as failure lesson."""
    with _agent_stats_lock:
        s = _agent_stats.get(agent)
        if not s or len(s["durations"]) < 5:
            return  # not enough data

        # Rate-limit struggle recording to once per hour per agent
        now = _time.monotonic()
        if now - s["last_struggle_logged"] < 3600:
            return

        # Compute median duration
        sorted_durations = sorted(s["durations"])
        median = sorted_durations[len(sorted_durations) // 2]

        signals = []
        if duration_ms > 2 * median and median > 1000:
            signals.append(f"duration {duration_ms}ms is 2x median {median}ms")
        if attempts >= 3:
            signals.append(f"{attempts} retries needed")
        if not ok and s["failures"] > 3:
            recent_failure_rate = s["failures"] / s["total"]
            if recent_failure_rate > 0.3:
                signals.append(f"{recent_failure_rate*100:.0f}% recent failure rate")

        if not signals:
            return

        # Record lesson via failure_memory
        s["last_struggle_logged"] = now

        try:
            from failure_memory import record_failure_lesson

            record_failure_lesson(
                task_description=message[:300],
                failure_reason="; ".join(signals),
                agent_id=agent,
                context="Struggle signal detected automatically",
            )
        except Exception as e:
            log.debug("record_failure_lesson failed: %s", e)


@dataclass
class DispatchResult:
    """Outcome of an openclaw dispatch call."""

    ok: bool
    text: str = ""
    error: str = ""
    attempts: int = 0
    duration_ms: int = 0
    provider: str = ""
    model: str = ""
    envelope: dict[str, Any] = field(default_factory=dict)
    degraded: str = ""
    rate_limited: bool = False
    auth_failed: bool = False


def _log_failure(entry: dict[str, Any]) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now().isoformat(timespec="seconds"), **entry}
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # never let logging break dispatch


def _classify_error(stderr: str, stdout: str) -> tuple[bool, bool]:
    """Return (rate_limited, auth_failed)."""
    blob = f"{stderr}\n{stdout}"
    rate = any(p.search(blob) for p in RATE_LIMIT_PATTERNS)
    auth = any(p.search(blob) for p in AUTH_PATTERNS)
    return rate, auth


def _parse_envelope(raw: str) -> tuple[str, dict[str, Any]]:
    """Return (extracted_text, full_envelope). Handles OpenClaw 2026.4+ shape
    {result: {payloads: [{text: ...}]}} and legacy {response|message|text}."""
    if not raw:
        return "", {}
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip(), {}

    if isinstance(envelope, list):
        return json.dumps(envelope), {"raw_list": envelope}
    if not isinstance(envelope, dict):
        return "", {}

    # Primary path: OpenClaw 2026.4+
    result = envelope.get("result") or {}
    if isinstance(result, dict):
        payloads = result.get("payloads")
        if isinstance(payloads, list) and payloads:
            first = payloads[0]
            if isinstance(first, dict) and first.get("text"):
                return first["text"].strip(), envelope
    # Legacy fallbacks
    for k in ("response", "message", "text"):
        v = envelope.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip(), envelope

    return "", envelope


def _extract_meta(envelope: dict[str, Any]) -> tuple[str, str]:
    """Pull provider + model from the envelope for telemetry."""
    result = envelope.get("result") or {}
    if not isinstance(result, dict):
        return "", ""
    meta = result.get("meta") or {}
    agent_meta = meta.get("agentMeta") or {}
    provider = str(agent_meta.get("provider") or "")
    model = str(agent_meta.get("model") or "")
    return provider, model


def _extract_usage(envelope: dict[str, Any]) -> dict[str, int]:
    """Extract token usage from envelope.result.meta.agentMeta.usage.

    OpenClaw 2026.4+ exposes {input, output, cacheRead, cacheWrite, total}.
    Returns dict with zero fallbacks so callers don't need to null-check.
    """
    try:
        result = envelope.get("result") or {}
        meta = result.get("meta") or {}
        agent_meta = meta.get("agentMeta") or {}
        usage = agent_meta.get("usage") or {}
        return {
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
            "cache_read": int(usage.get("cacheRead") or 0),
            "cache_write": int(usage.get("cacheWrite") or 0),
            "total": int(usage.get("total") or 0),
        }
    except (TypeError, ValueError, AttributeError):
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}


# Rough cost tables — $ per 1M tokens. Updated from OpenAI + Anthropic 2026 pricing.
# Cache reads cost 25% of input, cache writes cost 125% of input (OpenAI prompt caching).
_COST_TABLE = {
    # (provider, model_prefix) → (input_per_m, output_per_m)
    ("openai-codex", "gpt-5.4"): (15.0, 75.0),
    ("openai-codex", "gpt-5.3"): (15.0, 75.0),
    ("openai-codex", "gpt-5"): (15.0, 75.0),
    ("openai-codex", "gpt-4o"): (5.0, 20.0),
    ("anthropic", "claude-opus-4-6"): (15.0, 75.0),
    ("anthropic", "claude-sonnet-4-6"): (3.0, 15.0),
    ("anthropic", "claude-haiku-4-5"): (1.0, 5.0),
    ("ollama", ""): (0.0, 0.0),  # local, free
    ("gemini", ""): (0.0, 0.0),  # free tier
}


def _estimate_cost_usd(provider: str, model: str, usage: dict[str, int]) -> float:
    """Rough per-call cost in USD. Returns 0 when table has no match."""
    if not provider or not usage:
        return 0.0
    rates: tuple[float, float] | None = None
    for (p, m_prefix), (in_rate, out_rate) in _COST_TABLE.items():
        if provider == p and (not m_prefix or model.startswith(m_prefix)):
            rates = (in_rate, out_rate)
            break
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    # Non-cached input portion: total input minus cache_read
    fresh_input = max(0, usage["input"] - usage["cache_read"])
    cached_input = usage["cache_read"]
    output = usage["output"]
    cost = (
        (fresh_input / 1_000_000) * input_rate
        + (cached_input / 1_000_000) * (input_rate * 0.25)
        + (output / 1_000_000) * output_rate
    )
    return round(cost, 6)


def dispatch(
    agent: str,
    message: str,
    *,
    thinking: str = "low",
    timeout: int = 60,
    degraded_placeholder: str = "",
    session_id: str | None = None,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
) -> DispatchResult:
    """Dispatch to an OpenClaw agent with retry + degraded fallback.

    Parameters
    ----------
    agent               : OpenClaw agent id (jenna | liz | ellie | sage | market)
    message             : prompt body
    thinking            : off | minimal | low | medium | high | xhigh
    timeout             : per-attempt subprocess timeout (seconds)
    degraded_placeholder: text to return in .degraded when all retries fail
                          (callers can still persist this to avoid a data gap)
    session_id          : Optional explicit session to target. When set to a
                          brain-owned id (e.g. 'brain_dispatch_sage'), isolates
                          brain's mechanical dispatches from Chris's interactive
                          Jenna Telegram session, avoiding cross-contamination
                          of cache prefixes. Default None = agent's main session.
    backlog_kind        : Optional llm_backlog kind — if the dispatch fails
                          (rate-limited, circuit-breaker-open, or all retries
                          exhausted), the work is enqueued onto llm_backlog
                          for catch-up when LLM quota returns. One of:
                          classify | entities | distill | synthesis |
                          proactive | telegram | reflect.
    backlog_payload     : Payload dict stored with the backlog entry. Must
                          contain enough context for the kind's handler to
                          re-run the work (e.g. prompt, out_path, severity).
    """
    global _cb_failures, _cb_open_until
    t_start = _time.time()
    result = DispatchResult(ok=False, attempts=0)

    # Phase 5: persistent circuit breaker via brain_core.breakers (replaces
    # the in-memory CB). Falls back to legacy in-memory CB if breakers module
    # can't be imported.
    #
    # CR9 fix (2026-04-14): breaker check MUST run BEFORE cache check, so
    # time-sensitive callers hit the enqueue-backlog path when LLM is
    # known-down rather than getting a stale cache hit that masks the
    # outage from their own catch-up logic. Previously cache was first
    # and the enqueue was skipped — a Telegram alert with a cached
    # response text would return ok=True to the caller, bypassing the
    # intended llm_backlog behavior.
    _persistent_cb = None
    try:
        from breakers import peek_breaker as _pb
        from breakers import record_result as _br_record

        _persistent_cb = (_pb, _br_record)
        snapshot = _pb("llm.dispatch")
        if snapshot.is_open:
            log.warning("llm.dispatch breaker open — fast-failing dispatch to %s", agent)
            _record_usage(agent, 0, ok=False, skipped_cb=True)
            _enqueue_backlog_if_requested(backlog_kind, backlog_payload, "breaker_open")
            return DispatchResult(
                ok=False,
                text="",
                error="circuit breaker open (persistent)",
                attempts=0,
                duration_ms=0,
                degraded="circuit breaker open",
            )
    except Exception:
        _persistent_cb = None

    # Semantic cache check — return cached response for near-identical prompts.
    # Opt-in via BRAIN_DISPATCH_CACHE_ENABLED (default false).
    #
    # CR9 fix (2026-04-14): cache hit MUST still record usage + heal
    # breaker so metering + spike detection stay accurate. Previously
    # cache returned a bare DispatchResult bypassing every bookkeeping
    # path, so _sense_llm_usage_spike saw deflated baselines and the
    # half-open breaker never healed from cached traffic.
    if BRAIN_DISPATCH_CACHE_ENABLED:
        cached_text = _dispatch_cache_lookup(message)
        if cached_text:
            log.info("dispatch: semantic cache hit for agent=%s", agent)
            # Record usage with provider='cache' so spike detection sees
            # every call (even cached) and cost metering stays accurate.
            _record_usage(agent, 0, ok=True, provider="cache", model="cache")
            if _persistent_cb is not None:
                try:
                    _persistent_cb[1]("llm.dispatch", ok=True)
                except Exception:
                    pass
            return DispatchResult(
                ok=True,
                text=cached_text,
                error="",
                attempts=0,
                duration_ms=0,
                provider="cache",
                model="cache",
            )

    # Legacy in-memory CB (kept as fallback during cutover)
    with _cb_lock:
        if _time.monotonic() < _cb_open_until:
            log.warning("circuit breaker open — fast-failing dispatch to %s", agent)
            # Record as skipped_cb so it doesn't inflate failure counts in budget stats
            _record_usage(agent, 0, ok=False, skipped_cb=True)
            _enqueue_backlog_if_requested(backlog_kind, backlog_payload, "breaker_open_legacy")
            return DispatchResult(
                ok=False,
                text="",
                error="circuit breaker open",
                attempts=0,
                duration_ms=0,
                degraded="circuit breaker open",
            )
        # Half-open transition: breaker just expired. Reset the failure counter
        # so the next attempt gets a fresh budget rather than tripping instantly
        # the moment it encounters the first error.
        if _cb_failures >= _CB_THRESHOLD and _cb_open_until and _time.monotonic() >= _cb_open_until:
            log.info("circuit breaker half-open — resetting failure counter")
            _cb_failures = 0
            _cb_open_until = 0.0

    for attempt_index in range(MAX_ATTEMPTS):
        # Hard wall-time cap — abort early if we've already been retrying too long
        if (_time.time() - t_start) > MAX_TOTAL_SECONDS:
            result.error = f"total wall time exceeded {MAX_TOTAL_SECONDS}s"
            break
        result.attempts = attempt_index + 1
        # D fix (2026-04-14): replaced Popen PIPE + communicate() with tempfile
        # redirection + wait(). The old M9.1 approach used start_new_session
        # + killpg on timeout, but that still left a class of deadlocks where
        # openclaw grandchildren (ACP gateway HTTP client) held the stdout
        # pipe open AND somehow escaped the process group via their own
        # setsid() — so killpg couldn't reach them, and Python's
        # communicate() blocked forever waiting for pipe EOF. Reproduced
        # twice in bulk entity extraction (backfill hung at atom ~50 both
        # runs, 0% CPU, single idle Neo4j socket).
        #
        # Tempfile-based I/O eliminates the entire deadlock class: writes go
        # to disk file descriptors, not pipes, so there's nothing for
        # grandchildren to hold open from the parent's perspective. wait()
        # is much simpler than communicate() — it just polls the child's
        # exit. On timeout we still killpg the whole tree for safety, but
        # we never depend on pipe drainage to unblock the parent.
        popen = None
        stdout_f = None
        stderr_f = None
        try:
            _cmd = [
                OPENCLAW_BIN,
                "agent",
                "--agent",
                agent,
                "--message",
                message,
                "--json",
                "--thinking",
                thinking,
                "--timeout",
                str(timeout),
            ]
            # 2026-04-17 cost fix: brain dispatches MUST NOT land in the
            # agent's interactive session (Chris's Telegram chat with
            # Jenna accumulates 30k+ events / 95MB over 2 weeks, and every
            # call replays the full history — ~140k tokens avg per call,
            # $150+/day). Use a stable brain-owned session that rotates
            # daily to cap prompt size. Telegram interactive session is
            # untouched.
            if session_id is None:
                session_id = f"brain-auto-{agent}-{datetime.now().strftime('%Y-%m-%d')}"
            _cmd.extend(["--session-id", session_id])
            import tempfile as _tempfile

            stdout_f = _tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            stderr_f = _tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            popen = subprocess.Popen(
                _cmd,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                start_new_session=True,  # makes popen.pid a process group leader
            )
            try:
                popen.wait(timeout=timeout + 30)

                # Rewind and read tempfiles. No pipe — no deadlock.
                stdout_f.seek(0)
                stdout = stdout_f.read()
                stderr_f.seek(0)
                stderr = stderr_f.read()

                class _ProcLike:
                    pass

                proc = _ProcLike()
                proc.returncode = popen.returncode
                proc.stdout = stdout
                proc.stderr = stderr
            except subprocess.TimeoutExpired as e:
                # Hard-kill the whole process group — don't wait for children
                try:
                    import os as _os
                    import signal as _signal

                    _os.killpg(_os.getpgid(popen.pid), _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                # Give the group 5s to die, then hard-kill the immediate child
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    popen.kill()
                    try:
                        popen.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                raise e from None
            finally:
                # Always close tempfiles — they're unlinked on close via
                # TemporaryFile semantics, so grandchildren holding the fd
                # just drain into a deleted file (harmless).
                if stdout_f is not None:
                    try:
                        stdout_f.close()
                    except Exception:
                        pass
                if stderr_f is not None:
                    try:
                        stderr_f.close()
                    except Exception:
                        pass
        except subprocess.TimeoutExpired as e:
            result.error = f"subprocess timeout: {e}"
            _log_failure(
                {
                    "agent": agent,
                    "attempt": result.attempts,
                    "error": result.error,
                    "kind": "timeout",
                }
            )
        else:
            rate, auth = _classify_error(proc.stderr, proc.stdout)
            result.rate_limited = result.rate_limited or rate
            result.auth_failed = result.auth_failed or auth

            if proc.returncode == 0 and proc.stdout.strip():
                text, envelope = _parse_envelope(proc.stdout)
                if text:
                    result.ok = True
                    result.text = text
                    result.envelope = envelope
                    result.provider, result.model = _extract_meta(envelope)
                    result.duration_ms = int((_time.time() - t_start) * 1000)
                    # CR10 fix (2026-04-14): clear sticky error flags on
                    # ultimate success. Previously result.rate_limited and
                    # auth_failed were `x or y` accumulators that never
                    # reset — so an attempt 1 rate-limited + attempt 2
                    # success returned ok=True AND rate_limited=True.
                    # Callers inspecting rate_limited on a success path
                    # (e.g. future conditional backlog enqueue) would
                    # make the wrong call.
                    result.rate_limited = False
                    result.auth_failed = False
                    with _cb_lock:
                        _cb_failures = 0
                    if _persistent_cb is not None:
                        try:
                            _persistent_cb[1]("llm.dispatch", ok=True)
                        except Exception:
                            pass
                    # v3 metering: pull usage from envelope + estimate cost
                    _usage = _extract_usage(envelope)
                    _cost = _estimate_cost_usd(result.provider, result.model, _usage)
                    _record_usage(
                        agent,
                        result.duration_ms,
                        ok=True,
                        prompt_tokens=_usage["input"],
                        response_tokens=_usage["output"],
                        provider=result.provider,
                        model=result.model,
                        cache_read_tokens=_usage["cache_read"],
                        cache_write_tokens=_usage["cache_write"],
                        cost_usd=_cost,
                    )
                    _update_agent_stats(agent, result.duration_ms, True, result.attempts)
                    _check_struggle(agent, message, result.duration_ms, True, result.attempts)
                    if BRAIN_DISPATCH_CACHE_ENABLED:
                        _dispatch_cache_put(message, result.text)
                    return result
                # Empty text despite rc=0 — treat as transient error.
                result.error = "empty text in envelope"
                _log_failure(
                    {
                        "agent": agent,
                        "attempt": result.attempts,
                        "error": result.error,
                        "stdout_preview": proc.stdout[:300],
                    }
                )
            else:
                result.error = (proc.stderr or proc.stdout or "unknown error")[:300]
                _log_failure(
                    {
                        "agent": agent,
                        "attempt": result.attempts,
                        "returncode": proc.returncode,
                        "error": result.error,
                        "rate_limited": rate,
                        "auth_failed": auth,
                    }
                )

        # Auth failures don't recover from retry — fail fast.
        if result.auth_failed:
            break

        if attempt_index < len(RETRY_DELAYS_SECONDS):
            delay = RETRY_DELAYS_SECONDS[attempt_index]
            _time.sleep(delay)

    # All attempts exhausted — degraded fallback.
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_open_until = _time.monotonic() + _CB_COOLDOWN
            log.warning(
                "circuit breaker OPEN — %d consecutive failures, cooldown %ds", _cb_failures, _CB_COOLDOWN
            )
    if _persistent_cb is not None:
        try:
            _persistent_cb[1]("llm.dispatch", ok=False, error=result.error[:200])
        except Exception:
            pass
    result.duration_ms = int((_time.time() - t_start) * 1000)
    result.degraded = degraded_placeholder or _build_degraded_placeholder(agent, message, result)
    _record_usage(agent, result.duration_ms, ok=False)
    _update_agent_stats(agent, result.duration_ms, False, result.attempts)
    _check_struggle(agent, message, result.duration_ms, False, result.attempts)
    _enqueue_backlog_if_requested(
        backlog_kind,
        backlog_payload,
        reason=(
            "rate_limited" if result.rate_limited else ("auth_failed" if result.auth_failed else "exhausted")
        ),
    )
    return result


def _enqueue_backlog_if_requested(
    kind: str | None,
    payload: dict | None,
    reason: str,
) -> None:
    """Stamp this failed dispatch onto llm_backlog so the work gets re-tried
    when quota returns. Best-effort — swallows all errors."""
    if not kind:
        return
    try:
        from llm_backlog import enqueue as _backlog_enqueue

        final_payload = dict(payload or {})
        final_payload.setdefault("failure_reason", reason)
        _backlog_enqueue(kind, final_payload)
    except Exception:
        pass


def dispatch_with_schema(
    agent: str,
    message: str,
    schema_description: str,
    thinking: str = "low",
    timeout: int = 60,
    max_retries: int = 2,
    backlog_kind: str | None = None,
    backlog_payload: dict | None = None,
) -> dict | None:
    """Dispatch with JSON schema validation + retry on parse failure.

    Returns parsed dict on success, None if all retries fail.

    backlog_kind / backlog_payload are passed through to dispatch() on
    transport failure so llm_backlog catches up when quota returns.
    """
    import json as _json
    import re as _re

    schema_instruction = f"\n\nYou MUST respond with strict JSON matching this schema:\n{schema_description}\n\nNo prose. No markdown fences. Just the JSON object."

    error_suffix = ""
    for attempt in range(max_retries + 1):
        # Rebuild prompt each iteration — schema_instruction is never duplicated
        full_message = message + schema_instruction + error_suffix
        result = dispatch(
            agent=agent,
            message=full_message,
            thinking=thinking,
            timeout=timeout,
            backlog_kind=backlog_kind,
            backlog_payload=backlog_payload,
        )

        # Transport failure: dispatch already retried internally. Give up.
        if not result.ok:
            return None

        text = result.text.strip()
        # Strip markdown fences — handles both ```json\n...\n``` and plain ```\n...\n```
        text = _re.sub(r"^```(?:json)?\s*", "", text)
        text = _re.sub(r"\s*```\s*$", "", text).strip()

        try:
            return _json.loads(text)
        except _json.JSONDecodeError as e:
            error_suffix = f"\n\nPrevious attempt failed with: JSON parse error: {e}. Respond again, strictly matching the schema. No prose. No fences."

    return None


def _build_degraded_placeholder(agent: str, message: str, result: DispatchResult) -> str:
    """Generate a marker string that synthesis jobs can safely persist as 'no-op output'."""
    ts = datetime.now().isoformat(timespec="seconds")
    reason = (
        "rate_limited"
        if result.rate_limited
        else ("auth_failed" if result.auth_failed else "dispatch_failed")
    )
    return (
        f"[DEGRADED {ts}] agent={agent} reason={reason} attempts={result.attempts}\n"
        f"(Original request: {message[:300]})\n"
        f"[This is an auto-generated stub. The canonical pipeline will retry on next run.]"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="openclaw dispatch smoke test")
    parser.add_argument("--agent", default="jenna")
    parser.add_argument("--message", required=True)
    parser.add_argument("--thinking", default="low")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    r = dispatch(args.agent, args.message, thinking=args.thinking, timeout=args.timeout)
    print(f"ok={r.ok} attempts={r.attempts} duration_ms={r.duration_ms}")
    print(f"provider={r.provider} model={r.model}")
    if r.ok:
        print(f"text: {r.text[:400]}")
    else:
        print(f"error: {r.error}")
        print(f"rate_limited={r.rate_limited} auth_failed={r.auth_failed}")
        print(f"degraded: {r.degraded}")
