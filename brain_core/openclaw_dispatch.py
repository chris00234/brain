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
import time
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
    from config import OPENCLAW_BIN, BRAIN_LOGS_DIR, BRAIN_DISPATCH_CACHE_ENABLED
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
    # Add skipped_cb column for legacy DBs created before this change
    try:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN skipped_cb INTEGER DEFAULT 0")
    except Exception:
        pass  # column already exists


def _record_usage(agent: str, duration_ms: int, ok: bool, prompt_tokens: int = 0, response_tokens: int = 0, skipped_cb: bool = False):
    """Record a dispatch to SQLite for budget monitoring."""
    try:
        LLM_USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        try:
            _ensure_usage_schema(conn)
            conn.execute(
                "INSERT INTO llm_usage (timestamp, agent, duration_ms, ok, prompt_tokens, response_tokens, skipped_cb) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_dt.now().isoformat(), agent, duration_ms, 1 if ok else 0, prompt_tokens, response_tokens, 1 if skipped_cb else 0)
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
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0",
                (cutoff,)
            ).fetchone()[0]
            per_agent = dict(conn.execute(
                "SELECT agent, COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0 GROUP BY agent",
                (cutoff,)
            ).fetchall())
            today_cutoff = _dt.now().strftime("%Y-%m-%d")
            today_count = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 0",
                (today_cutoff,)
            ).fetchone()[0]
            cb_skipped = conn.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND skipped_cb = 1",
                (cutoff,)
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
            dot = sum(a * b for a, b in zip(emb, cached_emb))
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


RETRY_DELAYS_SECONDS = (15, 30)               # 2 retries (was 3) — keeps total wall time under 3 min
MAX_ATTEMPTS = len(RETRY_DELAYS_SECONDS) + 1  # 3 attempts total
MAX_TOTAL_SECONDS = 180                       # hard cap — abort if total time exceeds 3 minutes

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
        s = _agent_stats.setdefault(agent, {
            "durations": [],
            "failures": 0,
            "total": 0,
            "last_struggle_logged": 0.0,
        })
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
                context=f"Struggle signal detected automatically",
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


def dispatch(
    agent: str,
    message: str,
    *,
    thinking: str = "low",
    timeout: int = 60,
    degraded_placeholder: str = "",
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
    """
    global _cb_failures, _cb_open_until
    t_start = time.time()
    result = DispatchResult(ok=False, attempts=0)

    # Semantic cache check — return cached response for near-identical prompts.
    # Opt-in via BRAIN_DISPATCH_CACHE_ENABLED (default false).
    if BRAIN_DISPATCH_CACHE_ENABLED:
        cached_text = _dispatch_cache_lookup(message)
        if cached_text:
            log.info("dispatch: semantic cache hit for agent=%s", agent)
            return DispatchResult(ok=True, text=cached_text, error="", attempts=0, duration_ms=0)

    # Circuit breaker check
    with _cb_lock:
        if _time.monotonic() < _cb_open_until:
            log.warning("circuit breaker open — fast-failing dispatch to %s", agent)
            # Record as skipped_cb so it doesn't inflate failure counts in budget stats
            _record_usage(agent, 0, ok=False, skipped_cb=True)
            return DispatchResult(ok=False, text="", error="circuit breaker open", attempts=0,
                                 duration_ms=0, degraded="circuit breaker open")
        # Half-open transition: breaker just expired. Reset the failure counter
        # so the next attempt gets a fresh budget rather than tripping instantly
        # the moment it encounters the first error.
        if _cb_failures >= _CB_THRESHOLD and _cb_open_until and _time.monotonic() >= _cb_open_until:
            log.info("circuit breaker half-open — resetting failure counter")
            _cb_failures = 0
            _cb_open_until = 0.0

    for attempt_index in range(MAX_ATTEMPTS):
        # Hard wall-time cap — abort early if we've already been retrying too long
        if (time.time() - t_start) > MAX_TOTAL_SECONDS:
            result.error = f"total wall time exceeded {MAX_TOTAL_SECONDS}s"
            break
        result.attempts = attempt_index + 1
        try:
            proc = subprocess.run(
                [
                    OPENCLAW_BIN, "agent",
                    "--agent", agent,
                    "--message", message,
                    "--json",
                    "--thinking", thinking,
                    "--timeout", str(timeout),
                ],
                capture_output=True, text=True, timeout=timeout + 30,
            )
        except subprocess.TimeoutExpired as e:
            result.error = f"subprocess timeout: {e}"
            _log_failure({
                "agent": agent,
                "attempt": result.attempts,
                "error": result.error,
                "kind": "timeout",
            })
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
                    result.duration_ms = int((time.time() - t_start) * 1000)
                    with _cb_lock:
                        _cb_failures = 0
                    _record_usage(agent, result.duration_ms, ok=True)
                    _update_agent_stats(agent, result.duration_ms, True, result.attempts)
                    _check_struggle(agent, message, result.duration_ms, True, result.attempts)
                    if BRAIN_DISPATCH_CACHE_ENABLED:
                        _dispatch_cache_put(message, result.text)
                    return result
                # Empty text despite rc=0 — treat as transient error.
                result.error = "empty text in envelope"
                _log_failure({
                    "agent": agent,
                    "attempt": result.attempts,
                    "error": result.error,
                    "stdout_preview": proc.stdout[:300],
                })
            else:
                result.error = (proc.stderr or proc.stdout or "unknown error")[:300]
                _log_failure({
                    "agent": agent,
                    "attempt": result.attempts,
                    "returncode": proc.returncode,
                    "error": result.error,
                    "rate_limited": rate,
                    "auth_failed": auth,
                })

        # Auth failures don't recover from retry — fail fast.
        if result.auth_failed:
            break

        if attempt_index < len(RETRY_DELAYS_SECONDS):
            delay = RETRY_DELAYS_SECONDS[attempt_index]
            time.sleep(delay)

    # All attempts exhausted — degraded fallback.
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_open_until = _time.monotonic() + _CB_COOLDOWN
            log.warning("circuit breaker OPEN — %d consecutive failures, cooldown %ds", _cb_failures, _CB_COOLDOWN)
    result.duration_ms = int((time.time() - t_start) * 1000)
    result.degraded = degraded_placeholder or _build_degraded_placeholder(agent, message, result)
    _record_usage(agent, result.duration_ms, ok=False)
    _update_agent_stats(agent, result.duration_ms, False, result.attempts)
    _check_struggle(agent, message, result.duration_ms, False, result.attempts)
    return result


def dispatch_with_schema(
    agent: str,
    message: str,
    schema_description: str,
    thinking: str = "low",
    timeout: int = 60,
    max_retries: int = 2,
) -> dict | None:
    """Dispatch with JSON schema validation + retry on parse failure.

    Returns parsed dict on success, None if all retries fail.
    """
    import json as _json
    import re as _re

    schema_instruction = f"\n\nYou MUST respond with strict JSON matching this schema:\n{schema_description}\n\nNo prose. No markdown fences. Just the JSON object."

    error_suffix = ""
    for attempt in range(max_retries + 1):
        # Rebuild prompt each iteration — schema_instruction is never duplicated
        full_message = message + schema_instruction + error_suffix
        result = dispatch(agent=agent, message=full_message, thinking=thinking, timeout=timeout)

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
    reason = "rate_limited" if result.rate_limited else ("auth_failed" if result.auth_failed else "dispatch_failed")
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
