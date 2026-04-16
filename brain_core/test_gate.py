"""brain_core/test_gate.py — deterministic test-data detection at ingest.

Catches the session_ids, source tags, and prompt patterns that mean "this is
a test or smoke-test, not Chris's real input", so brain's truth layer never
gets polluted by verification runs.

Layer A of the Brain Hygiene Stack — runs BEFORE /memory POST + wm_set +
atoms_store.upsert_atom. Pattern-based (deterministic, zero LLM cost).

When is_test_context() returns True, the caller should either reject the
write entirely (with 400 test_data_blocked) or mark the record as provisional.
Default policy is REJECT — brain cleanliness > write success.
"""

from __future__ import annotations

import re

# session_id prefixes that mark a session as a test harness, not a real user
# interaction. Deliberately conservative — these strings should never appear
# in real Claude Code or OpenClaw session_ids.
TEST_SESSION_PREFIXES = (
    "test-",
    "e2e-",
    "smoke-",
    "final-",        # I used final-verif, final-after-autopilot, etc.
    "hook-e2e-",
    "wm-e2e-",
    "e2e-test-",
    "synthetic-",
    "bench-",
    "ci-",
    "debug-",
)

# MR3 fix (2026-04-14): agent prefixes that mark a call as a test
# harness. Previously the `agent` parameter in is_test_context was
# accepted but never checked — test-agent signals were ignored.
TEST_AGENT_PREFIXES = (
    "test-",
    "test_",
    "smoke-",
    "e2e-",
    "bench-",
    "ci-",
    "fixture-",
    "mock-",
)

# source tags that mark an ingest as test content
TEST_SOURCE_PATTERNS = (
    re.compile(r"^test[:_-]", re.IGNORECASE),
    re.compile(r"^smoke[:_-]", re.IGNORECASE),
    re.compile(r"^e2e[:_-]", re.IGNORECASE),
    re.compile(r"^bench[:_-]", re.IGNORECASE),
    re.compile(r"^ci[:_-]", re.IGNORECASE),
    re.compile(r"(?:^|[:_-])synthetic(?:[:_-]|$)", re.IGNORECASE),
)

# content patterns that are obvious test payloads.
# All anchored ^...$ so we only match when the ENTIRE content is the stub —
# real content mentioning "hello world" as an example must NOT be blocked.
TEST_CONTENT_PATTERNS = (
    re.compile(
        r"^\s*(?:test|lorem ipsum|asdf|qwerty|foo ?bar|hello world)[\s.!?]*$",
        re.IGNORECASE,
    ),
)


def is_test_session(session_id: str | None) -> bool:
    """True if the session_id looks like a test harness run."""
    if not session_id:
        return False
    sid = session_id.lower()
    return any(sid.startswith(p) for p in TEST_SESSION_PREFIXES)


def is_test_agent(agent: str | None) -> bool:
    """MR3 fix: true if the agent id has a test-harness prefix."""
    if not agent:
        return False
    a = agent.lower()
    return any(a.startswith(p) for p in TEST_AGENT_PREFIXES)


def is_test_source(source: str | None) -> bool:
    """True if the source tag indicates a test ingest."""
    if not source:
        return False
    return any(p.search(source) for p in TEST_SOURCE_PATTERNS)


def is_test_content(content: str | None) -> bool:
    """True if the payload itself is an obvious test stub."""
    if not content:
        return False
    # Only match on short inputs — don't false-positive on real content
    # that happens to mention 'test'.
    if len(content) > 200:
        return False
    return any(p.search(content) for p in TEST_CONTENT_PATTERNS)


def is_test_context(
    *,
    session_id: str | None = None,
    source: str | None = None,
    content: str | None = None,
    agent: str | None = None,
) -> tuple[bool, str]:
    """Unified gate. Returns (is_test, reason).

    Call from /memory POST, wm_set, atoms_store.upsert_atom, etc. When True,
    the caller should reject the write to keep brain clean.
    """
    if is_test_session(session_id):
        return True, f"test_session_id:{session_id}"
    if is_test_agent(agent):
        return True, f"test_agent:{agent}"
    if is_test_source(source):
        return True, f"test_source:{source}"
    if is_test_content(content):
        return True, f"test_content:{(content or '')[:40]}"
    return False, ""
