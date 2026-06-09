from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
HERMES_ROOT_CANDIDATES = [
    Path(p)
    for p in (
        os.environ.get("HERMES_AGENT_ROOT", ""),
        os.environ.get("OMX_ADAPT_HERMES_ROOT", ""),
        "/Users/chrischo/.hermes/hermes-agent",
        str(Path.home() / ".hermes/hermes-agent"),
        str(BRAIN_ROOT.parent / "hermes-agent"),
    )
    if p
]
for hermes_root in HERMES_ROOT_CANDIDATES:
    if (hermes_root / "agent" / "memory_provider.py").exists():
        sys.path.insert(0, str(hermes_root))
        break

from hermes_integration import brain_memory_provider as provider_mod  # noqa: E402
from hermes_integration.brain_memory_provider import BrainMemoryProvider  # noqa: E402


def test_shutdown_drains_queued_turn_writes(monkeypatch):
    writes: list[dict] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        if path == "/memory" and method == "POST":
            writes.append({"body": body, "actor": actor})
        return {"ok": True}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "jenna"
    provider._writer_thread = threading.Thread(target=provider._writer_loop)
    provider._writer_thread.start()

    provider.sync_turn("u1", "a1", session_id="s1")
    provider.sync_turn("u2", "a2", session_id="s1")
    provider.sync_turn("u3", "a3", session_id="s1")
    provider.shutdown()

    assert [w["body"]["content"] for w in writes] == [
        "User: u1\nAssistant: a1",
        "User: u2\nAssistant: a2",
        "User: u3\nAssistant: a3",
    ]
    assert {w["actor"] for w in writes} == {"jenna"}


