"""Integration: live Hermes provider prefetch for the passive durable-guidance class.

Exercises ``hermes_integration.brain_memory_provider.BrainMemoryProvider.prefetch()``
end-to-end against the RUNNING brain server (real ``/recall/v2`` over HTTP) — NOT a
mocked ``_brain_request``. The unit tests fake the HTTP round-trip, so they cannot
catch the live regression this task fixes (passive durable-guidance positives that
classify correctly but get zeroed by the server's out-of-domain quality filter).

Pins both halves of the recall-governance contract for the passive-procedure class:

  - live / current-status reads   -> empty prefetch (suppressed, acceptance #1)
  - passive durable-guidance       -> routed to durable recall (not live-state) and
                                      the operational class is served through the
                                      real HTTP path (acceptance #2)

Class-level paraphrases (EN + KO), no exact acceptance-probe coupling. Marked
``integration`` (auto-skipped unless ``BRAIN_INTEGRATION_TESTS=1``) and skips when
the server / bearer token is unavailable. The token is loaded inside the provider's
own ``_brain_request`` helper and is never read or printed here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Resolve the Hermes runtime so the provider's `from agent.memory_provider import
# MemoryProvider` import succeeds (same shim the provider unit tests use).
for _hermes_root in (
    os.environ.get("HERMES_AGENT_ROOT", ""),
    os.environ.get("OMX_ADAPT_HERMES_ROOT", ""),
    "/Users/chrischo/.hermes/hermes-agent",
    str(Path.home() / ".hermes/hermes-agent"),
):
    if _hermes_root and (Path(_hermes_root) / "agent" / "memory_provider.py").exists():
        sys.path.insert(0, _hermes_root)
        break

# Passive durable-guidance positives: "how is/are <operational subject>
# <procedure-verb>?" — stored guidance a stale answer still satisfies.
POSITIVE_DURABLE = [
    "how are running tasks managed?",
    "how is the task runner used?",
    "how are running jobs supposed to be managed?",
    "how is the runner configured?",
    "how are jobs monitored?",
]
# Live / current-status controls — answered by live tools, never stale recall.
NEGATIVE_LIVE = [
    "how are running tasks?",
    "how are the tasks going?",
    "what is running now?",
    "현재 실행 중인 작업 뭐 있어?",
]


def _provider():
    try:
        from hermes_integration.brain_memory_provider import BrainMemoryProvider
    except Exception as exc:  # agent runtime not importable in this env
        pytest.skip(f"hermes provider not importable: {exc}")
    prov = BrainMemoryProvider()
    prov._profile = "claude"
    return prov


def _server_recall(query: str) -> dict:
    """Raw /recall/v2 via the provider's real HTTP helper. Skips (not fails) when
    the server is unreachable or the bearer token is missing — _brain_request
    returns None on any failure, so None == server unavailable for this smoke."""
    import urllib.parse

    from hermes_integration import brain_memory_provider as pm

    params = urllib.parse.urlencode({"q": query, "n": 5, "agent": "claude"})
    resp = pm._brain_request(f"/recall/v2?{params}", timeout=8.0, actor="claude")
    if resp is None:
        pytest.skip("brain server unreachable or unauthenticated on 127.0.0.1:8791")
    return resp


def _is_live_state_suppressed(resp: dict) -> bool:
    return bool((resp.get("timing") or {}).get("live_state_query"))


def test_live_negative_live_state_controls_return_empty_prefetch():
    """Acceptance #1: the live/current-status controls are short-circuited by the
    live server (live_state_query=true) AND the provider injects nothing."""
    prov = _provider()
    for q in NEGATIVE_LIVE:
        assert _is_live_state_suppressed(
            _server_recall(q)
        ), f"server did not live-state-suppress a status control: {q!r}"
        assert prov.prefetch(q) == "", f"live-state prompt leaked prefetch context: {q!r}"


def test_live_passive_durable_guidance_routed_to_durable_recall_and_served():
    """Acceptance #2: the passive durable-guidance positives are NOT live-state
    suppressed (they reach durable recall) and the provider's real HTTP path serves
    the operational class — proving the round-trip, not a mock."""
    prov = _provider()
    for q in POSITIVE_DURABLE:
        assert not _is_live_state_suppressed(
            _server_recall(q)
        ), f"durable-guidance prompt wrongly live-state-suppressed server-side: {q!r}"
    # End-to-end: EVERY passive durable-guidance positive must surface prefetch
    # context through live HTTP — including the terse 'how is the runner configured?'
    # form whose raw short query recalls only off-topic rows. The provider's
    # class-level operational-guidance expansion is what carries those terse prompts,
    # so an empty prefetch for ANY positive is a regression (the weak `any()` gate
    # masked exactly the 'configured?' failure this task fixes).
    served = {q: prov.prefetch(q).strip() for q in POSITIVE_DURABLE}
    empty = sorted(q for q, ctx in served.items() if not ctx)
    assert not empty, (
        f"passive durable-guidance positives served EMPTY provider prefetch: {empty!r}. "
        "Either the live server still drops in-domain positives on the out-of-domain "
        "quality filter (count=0) — restart Brain to deploy the operational-anchor "
        "analyzer (acceptance #4 restart gate) — or the provider's operational-guidance "
        "expansion failed to retrieve a durable row for the terse prompt."
    )
