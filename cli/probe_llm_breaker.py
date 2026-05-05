#!/usr/bin/env python3
"""Probe and safely close the llm.dispatch breaker after provider success.

This intentionally bypasses cli_dispatch's breaker pre-flight gate for one tiny
provider-health probe. The breaker is closed only when a real backend returns a
non-empty response. Failed probes do not extend the cooldown.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = BRAIN_ROOT / "brain_core"
sys.path.insert(0, str(CORE_DIR))

from breakers import peek_breaker, record_result  # noqa: E402
from cli_llm import BREAKER_KIND, FALLBACK_CHAIN, _try_backend  # noqa: E402

PROBE_PROMPT = "Reply exactly with these two letters and no extra text: OK"
DEFAULT_TIMEOUT_S = 20
DEFAULT_MAX_BACKENDS = 2


def _snapshot_dict(snapshot: Any) -> dict:
    if snapshot is None:
        return {}
    return {
        "kind": snapshot.kind,
        "state": snapshot.state,
        "failures": snapshot.failures,
        "trip_count": snapshot.trip_count,
        "reset_after_s": snapshot.reset_after_s,
        "remaining_cooldown_s": round(snapshot.remaining_cooldown_s, 1),
        "reason": snapshot.reason,
        "opened_at": snapshot.opened_at,
        "last_failure_at": snapshot.last_failure_at,
        "last_action_at": snapshot.last_action_at,
    }


def _eligible_backends(max_backends: int) -> list[tuple[str, str, str]]:
    # Prefer stateless subscription CLIs for a cheap health probe; avoid the
    # OpenClaw fallback path because this breaker protects Brain LLM dispatch,
    # not interactive OpenClaw sessions.
    entries = [(b, m, d) for b, m, d in FALLBACK_CHAIN if b in {"codex", "claude"}]
    return entries[: max(1, max_backends)]


def run(*, timeout: int = DEFAULT_TIMEOUT_S, max_backends: int = DEFAULT_MAX_BACKENDS) -> dict:
    before = peek_breaker(BREAKER_KIND)
    attempts: list[dict] = []
    started = time.time()
    ok = False
    success_error = ""

    for backend, model, description in _eligible_backends(max_backends):
        t0 = time.time()
        try:
            result = _try_backend(backend, model, PROBE_PROMPT, timeout)
            text = (result.text or "").strip()
            attempt = {
                "backend": backend,
                "model": model,
                "description": description,
                "ok": bool(result.ok and text),
                "duration_ms": result.duration_ms,
                "error": (result.error or "")[:300],
                "text_preview": text[:40],
            }
        except Exception as exc:
            attempt = {
                "backend": backend,
                "model": model,
                "description": description,
                "ok": False,
                "duration_ms": int((time.time() - t0) * 1000),
                "error": str(exc)[:300],
            }
        attempts.append(attempt)
        if attempt["ok"]:
            ok = True
            success_error = ""
            break

    after_probe = peek_breaker(BREAKER_KIND)
    reset_snapshot = None
    if ok:
        reset_snapshot = record_result(BREAKER_KIND, ok=True, error=success_error)

    return {
        "ok": ok,
        "breaker": BREAKER_KIND,
        "before": _snapshot_dict(before),
        "after_probe": _snapshot_dict(after_probe),
        "after_reset": _snapshot_dict(reset_snapshot or peek_breaker(BREAKER_KIND)),
        "attempts": attempts,
        "duration_s": round(time.time() - started, 3),
        "note": "breaker_closed_after_successful_provider_probe"
        if ok
        else "probe_failed_breaker_not_extended",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--max-backends", type=int, default=DEFAULT_MAX_BACKENDS)
    args = parser.parse_args()
    result = run(timeout=args.timeout, max_backends=args.max_backends)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