def test_prefetch_uses_profile_scoped_recall(monkeypatch):
    calls: list[dict] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append({"path": path, "method": method, "actor": actor})
        return {
            "results": [
                {
                    "title": "Preference",
                    "content": "Chris prefers concise Korean status updates.",
                    "score": 0.91,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "sage"
    context = provider.prefetch("response style")

    assert "Brain recall (profile=sage)" in context
    assert "Chris prefers concise Korean status updates." in context
    assert calls[0]["method"] == "GET"
    assert calls[0]["actor"] == "sage"
    assert "/recall/v2?" in calls[0]["path"]
    assert "agent=sage" in calls[0]["path"]


def test_builtin_memory_write_is_mirrored_to_brain(monkeypatch):
    writes: list[dict] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        if path == "/memory" and method == "POST":
            writes.append({"body": body, "actor": actor})
        return {"ok": True}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "liz"
    provider._platform = "cli"
    provider._writer_thread = threading.Thread(target=provider._writer_loop)
    provider._writer_thread.start()

    provider.on_memory_write(
        "add",
        "user",
        "Chris prefers durable settings to be explicit.",
        metadata={"session_id": "s2"},
    )
    provider.shutdown()

    assert writes == [
        {
            "actor": "liz",
            "body": {
                "content": "Chris prefers durable settings to be explicit.",
                "category": "preference",
                "agent": "liz",
                "source": "hermes",
                "confidence": 0.65,
                "reason": ("kind=builtin_memory_write action=add target=user " "session=s2 platform=cli"),
            },
        }
    ]


def test_prefetch_collapses_near_duplicate_eval_score_preferences(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "old",
                    "collection": "semantic_memory",
                    "title": "Brain eval preference",
                    "content": "Chris wants Brain fine tuning judged by measurable eval score improvements.",
                    "score": 0.99,
                },
                {
                    "id": "canonical",
                    "collection": "canonical",
                    "title": "Brain quality decision",
                    "content": "Brain fine-tuning should improve measurable eval score improvements, not vibes.",
                    "score": 0.70,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "liz"
    context = provider.prefetch("브레인 검색품질 평가 점수 개선")

    assert context.count("eval score") == 1
    assert "canonical: Brain quality decision" in context


def test_prefetch_filters_generic_brain_infra_noise(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "actionable",
                    "title": "Brain eval score preference",
                    "collection": "canonical",
                    "content": "Chris wants Brain fine-tuning judged by measurable eval score improvements, not vibes.",
                    "score": 0.97,
                },
                {
                    "id": "noise",
                    "title": "Knowledge gap bridge: Brain system dependency",
                    "collection": "canonical",
                    "content": "Brain depends on FastAPI brain-server, native Qdrant, and native Ollama.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    context = provider.prefetch("Brain recall quality should be no-noise and improve eval score")

    assert "Brain recall" in context
    assert "eval score improvements" in context
    assert "Knowledge gap bridge" not in context


def test_prefetch_returns_empty_when_all_brain_quality_hits_are_noise(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "noise",
                    "title": "Knowledge gap bridge: Brain system dependency",
                    "collection": "canonical",
                    "content": "Brain depends on FastAPI brain-server, native Qdrant, and native Ollama.",
                    "score": 0.95,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("Brain recall quality should be no-noise") == ""


def test_prefetch_korean_live_status_prompt_suppresses_stale_memory(monkeypatch):
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "Old kanban memory", "content": "stale", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("칸반 태스크 alpha7 진행상황 업데이트") == ""
    assert calls == []


def test_prefetch_capability_recommendation_keeps_hard_constraints(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "constraint",
                    "title": "Music/TTS capability constraint",
                    "collection": "canonical",
                    "content": "For music/TTS capability recommendations, Chris has hard constraints against local generation models and paid SaaS API billing.",
                    "score": 0.8,
                },
                {
                    "id": "noise",
                    "title": "Boston session note",
                    "collection": "experience",
                    "content": "A trip note that mentioned audio once.",
                    "score": 0.99,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    context = provider.prefetch("Get me updated recommendations for music/TTS capability")

    assert "hard constraints against local generation models" in context
    assert "Boston session note" not in context


def test_prefetch_shared_live_state_gate_suppresses_present_state(monkeypatch):
    """The shared live-state classifier (recall_governance) suppresses a
    present-state prompt that the local status regex misses ("wrapped up at the
    moment") — provider, /recall/v2, and /recall/active now agree."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "stale", "content": "old memory", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("Is it wrapped up at the moment?") == ""
    assert calls == []


def test_prefetch_drops_low_authority_session_summary_unless_summary_intent(monkeypatch):
    """provider_prefetch policy: a low-authority session-summary row is dropped
    even with topical overlap; a durable preference survives. When the user
    explicitly asks for a summary, the low-authority row is kept."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "durable",
                    "title": "Deployment preference",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "content": "Chris's durable deployment preference: Docker containers in Uptime Kuma.",
                    "score": 0.60,
                },
                {
                    "id": "sess",
                    "title": "Session summary",
                    "collection": "rag",
                    "metadata": {"source_path": "/sessions/2026-05-10-deploy.md"},
                    "content": "deployment preference discussed in the weekly session summary.",
                    "score": 0.99,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    context = provider.prefetch("what is my deployment preference")
    assert "Docker containers in Uptime Kuma" in context
    assert "weekly session summary" not in context

    # Explicit summary intent → the low-authority summary is the requested row.
    summary_ctx = provider.prefetch("summarize my deployment preferences")
    assert "weekly session summary" in summary_ctx


def test_prefetch_empty_when_only_low_authority_rows_for_non_summary_query(monkeypatch):
    """Strict provider_prefetch contract: when EVERY candidate is a low-authority
    summary/session/procedure row and the query is not a summary request (nor a
    brain-quality query), prefetch must return empty — it must NOT fall back to
    injecting the rejected rows. Regression for the `filtered or results`
    fallback re-injecting low-authority context."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "sess",
                    "title": "Session summary",
                    "collection": "rag",
                    "metadata": {"source_path": "/sessions/2026-05-10-deploy.md"},
                    "content": "deployment preference discussed in the weekly session summary.",
                    "score": 0.99,
                },
                {
                    "id": "proc",
                    "title": "procedure: deploy steps",
                    "collection": "knowledge",
                    "metadata": {"source_path": "/procedures/deploy_voyager.md"},
                    "content": "deployment procedure steps recorded from a voyager run.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("what is my deployment preference") == ""


def test_prefetch_route_guarantee_outranks_distilled_apibilling_escalation(monkeypatch):
    """A server-injected route_guarantee row must outrank an older distilled
    escalation summary that merely contains an 'api billing' phrase, so the
    Codex-Hermes tmux/TUI preference (not the escalation row) reaches the system
    prompt. Regression for provider _rank_score tiering."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "escalation",
                    "collection": "distilled",
                    "source_type": "distilled",
                    "title": "Codex/Claude escalation policy",
                    "content": (
                        "Chris prefers subscription-backed Codex/Claude CLI reasoning only for "
                        "Brain escalation handling, avoiding paid API billing."
                    ),
                    "score": 0.95,
                },
                {
                    "id": "rg",
                    "collection": "canonical",
                    "source_type": "route_guarantee",
                    "title": "codex_workflow route guarantee",
                    "content": (
                        "Chris prefers using Codex through Hermes as an interactive terminal-like "
                        "tmux TUI when quality or steering matters; headless codex exec is only "
                        "for bounded automation."
                    ),
                    "metadata": {"authority_tier": "direct_current_truth"},
                    "score": 0.90,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    context = provider.prefetch("When quality or steering matters, how should Chris drive Codex?")
    low = context.lower()
    assert "tmux" in low and "bounded automation" in low
    # the route guarantee must rank above (appear before) the escalation summary
    assert context.index("route guarantee") < context.index("escalation policy")


def test_prefetch_drops_openclaw_historical_row_for_non_openclaw_query(monkeypatch):
    """OpenClaw is historical context (Hermes is current). For a cost/tooling
    prompt that is NOT about OpenClaw/agents, a stale OpenClaw-provenance row must
    not leak into prefetch even when it reads as a hard-constraint hit — the clean
    current route guarantee carries the answer instead. Kept for an OpenClaw query."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "rg",
                    "collection": "canonical",
                    "source_type": "route_guarantee",
                    "title": "cost_billing route guarantee",
                    "content": (
                        "Chris is cost-conscious: prefer existing subscriptions and approved "
                        "integrations over new paid API billing or local model hosting."
                    ),
                    "metadata": {"authority_tier": "direct_current_truth"},
                    "score": 150.0,
                },
                {
                    "id": "oc",
                    "collection": "distilled",
                    "source_type": "distilled",
                    "title": "Decision: media generation approach",
                    "content": (
                        "# Summary OpenClaw jenna session: prefer existing subscriptions over "
                        "new paid API spend; no separate billing."
                    ),
                    "score": 400.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    cost_ctx = provider.prefetch("Recommend an LLM tooling approach without extra API billing")
    assert "openclaw" not in cost_ctx.lower(), "stale OpenClaw row leaked into non-OpenClaw recommendation"
    assert "cost_billing route guarantee" in cost_ctx, "clean current guarantee must still surface"

    # A prompt actually about the OpenClaw runtime keeps the OpenClaw row.
    oc_ctx = provider.prefetch("What is the current OpenClaw vs Hermes runtime distinction?")
    assert "openclaw" in oc_ctx.lower()


def test_recall_governance_loads_via_brain_core_package_when_toplevel_absent(monkeypatch):
    """Repo-root sys.path case (the Hermes runtime): top-level `recall_governance`
    is NOT importable, only `brain_core.recall_governance` is. The provider loader
    must still bind the governance callables + policy, so the OpenClaw / low-
    authority / live-state filters are not silent no-ops in live validation."""
    # Ensure the repo root is importable so brain_core.recall_governance resolves.
    monkeypatch.syspath_prepend(str(BRAIN_ROOT))
    # Force the top-level name unimportable: clear any cached modules, then shadow
    # the package with None so a fresh import raises ModuleNotFoundError.
    for name in list(sys.modules):
        if name == "recall_governance" or name.startswith("recall_governance."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "recall_governance", None)

    govern = provider_mod._load_recall_governance()
    for key in (
        "is_live_state_query",
        "is_positive_summary_intent",
        "query_targets_openclaw",
        "is_low_authority_result",
        "is_openclaw_historical_result",
        "prefetch_policy",
    ):
        assert govern.get(key) is not None, f"governance missing {key} via brain_core package"
    assert govern["prefetch_policy"].mode == "provider_prefetch"
    # Resolved from the brain_core-qualified package, not the (absent) top-level one.
    assert govern["query_targets_openclaw"].__module__.startswith("brain_core.recall_governance")


def test_recall_governance_loader_populated_in_default_env():
    """Sanity: in a normally-configured env the loader binds every callable."""
    govern = provider_mod._load_recall_governance()
    assert govern and govern.get("query_targets_openclaw") is not None
    assert govern["prefetch_policy"].mode == "provider_prefetch"


def test_prefetch_summary_excluded_keeps_guarantee_drops_distilled_and_summary(monkeypatch):
    """When the prompt explicitly excludes summaries ('not a summary'), provider
    prefetch must drop derived distilled and summary-shaped rows even though they
    read as constraint/preference hits, while the direct route_guarantee remains."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "rg",
                    "collection": "canonical",
                    "source_type": "route_guarantee",
                    "title": "codex_workflow route guarantee",
                    "content": (
                        "Chris prefers using Codex through Hermes as an interactive terminal-like "
                        "tmux TUI when quality or steering matters; headless codex exec is only "
                        "for bounded automation."
                    ),
                    "metadata": {"authority_tier": "direct_current_truth"},
                    "score": 0.90,
                },
                {
                    "id": "dist",
                    "collection": "distilled",
                    "source_type": "distilled",
                    "title": "Decision: tooling approach",
                    "content": "## Recommendation existing subscriptions. ## Reasoning prefers subscriptions.",
                    "score": 0.99,
                },
                {
                    "id": "summ",
                    "collection": "canonical",
                    "title": "distilled preference",
                    "content": "# Summary Chris prefers contract-first execution with explicit constraints.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("tell me Chris's current codex preference, not a summary")
    low = ctx.lower()
    assert "route guarantee" in low and "tmux" in low, "direct guarantee must remain"
    assert "summary" not in low, f"summary-shaped row leaked: {ctx!r}"
    assert "distilled" not in low, f"distilled row leaked: {ctx!r}"


def test_prefetch_positive_summary_intent_still_allows_summary(monkeypatch):
    """Control: an explicit summary request must still surface a summary row —
    the summary-excluded drop only fires on exclusion intent."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "s",
                    "collection": "rag",
                    "metadata": {"source_path": "/sessions/2026-05-10-deploy.md"},
                    "title": "Session summary",
                    "content": "deployment preference discussed in the weekly session summary.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("summarize my deployment preferences")
    assert "weekly session summary" in ctx


# ── Holdout regression (t_c7453635): out-of-domain world-knowledge gate ────


def test_prefetch_world_knowledge_prompt_defers_to_v2_out_of_domain_drop(monkeypatch):
    """Provider rides on /recall/v2's out-of-domain drop rather than a blunt
    pre-search gate. A hard OOD short-circuit would wrongly suppress terse
    in-domain prompts (e.g. 'response style', which the classifier also reads as
    anchor-less). So the provider still queries, but when the fixed /recall/v2
    returns no durable answer for a world-knowledge prompt it injects nothing —
    it must not invent profile/identity noise."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": []}  # fixed /recall/v2 drops out-of-domain noise

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "sage"

    assert provider.prefetch("How do I cook spaghetti arrabbiata tonight?") == ""
    assert calls, "recipe is not a hard pre-search gate; provider defers to v2"


def test_prefetch_in_domain_cost_paraphrase_is_not_suppressed(monkeypatch):
    """Negative control for the out-of-domain gate: a cost/tooling paraphrase
    (no spend noun) is in-domain and must still surface durable cost truth."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "title": "cost_billing route guarantee",
                    "source_type": "route_guarantee",
                    "collection": "canonical",
                    "metadata": {"authority_tier": "direct_current_truth"},
                    "content": (
                        "Prefer existing subscriptions over new paid API billing or local "
                        "model hosting; local models embeddings-only."
                    ),
                    "score": 250.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "sage"

    ctx = provider.prefetch(
        "Choose an AI tooling path that avoids new paid APIs and avoids self-hosting generation models."
    )
    low = ctx.lower()
    assert "subscription" in low and "local model" in low, f"cost truth suppressed: {ctx!r}"


# ── REQUEST_CHANGES f1: live-state precedence over strong-constraint ───────


def test_prefetch_live_state_with_brain_term_suppresses_despite_constraint(monkeypatch):
    """Finding 1 (provider_live_state_leakage): a live/current-state prompt that
    also contains a durable-constraint term ('brain') must still be suppressed.
    The shared live-state classifier takes precedence over the local strong-
    constraint override and over constraint expansion — the provider now agrees
    with /recall/v2 and /recall/active. EN + KO."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "stale", "content": "old brain memory", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("What is the brain prefetch pipeline running right now?") == ""
    assert provider.prefetch("brain 진행상황 지금 어때?") == ""
    assert calls == [], "live-state prompt must not hit recall even with a 'brain' constraint term"


def test_prefetch_out_of_domain_skips_constraint_expansion(monkeypatch):
    """Finding 1 (out-of-domain precedence over provider constraint expansion):
    an anchor-less world-knowledge prompt that happens to contain a constraint
    keyword ('music') must NOT be rewritten into a durable-preference fetch. The
    provider still defers the raw query to /recall/v2 (which drops it), but never
    issues the 'durable preferences/constraints' expansion queries that would
    surface unrelated personal memories."""
    paths: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        paths.append(path)
        # Only constraint-expansion queries carry these durable markers; the raw
        # out-of-domain query is dropped by the fixed /recall/v2.
        if any(marker in path for marker in ("durable", "preferences", "constraints")):
            return {
                "results": [
                    {
                        "id": "pref",
                        "title": "music preference",
                        "collection": "semantic_memory",
                        "metadata": {"category": "preference"},
                        "content": "Chris prefers subscription tools over paid music APIs.",
                        "score": 200.0,
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("How do I make a good music playlist for a party?") == ""
    assert paths, "provider still defers the raw query to /recall/v2"
    assert not any(
        any(marker in p for marker in ("durable", "preferences", "constraints")) for p in paths
    ), "out-of-domain prompt must not trigger durable constraint expansion"


# ── REQUEST_CHANGES f3: low-authority top-K filtered after a route guarantee ─


def test_prefetch_route_guarantee_drops_low_authority_constraint_hits(monkeypatch):
    """Finding 3 (provider_low_authority_topk_leakage): once a direct
    route_guarantee already satisfies the route, low-authority distilled /
    reflection / session / procedure rows must NOT survive into ranks 2-5 just
    because they textually contain a constraint phrase ('no paid saas', 'local
    models', 'subscription cli'). Only the guarantee reaches the system prompt."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "rg",
                    "collection": "canonical",
                    "source_type": "route_guarantee",
                    "title": "cost_billing route guarantee",
                    "content": (
                        "Chris is cost-conscious: prefer existing subscriptions over new paid "
                        "SaaS API billing or local model hosting; local models embeddings-only."
                    ),
                    "metadata": {"authority_tier": "direct_current_truth"},
                    "score": 120.0,
                },
                {
                    "id": "dist",
                    "collection": "distilled",
                    "source_type": "distilled",
                    "title": "Decision: TTS approach",
                    "score": 500.0,
                    "content": "# Summary distilled: no paid saas, use subscription cli, no local models for TTS.",
                },
                {
                    "id": "refl",
                    "collection": "rag",
                    "metadata": {"source_path": "/brain-reflect/2026-05-20-audio.md"},
                    "title": "reflection: audio cost",
                    "score": 480.0,
                    "content": "reflection: avoid paid saas, prefer subscription cli for audio.",
                },
                {
                    "id": "sess",
                    "collection": "rag",
                    "metadata": {"source_path": "/sessions/2026-05-21-tts.md"},
                    "title": "Session summary",
                    "score": 470.0,
                    "content": "weekly session summary: no local models, subscription cli for music tts.",
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("Recommend a music and TTS tooling approach that avoids new paid API billing.")
    low = ctx.lower()
    assert "cost_billing route guarantee" in ctx, "the direct guarantee must reach the prompt"
    assert "# summary" not in low, f"distilled summary row leaked past the guarantee: {ctx!r}"
    assert "reflection:" not in low, f"reflection row leaked past the guarantee: {ctx!r}"
    assert "session summary" not in low, f"session summary row leaked past the guarantee: {ctx!r}"


# ── Sage REQUEST_CHANGES f1: generic present-state status suppresses prefetch ─


def test_prefetch_generic_current_state_suppresses_without_recall(monkeypatch):
    """Finding 1 (provider mirror): a present-state status question about an
    arbitrary in-domain system ('current health of the deploy pipeline', 'where is
    the deploy rollout right now') must suppress prefetch entirely via the shared
    live-state gate — no recall call, empty injection. These are NOT low-signal
    'status'/'task'-keyword prompts and NOT Brain-special; the only reason to
    suppress is correct live-state classification. Holdout phrasings (no Brain/
    recall/prefetch term, not Sage's exact strings)."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "stale", "content": "old deploy status memory", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("What's the current health of the deploy pipeline?") == ""
    assert provider.prefetch("Where is the deploy rollout right now?") == ""
    assert calls == [], "generic present-state prompts must not hit recall"


# ── Sage REQUEST_CHANGES f2: durable workflow advice w/ 'running tasks' recalls ─


def test_prefetch_durable_workflow_with_running_tasks_term_still_recalls(monkeypatch):
    """Finding 2 (provider mirror): a durable workflow/tool-policy recommendation
    that merely CONTAINS a 'running tasks' (or KO 실행 중인 작업) noun phrase must
    NOT be suppressed as live-state — provider prefetch must still recall the
    durable workflow preference. The query carries recommend/workflow advice intent
    and no present-time deixis, so it is durable guidance, not a status read. EN+KO."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {
            "results": [
                {
                    "id": "pref",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "Task runner workflow preference",
                    "content": (
                        "Chris's workflow: run 작업/tasks through a tmux-managed runner, "
                        "not ad-hoc running shells."
                    ),
                    "score": 0.8,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    en = provider.prefetch("Recommend a workflow for monitoring my running tasks")
    ko = provider.prefetch("실행 중인 작업 관리 워크플로 추천해줘")
    assert "tmux-managed runner" in en, f"durable EN workflow advice suppressed as live-state: {en!r}"
    assert "tmux-managed runner" in ko, f"durable KO workflow advice suppressed as live-state: {ko!r}"
    assert calls, "durable workflow advice must hit recall, not be suppressed as live-state"


# ── Sage f1 (missed class): current-subject state suppresses prefetch ──────


def test_prefetch_current_subject_state_suppresses_without_recall(monkeypatch):
    """Finding 1 missed class (provider mirror): 'current <subject> state' (and
    '… right now') must suppress prefetch via the shared live-state gate — no
    recall call. 'state' is deliberately NOT in the provider's low-signal 'status'
    regex, so the ONLY thing that can suppress these is correct live-state
    classification. Arbitrary in-domain subjects; not Brain-special, not Sage's
    exact string."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "stale", "content": "old deploy state memory", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("what is the current deploy pipeline state?") == ""
    assert provider.prefetch("what is the current deploy rollout state right now?") == ""
    assert calls == [], "current-subject state prompts must not hit recall"


# ── Sage f2 (provider precedence): durable preference/workflow vs low-signal status ─


def test_prefetch_preferred_workflow_running_tasks_not_suppressed(monkeypatch):
    """Finding 2 (provider precedence): a durable preference/workflow/procedure
    prompt that contains running/tasks words must NOT be suppressed by the
    provider's low-signal status gate just because it lacks a 'recommend' verb.
    'what is the preferred workflow for running tasks?' carries durable-advice
    intent ('preferred'), so prefetch must reach recall and surface the stored
    workflow preference. The low-signal gate must defer to the shared durable-advice
    classification, not only to _STRONG_CONSTRAINT_QUERY_RE. A non-durable live
    status prompt with the same task words must still be suppressed."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {
            "results": [
                {
                    "id": "pref",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "Task runner workflow preference",
                    "content": (
                        "Chris's preferred workflow: drive running tasks through a " "tmux-managed runner."
                    ),
                    "score": 0.8,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    a = provider.prefetch("what is the preferred workflow for running tasks?")
    b = provider.prefetch("what is the preferred procedure for running jobs?")
    assert "tmux-managed runner" in a, f"durable preferred-workflow suppressed by low-signal gate: {a!r}"
    assert "tmux-managed runner" in b, f"durable preferred-procedure suppressed by low-signal gate: {b!r}"
    assert calls, "durable preference/workflow prompts must reach recall"

    # Control: a non-durable present-status prompt with the SAME task words must
    # still be suppressed before recall — the durable exemption is scoped to
    # advice intent, it does not re-open the low-signal status leak.
    before = len(calls)
    assert provider.prefetch("what is the current task status right now?") == ""
    assert len(calls) == before, "non-durable live status prompt must not hit recall"


# ── Sage REQUEST_CHANGES (t_daa410e4): durable procedure/workflow/how-to/policy ─
# WITHOUT a recommend/prefer marker must still recall ──────────────────────────


def test_prefetch_durable_procedure_workflow_no_advice_marker_still_recalls(monkeypatch):
    """Provider mirror of the analyzer fix: a durable procedure / workflow /
    how-to / monitoring-policy prompt that merely CONTAINS a 'running tasks/jobs'
    (or KO 실행 중인 작업) noun phrase must NOT be suppressed as live-state — even
    with NO recommend/prefer/추천 marker. Provider prefetch must reach recall and
    surface the stored workflow preference. The existing durable-workflow provider
    tests all rely on a recommend/prefer/preferred marker; this pins the unmarked
    class, which today is suppressed by BOTH the shared live-state gate and the
    low-signal status gate. EN + KO; holdout phrasings beyond the task-named
    strings, no Brain/recall special-casing.
    """
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {
            "results": [
                {
                    "id": "pref",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "Task runner workflow preference",
                    "content": (
                        "Chris's workflow: run 작업/tasks through a tmux-managed runner, "
                        "not ad-hoc running shells."
                    ),
                    "score": 0.8,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    durable = [
        "what workflow should I use for running tasks?",  # workflow, no advice marker
        "what procedure should we follow for monitoring running jobs?",  # procedure + monitoring-policy
        "what policy should we follow for monitoring running jobs?",  # holdout: EN monitoring-policy
        "실행 중인 작업 관리 방법 알려줘",  # KO how-to (방법) paraphrase
    ]
    for q in durable:
        ctx = provider.prefetch(q)
        assert "tmux-managed runner" in ctx, f"durable guidance suppressed as live-state: {q!r} -> {ctx!r}"
    assert calls, "durable procedure/workflow prompts must hit recall, not be suppressed"

    # Control: a true present-status prompt with the SAME task words stays
    # suppressed before recall — the durable exemption must not re-open the
    # live-state / low-signal status leak.
    before = len(calls)
    assert provider.prefetch("what is the current task status right now?") == ""
    assert provider.prefetch("what is running now?") == ""
    assert len(calls) == before, "true live-status prompts must not hit recall"


# ── Sage REQUEST_CHANGES (t_7890c31b): bare `how is/are` live-status suppresses; ─
# procedural how-to still recalls durable guidance ─────────────────────────────


def test_prefetch_bare_how_is_are_status_suppresses_without_recall(monkeypatch):
    """A bare `how is/are <subject> …?` status question must suppress prefetch
    entirely — empty injection, no recall call. These open with the framing word
    `how` but are present status/progress reads, not how-to guidance. Today the
    provider reads the standalone `how` as durable guidance, which bypasses BOTH the
    low-signal status gate (it sets durable_guidance=True) AND the shared live-state
    gate, so stale recall leaks in. Arbitrary in-domain subjects (running tasks,
    deploy pipeline), holdout phrasings, no Brain/recall special-casing."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "stale", "content": "old task status memory", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("how are running tasks?") == ""
    assert provider.prefetch("how are the tasks going?") == ""
    assert provider.prefetch("how is the deploy pipeline doing?") == ""
    # Non-copular paraphrase of the same class: `how does <subject> look?` carries no
    # low-signal status keyword, so the ONLY thing that can suppress it is correct
    # live-state classification via the shared gate.
    assert provider.prefetch("how does the deploy pipeline look?") == ""
    assert calls == [], "bare `how is/are` status prompts must not hit recall"


def test_prefetch_how_procedural_advice_still_recalls_durable_guidance(monkeypatch):
    """Positive controls (provider mirror): procedural `how do/should/can we
    manage/monitor/use …` prompts must STILL reach recall and surface the durable
    workflow guidance — the narrowing must not over-suppress the how-to class. The
    `how should we manage running tasks?` control specifically exercises the
    low-signal status gate deferring to durable-guidance (it contains 'tasks', so
    the low-signal regex fires); the durable exemption must keep it recallable.
    EN + a KO procedural control (multilingual)."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {
            "results": [
                {
                    "id": "pref",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "Task runner workflow preference",
                    "content": (
                        "Chris's workflow: run 작업/tasks through a tmux-managed runner, "
                        "not ad-hoc running shells."
                    ),
                    "score": 0.8,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    procedural = [
        "how should we manage running tasks?",  # low-signal 'tasks' → must defer to durable guidance
        "how do we monitor running tasks?",
        "how can we use the task runner?",
        "실행 중인 작업 관리 방법 알려줘",  # KO procedural (multilingual control)
    ]
    for q in procedural:
        ctx = provider.prefetch(q)
        assert "tmux-managed runner" in ctx, f"durable how-to guidance suppressed: {q!r} -> {ctx!r}"
    assert calls, "procedural how-to prompts must hit recall, not be suppressed"


# ── Sage holdout (t_8fb29191): PASSIVE procedure/usage/manage forms reach recall ─


def test_prefetch_passive_procedure_usage_forms_reach_recall_and_inject_durable_row(monkeypatch):
    """Sage holdout (t_8fb29191): PASSIVE procedure/usage/manage questions — 'how
    are running tasks managed?', 'how is the task runner used?', 'how are running
    jobs supposed to be managed?' — are durable workflow/usage guidance, not live
    status reads. Provider prefetch must reach recall and surface the stored
    durable workflow row.

    Today the analyzer reads the passive `how is/are <subject> <participle>` form
    as live-state (running-aspect) or the provider's low-signal status gate fires
    ('how is/are', 'task'), and because the past-participle procedure verbs
    (managed/used/organized/handled/operated) are not in the durable-guidance
    vocab the durable-guidance exemption does not save them — so the provider
    suppresses these before any recall call. EN verb-paraphrase class, holdout
    phrasings, no Brain/recall special-casing. True live-status forms with the
    same subjects must still be suppressed."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {
            "results": [
                {
                    "id": "pref",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "Task runner workflow preference",
                    "content": (
                        "Chris's durable workflow: running tasks and jobs are managed, "
                        "used, organized, handled, and operated through a tmux-managed "
                        "runner, not ad-hoc shells."
                    ),
                    "score": 0.8,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    durable = [
        "how are running tasks managed?",  # Sage holdout 1 (managed)
        "how is the task runner used?",  # Sage holdout 2 (used)
        "how are running jobs supposed to be managed?",  # Sage holdout 3 (supposed-to-be)
        "how are tasks organized?",  # verb paraphrase (organized)
        "how are jobs handled?",  # verb paraphrase (handled)
        "how is the task runner operated?",  # verb paraphrase (operated)
    ]
    for q in durable:
        ctx = provider.prefetch(q)
        assert (
            "tmux-managed runner" in ctx
        ), f"passive durable form suppressed before recall: {q!r} -> {ctx!r}"
    assert calls, "passive procedure/usage prompts must hit recall, not be suppressed"

    # Control: TRUE live-status forms with the same subjects must NOT hit recall —
    # the passive-form exemption must not re-open the live-status leak.
    before = len(calls)
    for q in (
        "how are running tasks?",
        "how are the tasks going?",
        "how is the deploy pipeline doing?",
        "what is the current task status right now?",
    ):
        assert provider.prefetch(q) == "", f"true live-status must stay suppressed: {q!r}"
    assert len(calls) == before, "true live-status prompts must not hit recall"


# ── Kanban t_d4acddbc: operational-guidance expansion serves an off-topic raw recall ─


def test_prefetch_operational_guidance_expansion_serves_offtopic_raw_recall(monkeypatch):
    """Kanban t_d4acddbc (provider expansion): a passive operational durable-guidance
    prompt whose RAW short query recalls only OFF-TOPIC / low-authority rows must
    still serve durable guidance via the class-level operational-guidance expansion.

    This is the live regression the unit tests with a single static mock row could
    not catch: '/recall/v2' returns count>0 for 'how is the runner configured?' but
    the raw rows share zero topical overlap with the terse prompt AND are
    low-authority, so the strict provider filter zeroes the injection. The fix issues
    a generic expansion probe built from the prompt's OWN operational anchors
    (task/runner/job/…); that probe retrieves the durable canonical runner row, which
    survives via the operational-guidance hit even though it never repeats the word
    'configured'. The mock distinguishes raw vs expansion by the
    procedure/framing vocabulary the expansion query carries — class-level, not a
    probe-string match on the acceptance prompt."""
    import urllib.parse

    raw_calls: list[str] = []
    expansion_calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get("q", [""])[0].lower()
        # The operational-guidance expansion probe carries the closed procedure/framing
        # vocabulary (workflow/procedure/configured/…) the terse prompt does not.
        is_expansion = "workflow" in q and "procedure" in q and "configured" in q
        if is_expansion:
            expansion_calls.append(q)
            return {
                "results": [
                    {
                        "id": "rg",
                        "collection": "canonical",
                        "metadata": {"category": "preference", "review_state": "accepted"},
                        "title": "Task runner workflow preference",
                        "content": (
                            "Chris's durable workflow: the task runner is driven through a "
                            "tmux-managed runner; jobs are scheduled, not ad-hoc shells."
                        ),
                        "score": 0.7,
                    }
                ]
            }
        # Raw terse query: off-topic + low-authority session row (no runner/configured
        # overlap), exactly what the live server returns for the bare prompt.
        raw_calls.append(q)
        return {
            "results": [
                {
                    "id": "sess",
                    "collection": "rag",
                    "metadata": {"source_path": "/sessions/2026-05-21-standup.md"},
                    "title": "Session summary",
                    "score": 1.0,
                    "content": "weekly session summary: standup notes about unrelated topics.",
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    paraphrases = [
        "how is the runner configured?",  # the failing live positive
        "how are running jobs supposed to be managed?",  # holdout paraphrase
        "how is the task runner used?",  # holdout paraphrase
    ]
    for q in paraphrases:
        ctx = provider.prefetch(q)
        assert (
            "tmux-managed runner" in ctx
        ), f"operational-guidance expansion did not serve durable row: {q!r} -> {ctx!r}"
        assert (
            "session summary" not in ctx.lower()
        ), f"off-topic low-authority raw row leaked into injection: {q!r} -> {ctx!r}"
    assert expansion_calls, "operational-guidance prompts must issue the expansion probe"

    # Negative control 1 (recipe): durable-guidance but NO operational anchor → no
    # expansion probe is issued, and the off-topic/low-authority raw row is filtered
    # out, so nothing operational is injected.
    before_exp = len(expansion_calls)
    assert "tmux-managed runner" not in provider.prefetch("how do I make tomato pasta sauce?")
    assert len(expansion_calls) == before_exp, "recipe how-to must not trigger operational expansion"

    # Negative control 2 (current-status): a live status read about the same subject
    # is suppressed before any expansion — live-state precedence holds.
    before_exp = len(expansion_calls)
    assert provider.prefetch("how is the runner doing right now?") == ""
    assert provider.prefetch("what is running now?") == ""
    assert len(expansion_calls) == before_exp, "live-status prompts must not trigger expansion"


# ── Kanban t_77a7f982: birthday / date-of-birth identity contamination ─────
# A Chris birthday/DOB query must NEVER inject a different entity's birthday
# (Ellie/agent/pet) or an unrelated `when` fact. When Chris's birthday is unknown
# the prefetch must be empty. A legitimate explicit third-person birthday query is
# still served. Generic class-level guard, EN + KO, no probe-string hardcode.


def test_prefetch_chris_birthday_query_never_injects_other_entity_birthday(monkeypatch):
    """Critical contamination case: 'what is my birthday?' must not inject Ellie's
    birthday even though the row shares the 'birthday' token. Provider returns
    empty (Chris's birthday is unknown)."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "ellie",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Ellie birthday",
                    "content": "Ellie's birthday is December 27, 2021.",
                    "score": 0.99,
                },
                {
                    "id": "ops",
                    "collection": "canonical",
                    "title": "Chris ops note",
                    "content": "Chris runs the brain server on port 8791.",
                    "score": 0.8,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    for q in ("what is my birthday?", "when is Chris's birthday?", "내 생일은 언제야?", "Chris 생일 언제야?"):
        ctx = provider.prefetch(q)
        assert "ellie" not in ctx.lower(), f"Ellie birthday leaked into {q!r}: {ctx!r}"
        assert "december 27" not in ctx.lower(), f"other-entity DOB leaked into {q!r}: {ctx!r}"
        assert ctx == "", f"unknown Chris birthday must yield empty prefetch: {q!r} -> {ctx!r}"


def test_prefetch_chris_birthday_query_drops_unrelated_when_facts(monkeypatch):
    """'when is Chris's birthday?' must not inject unrelated `when`/timing facts
    about Chris (evening-email timing, Korean-name recall) that share 'chris'/'when'
    but never state his birthday."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "timing",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Chris email timing",
                    "content": "Chris tends to send emails in the evening, around 9pm.",
                    "score": 0.97,
                },
                {
                    "id": "name",
                    "collection": "canonical",
                    "title": "Chris Korean name",
                    "content": "Chris's Korean name is 조대현 (Daehyun Cho).",
                    "score": 0.9,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("when is Chris's birthday?") == ""
    assert provider.prefetch("when is Chris's date of birth?") == ""


def test_prefetch_chris_birthday_query_injects_chris_birthday_when_known(monkeypatch):
    """Positive control: when Chris's OWN birthday is stored, the birthday query
    surfaces it (the guard scopes to identity, it does not blanket-empty)."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "chris",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "review_state": "accepted"},
                    "title": "Chris birthday",
                    "content": "Chris's birthday is March 3.",
                    "score": 0.8,
                },
                {
                    "id": "ellie",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Ellie birthday",
                    "content": "Ellie's birthday is December 27, 2021.",
                    "score": 0.99,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("what is my birthday?")
    assert "march 3" in ctx.lower(), f"Chris's own birthday must surface: {ctx!r}"
    assert "ellie" not in ctx.lower(), f"other-entity birthday leaked alongside: {ctx!r}"


def test_prefetch_explicit_third_person_birthday_query_is_allowed(monkeypatch):
    """A legitimate explicit third-person birthday query ('When is Ellie's
    birthday?') still surfaces that entity's birthday — the guard is identity-scoped,
    not a blanket birthday suppressor."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "ellie",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "review_state": "accepted"},
                    "title": "Ellie birthday",
                    "content": "Ellie's birthday is December 27, 2021.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("When is Ellie's birthday?")
    assert "december 27" in ctx.lower(), f"legit third-person birthday suppressed: {ctx!r}"


# ── Kanban t_21eba883: generalized personal-attribute identity/attribute guard ─
# Birthday generalized to arbitrary personal attributes (address, phone, legal
# name). A self/Chris attribute query must NEVER inject another entity's value, a
# DIFFERENT attribute of the same identity, or an unrelated row; an explicit
# third-person attribute query is still served. EN + KO, no probe-string hardcode.


def test_prefetch_self_address_query_drops_other_entity_wrong_attribute_unrelated(monkeypatch):
    """'what is my address?' / '내 주소가 뭐야?' must drop Ellie's address (wrong
    subject), Chris's phone (wrong attribute), a birthday row, and an unrelated ops
    row. Chris's address is unknown here, so prefetch is empty."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "ellie_addr",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Ellie address",
                    "content": "Ellie's address is 12 Oak St, Irvine.",
                    "score": 0.99,
                },
                {
                    "id": "chris_phone",
                    "collection": "canonical",
                    "metadata": {"category": "fact"},
                    "title": "Chris phone",
                    "content": "Chris's phone is 555-0100.",
                    "score": 0.95,
                },
                {
                    "id": "ellie_bday",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Ellie birthday",
                    "content": "Ellie's birthday is December 27, 2021.",
                    "score": 0.9,
                },
                {
                    "id": "ops",
                    "collection": "canonical",
                    "title": "ops note",
                    "content": "Chris runs the brain server on port 8791.",
                    "score": 0.8,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    for q in ("what is my address?", "내 주소가 뭐야?"):
        ctx = provider.prefetch(q)
        low = ctx.lower()
        assert "ellie" not in low, f"other-entity row leaked into {q!r}: {ctx!r}"
        assert "555-0100" not in low, f"wrong-attribute (phone) row leaked into {q!r}: {ctx!r}"
        assert "port 8791" not in low, f"unrelated row leaked into {q!r}: {ctx!r}"
        assert ctx == "", f"unknown self address must yield empty prefetch: {q!r} -> {ctx!r}"


def test_prefetch_self_address_query_injects_chris_address_when_known(monkeypatch):
    """Positive control: when Chris's OWN address is stored, the address query
    surfaces it and still drops the other-entity address."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "chris_addr",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "review_state": "accepted"},
                    "title": "Chris address",
                    "content": "Chris's address is 1 Main St, Irvine.",
                    "score": 0.8,
                },
                {
                    "id": "ellie_addr",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Ellie address",
                    "content": "Ellie's address is 12 Oak St.",
                    "score": 0.99,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("what is my address?")
    assert "1 main st" in ctx.lower(), f"Chris's own address must surface: {ctx!r}"
    assert "ellie" not in ctx.lower(), f"other-entity address leaked: {ctx!r}"


def test_prefetch_self_phone_query_drops_address_and_other_entity(monkeypatch):
    """A self phone query keeps only Chris's phone — Chris's address (wrong
    attribute) and Ellie's phone (wrong subject) are dropped."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "chris_phone",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "review_state": "accepted"},
                    "title": "Chris phone",
                    "content": "Chris's phone is 555-0100.",
                    "score": 0.8,
                },
                {
                    "id": "chris_addr",
                    "collection": "canonical",
                    "metadata": {"category": "fact"},
                    "title": "Chris address",
                    "content": "Chris's address is 1 Main St.",
                    "score": 0.95,
                },
                {
                    "id": "ellie_phone",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact"},
                    "title": "Ellie phone",
                    "content": "Ellie's phone is 555-0200.",
                    "score": 0.99,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("what is my phone number?")
    low = ctx.lower()
    assert "555-0100" in low, f"Chris's own phone must surface: {ctx!r}"
    assert "1 main st" not in low, f"wrong-attribute (address) leaked: {ctx!r}"
    assert "555-0200" not in low, f"other-entity phone leaked: {ctx!r}"


def test_prefetch_explicit_third_person_attribute_query_is_allowed(monkeypatch):
    """A legitimate explicit third-person attribute query ('what is Ellie's
    address?') still surfaces that entity's value — identity+attribute-scoped, not
    a blanket suppressor."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "ellie_addr",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "review_state": "accepted"},
                    "title": "Ellie address",
                    "content": "Ellie's address is 12 Oak St, Irvine.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    ctx = provider.prefetch("what is Ellie's address?")
    assert "12 oak st" in ctx.lower(), f"legit third-person address suppressed: {ctx!r}"


def test_prefetch_drops_low_confidence_rows_even_when_topical(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "weak",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact", "confidence": 0.2},
                    "title": "Chris address",
                    "content": "Chris's address is 9 Unverified Way.",
                    "score": 0.99,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("what is my address?") == ""


def test_prefetch_korean_entity_attribute_quality_matrix_suppresses_contamination(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "chris_addr_low",
                    "collection": "semantic_memory",
                    "metadata": {"category": "fact", "confidence": 0.25},
                    "title": "Chris address",
                    "content": "Chris's address is low confidence unverified text.",
                    "score": 0.99,
                },
                {
                    "id": "ellie_addr",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "confidence": 0.9},
                    "title": "Ellie address",
                    "content": "Ellie's address is 12 Oak St.",
                    "score": 0.98,
                },
                {
                    "id": "chris_phone",
                    "collection": "canonical",
                    "metadata": {"category": "fact", "confidence": 0.9},
                    "title": "Chris phone",
                    "content": "Chris's phone is 555-0100.",
                    "score": 0.97,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    for q in ("크리스 주소가 뭐야?", "내 주소가 뭐야?"):
        ctx = provider.prefetch(q)
        low = ctx.lower()
        assert ctx == ""
        assert "ellie" not in low
        assert "555-0100" not in low
        assert "low confidence" not in low


# ── t_2a086a4c: open-ended personal_factoid gate, off-route guarantee, usage ──


def test_prefetch_open_ended_personal_factoid_negative_injects_nothing(monkeypatch):
    """Provider prefetch mirrors /recall/v2's open-ended personal_factoid gate for
    a pure personal-fact probe (no matched route, no constraint intent). 'Chris
    childhood … first grade teacher' must inject nothing when the only hit is an
    unrelated design/UI row whose 'first'/'grade' tokens are merely hyphen-compound
    fragments ('content-first', 'production-grade'), never real attributes."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "erl",
                    "collection": "semantic_memory",
                    "title": "erl_extraction design",
                    "content": "content-first layout with production-grade UI components.",
                    "score": 200.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "jenna"

    assert provider.prefetch("Chris childhood elementary school first grade teacher") == ""


def test_prefetch_calendar_query_drops_offroute_cost_billing_guarantee(monkeypatch):
    """A route_guarantee row survives provider prefetch only when its route is one
    the ORIGINAL query matched. The calendar/reminders preference query does not
    match the cost_billing route (only the constraint EXPANSION does), so a leaked
    cost_billing guarantee is dropped while the calendar preference still surfaces."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "cost-rg",
                    "collection": "canonical",
                    "source_type": "route_guarantee",
                    "title": "cost_billing route guarantee",
                    "content": (
                        "Chris is cost-conscious: prefer existing subscriptions over new paid "
                        "API billing or local model hosting."
                    ),
                    "metadata": {"authority_tier": "direct_current_truth"},
                    "score": 300.0,
                },
                {
                    "id": "cal",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "Calendar and reminders preference",
                    "content": (
                        "Chris prefers Apple Calendar and Apple Reminders as the primary "
                        "calendar and reminders tooling."
                    ),
                    "score": 120.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "jenna"

    ctx = provider.prefetch("What does Chris prefer for Apple Calendar and Reminders?")
    low = ctx.lower()
    assert "apple calendar" in low and "apple reminders" in low, f"calendar preference missing: {ctx!r}"
    assert "cost_billing route guarantee" not in ctx, f"off-route cost guarantee leaked: {ctx!r}"


def test_prefetch_durable_preference_with_usage_word_is_not_suppressed(monkeypatch):
    """A durable preference question that merely contains a low-signal status word
    ('usage') must not be suppressed pre-search: the durable-advice intent
    ('prefer') overrides the low-signal-status gate (its documented contract), so
    the cost preference reaches the prompt instead of an empty injection."""
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {
            "results": [
                {
                    "id": "cost-pref",
                    "collection": "semantic_memory",
                    "metadata": {"category": "preference"},
                    "title": "LLM provider cost preference",
                    "content": (
                        "Chris prefers existing subscription LLM providers over new paid API "
                        "billing for cost control."
                    ),
                    "score": 200.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "jenna"

    ctx = provider.prefetch("What does Chris prefer for LLM provider cost billing subscription API usage?")
    assert calls, "durable-advice query must reach recall, not be suppressed pre-search"
    assert "subscription" in ctx.lower(), f"durable cost preference suppressed to empty: {ctx!r}"


def test_prefetch_personal_factoid_issues_focused_recall_variant(monkeypatch):
    """A durable personal-fact probe whose full phrasing is diluted by generic
    reminder scaffolding ('What should I remember about …') can return nothing on
    the raw query, so the provider issues a FOCUSED recall variant built from the
    distinctive personal_factoid terms (acronym + supporting tokens, scaffolding
    stripped) against canonical + semantic_memory. Generic: terms come from the
    shared analyzer, never a probe string."""
    seen: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(path).query).get("q", [""])[0]
        seen.append(q)
        toks = set(q.lower().split())
        # Live behavior: the scaffolded full query is diluted → 0 hits; only a
        # focused query on the distinctive terms retrieves the durable row.
        if {"omscs", "fall"} <= toks and "remember" not in toks:
            return {
                "results": [
                    {
                        "id": "omscs",
                        "collection": "semantic_memory",
                        "metadata": {"category": "fact"},
                        "title": "OMSCS",
                        "content": (
                            "Chris is enrolling in Georgia Tech OMSCS Fall 2026 and tracking "
                            "time tickets and course registration."
                        ),
                        "score": 180.0,
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "jenna"

    ctx = provider.prefetch("What should I remember about Chris OMSCS Fall 2026?")
    assert "georgia tech omscs" in ctx.lower(), f"durable OMSCS fact not surfaced: {ctx!r}"
    assert any(
        ({"omscs", "fall"} <= set(q.lower().split())) and "remember" not in q.lower() for q in seen
    ), f"provider issued no focused factoid recall variant: {seen}"


def test_prefetch_personal_factoid_drops_quoting_conversation_transcript(monkeypatch):
    """Pure personal-fact probe: a raw conversation / session-turn capture that
    merely QUOTES the probe terms (an ingested validation transcript with
    'User:'/'Assistant:' turns) is NOT a durable answer and must be dropped — even
    though it passes whole-word overlap — so negatives stay empty. Format/provenance
    signal, not a probe keyword; a declarative answer atom (no turn markers) is kept."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "raw_events_fts:abc",
                    "source_type": "raw_events_fts",
                    "title": "atoms_hot_path: User: childhood school question",
                    "content": (
                        "User: ask Chris about his childhood elementary school first grade "
                        "teacher to test recall noise\nAssistant: noted, will verify."
                    ),
                    "score": 300.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "jenna"

    assert provider.prefetch("Chris childhood elementary school first grade teacher") == ""


def test_prefetch_personal_factoid_keeps_declarative_answer_over_transcript(monkeypatch):
    """Mirror control: when a focused factoid recall returns BOTH a declarative
    answer atom and a quoting session-turn transcript, only the declarative answer
    (no 'User:'/'Assistant:' turn markers) is injected — the transcript is dropped."""

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "transcript",
                    "source_type": "raw_events_fts",
                    "title": "atoms_hot_path: User: time ticket question",
                    "content": (
                        "User: how does Chris check OMSCS time tickets for Fall 2026?\n"
                        "Assistant: via the registration portal."
                    ),
                    "score": 320.0,
                },
                {
                    "id": "answer",
                    "source_type": "raw_events_fts",
                    "title": "atoms_hot_path: OMSCS enrollment",
                    "content": (
                        "OMSCS: Chris is enrolling in Georgia Tech OMSCS Fall 2026 and tracking "
                        "time tickets and course registration."
                    ),
                    "score": 180.0,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()
    provider._profile = "jenna"

    ctx = provider.prefetch("What should I remember about Chris OMSCS Fall 2026?")
    low = ctx.lower()
    assert "georgia tech omscs" in low, f"declarative answer dropped: {ctx!r}"
    assert (
        "assistant:" not in low and "via the registration portal" not in low
    ), f"quoting transcript leaked: {ctx!r}"


# ── Generic ranking: off-topic generic-constraint-phrase contamination (t_1130ed6d) ──
# A calendar/reminders prompt was contaminated by music/TTS/image/cost rows that
# matched only a GENERIC cost-constraint phrase ("subscription-backed"/"no paid").
# These prove the hard-constraint rank-tier requires on-query topical relevance,
# the owner name alone is not relevance, and Korean domain nouns bridge to English.

_CAL_TOOLING_ROW = {
    "collection": "canonical",
    "source_type": "canonical",
    "title": "primary tooling choices",
    "metadata": {"review_state": "accepted", "category": "preference"},
    "content": (
        "Chris uses Apple Calendar and Apple Reminders as his primary calendar/reminder "
        "tooling; Google Calendar by default."
    ),
    "score": 110.0,
}
_OFFTOPIC_MUSIC_ROW = {
    "collection": "distilled",
    "source_type": "distilled",
    "title": "Decision: music/TTS + image route",
    "content": (
        "Chris's preferred route: existing/local subscription-backed tooling rather than "
        "adding new paid APIs; GPT Images 2."
    ),
    "score": 320.0,
}
_OFFTOPIC_COST_ROW = {
    "collection": "distilled",
    "source_type": "distilled",
    "title": "Decision: LLM cost posture",
    "content": "No separate paid SaaS API billing; prefer existing subscriptions; local models embeddings-only.",
    "score": 280.0,
}


def test_rank_score_offtopic_constraint_phrase_does_not_outrank_calendar_row_en():
    """EN positive: the relevant calendar tooling row must outrank off-topic
    cost/music rows that match only a generic cost-constraint phrase."""
    q = "What should I remember about Chris using Calendar and Reminders?"
    cal = BrainMemoryProvider._rank_score(_CAL_TOOLING_ROW, query=q)
    assert cal > BrainMemoryProvider._rank_score(_OFFTOPIC_MUSIC_ROW, query=q)
    assert cal > BrainMemoryProvider._rank_score(_OFFTOPIC_COST_ROW, query=q)


def test_rank_score_offtopic_constraint_phrase_does_not_outrank_calendar_row_ko():
    """Multilingual positive: same contract for a Korean calendar/reminder prompt
    whose nouns carry particles (일정이랑/리마인더는) — the KO→EN topic bridge gives
    the English Apple Calendar/Reminders atom real overlap so it leads."""
    q = "크리스 일정이랑 리마인더는 어떤 도구를 써야 해?"
    cal = BrainMemoryProvider._rank_score(_CAL_TOOLING_ROW, query=q)
    assert cal > BrainMemoryProvider._rank_score(_OFFTOPIC_MUSIC_ROW, query=q)
    assert cal > BrainMemoryProvider._rank_score(_OFFTOPIC_COST_ROW, query=q)


def test_rank_score_constraint_tier_preserved_for_ontopic_cost_query():
    """Negative control: a genuine cost/tooling prompt still grants the
    hard-constraint rank-tier to the ON-topic cost row (it shares paid/local/model
    overlap), so it outranks an unrelated calendar row — the relevance gate must
    not strip a legitimately on-topic constraint hit."""
    q = "When recommending a new LLM tool, should Chris use a new paid API or local model hosting?"
    assert BrainMemoryProvider._rank_score(_OFFTOPIC_COST_ROW, query=q) > BrainMemoryProvider._rank_score(
        _CAL_TOOLING_ROW, query=q
    )


def test_rank_score_owner_name_alone_is_not_topical_overlap():
    """The owner's own name is non-discriminating in an owner-scoped corpus: a row
    sharing ONLY 'Chris' with the prompt contributes zero topical overlap."""
    q = "What should I remember about Chris using Calendar and Reminders?"
    owner_only = {
        "collection": "distilled",
        "title": "Decision X",
        "content": "Chris decided something unrelated about budgets.",
        "score": 100.0,
    }
    assert BrainMemoryProvider._topical_overlap(owner_only, q) == 0


def test_query_topics_bridges_korean_domain_nouns_to_english():
    """A Korean calendar/reminder prompt must emit English topic equivalents so the
    English-biased overlap scorer can match an English durable atom."""
    topics = set(BrainMemoryProvider._query_topics("크리스 일정이랑 리마인더는 어떤 도구를 써야 해?"))
    assert {"calendar", "reminder"} <= topics
    # A non-domain Korean fact probe yields no spurious tooling bridge tokens.
    assert "calendar" not in set(BrainMemoryProvider._query_topics("크리스 OMSCS 2026년 가을"))
