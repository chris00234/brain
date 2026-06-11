"""Unit tests for first-class route guarantees + the prefetch policy.

Pins: durable facts are loaded and separated from search variants; matching is
token-boundary safe (`ui`/`tui` never fire inside `quality`/`intuitive`); the
declarative-shape test rejects bare probes; runtime_distinction requires a
distinction cue (a bare setup question must NOT match). Uses class-level tokens
and paraphrases, never exact acceptance-probe strings.
"""

import pytest
from recall_governance import prefetch_policy as pp
from recall_governance import route_guarantees as rg
from recall_governance.source_authority import AuthorityTier


def _ids(text: str) -> set[str]:
    return {g.id for g in rg.match_route_guarantees(text)}


def _routes(text: str) -> set[str]:
    return rg.matched_route_tags(text)


# ── Loading + fact/variant separation ─────────────────────────────────────


def test_codex_guarantee_fact_loads_and_separates_from_search_variants():
    facts = [
        g
        for g in rg.match_route_guarantees("How do I run codex with hermes for quality?")
        if g.route == "codex_workflow"
    ]
    assert facts, "codex_workflow guarantee should fire"
    fact = facts[0]
    assert fact.authority == AuthorityTier.DIRECT_CURRENT_TRUTH
    assert fact.status == "current"
    assert "interactive terminal-like tmux TUI" in fact.text
    assert "headless codex exec" in fact.text
    # search_variants are retrieval strings, distinct from the durable fact text
    assert fact.search_variants
    assert all(v != fact.text for v in fact.search_variants)


def test_runtime_distinction_fact_is_first_class():
    facts = [
        g
        for g in rg.match_route_guarantees("What is the current OpenClaw vs Hermes runtime distinction?")
        if g.route == "runtime_distinction"
    ]
    assert facts, "runtime_distinction guarantee should fire"
    assert facts[0].authority == AuthorityTier.DIRECT_CURRENT_TRUTH
    assert "Hermes" in facts[0].text and "historical" in facts[0].text.lower()


# ── Matching: token boundaries + required cues ─────────────────────────────


def test_codex_route_requires_codex_token_and_support():
    # codex + support token (quality/tmux/tui/hermes) → match
    assert "codex_workflow" in _routes("should I use codex through hermes tmux tui")
    assert "codex_workflow" in _routes("코덱스 품질 중요할 때 어떻게 써")
    # 'quality' alone (no codex) must NOT match codex_workflow
    assert "codex_workflow" not in _routes("how do I improve brain recall quality")
    # codex with no support token at all → no match (avoids bare-keyword over-fire)
    assert "codex_workflow" not in _routes("what is codex")


def test_runtime_distinction_needs_both_runtimes_and_a_distinction_cue():
    assert "runtime_distinction" in _routes("OpenClaw vs Hermes runtime distinction")
    assert "runtime_distinction" in _routes("Is Chris using OpenClaw now or Hermes now?")
    assert "runtime_distinction" in _routes("오픈클로랑 헤르메스 현재 런타임 차이 알려줘")
    # both runtimes but a SETUP question (no distinction cue) → must NOT match
    assert "runtime_distinction" not in _routes("OpenClaw and Hermes setup guide")
    assert "runtime_distinction" not in _routes("OpenClaw와 Hermes 설정 방법 알려줘")
    # only one runtime → no match
    assert "runtime_distinction" not in _routes("what is the hermes runtime")


def test_keyword_matching_is_token_boundary_safe():
    # short tokens must match on word boundaries, never as substrings
    assert rg._keyword_matches("codex", lowered="run codex now") is True
    assert rg._keyword_matches("tui", lowered="open a tui session") is True
    assert rg._keyword_matches("tui", lowered="this is intuitive") is False
    assert rg._keyword_matches("ui", lowered="improve recall quality") is False
    assert rg._keyword_matches("hermes", lowered="hermes runtime") is True


# ── Declarative-guarantee shape test ───────────────────────────────────────


def test_is_declarative_route_guarantee_excludes_search_probes():
    assert rg.is_declarative_route_guarantee("design standard") is False
    assert rg.is_declarative_route_guarantee("openclaw agent configuration heartbeat") is False
    assert rg.is_declarative_route_guarantee("image caption description") is False
    assert (
        rg.is_declarative_route_guarantee(
            "Chris prefers using Codex through Hermes as an interactive terminal-like "
            "tmux TUI when quality or steering matters; headless codex exec is only for "
            "bounded automation"
        )
        is True
    )


def test_no_exact_acceptance_probe_phrases_in_facts():
    """Production facts must be durable policy text, not Sage matrix probes."""
    facts = rg.match_route_guarantees("codex hermes quality OpenClaw runtime distinction")
    banned = [
        "how should i run codex when quality or steering matters for chris",
        "what is the current openclaw vs hermes runtime distinction for chris",
    ]
    for f in facts:
        assert f.text.strip().lower() not in banned


def test_match_is_fail_open_on_empty_or_garbage():
    assert rg.match_route_guarantees("") == []
    assert rg.matched_route_tags("") == set()


# ── Prefetch policy ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode,allow_low_authority,bias",
    [
        ("interactive", True, "low"),
        ("active", False, "medium"),
        ("provider_prefetch", False, "high"),
        ("raw", True, "low"),
    ],
)
def test_policy_for_modes(mode, allow_low_authority, bias):
    policy = pp.policy_for(mode)
    assert policy.mode == mode
    assert policy.allow_low_authority is allow_low_authority
    assert policy.false_positive_bias == bias


def test_policy_for_unknown_defaults_to_interactive():
    assert pp.policy_for("nonsense").mode == "interactive"


# ── Live-failure regressions: cost route + codex preference support ────────


def test_cost_billing_route_matches_cost_query_not_image():
    assert "cost_billing" in _routes(
        "Suggest an LLM tool that avoids new paid API billing and self-hosted local models"
    )
    assert "cost_billing" in _routes("과금 없이 구독으로 갈 수 있는 LLM 도구 추천해줘")
    # image query: a 'billing' token is present but no cost-support token, so the
    # generic cost route must NOT fire (image has its own handling).
    assert "cost_billing" not in _routes("Generate pictures for me without piling on extra billing.")


def test_cost_billing_fact_carries_constraint_terms():
    facts = [
        g
        for g in rg.match_route_guarantees("recommend an llm tool with no paid api billing or local model")
        if g.route == "cost_billing"
    ]
    assert facts, "cost_billing guarantee should fire"
    text = facts[0].text.lower()
    for term in ("subscription", "existing", "integrations", "paid api", "local model"):
        assert term in text, f"cost fact missing constraint term: {term}"


def test_codex_route_matches_preference_query():
    # A "Codex preference" (not a summary) query must route to codex_workflow via
    # the preference support token — regression for the Codex-preference class
    # where summary/digest rows otherwise dominate the direct preference.
    assert "codex_workflow" in _routes("Tell me Chris's actual Codex workflow preference, not a digest.")


def test_guarantee_tokens_excludes_function_and_subject_words():
    """guarantee_tokens must keep distinctive route/fact vocabulary but strip
    generic function/subject/policy words, so a row 'serves' a guarantee only on
    distinctive-term overlap, not common-word overlap."""
    facts = rg.match_route_guarantees("openclaw hermes runtime distinction current historical")
    g = next(x for x in facts if x.route == "runtime_distinction")
    toks = rg.guarantee_tokens(g)
    assert {"openclaw", "hermes", "runtime", "current", "historical"} <= toks
    assert not ({"chris", "is", "the", "do", "not", "as"} & toks)


# ── Brain recall/prefetch-quality route (durable eval policy, EN+KO) ───────


def test_brain_recall_quality_route_matches_brain_quality_query_not_generic_brain():
    # brain + a recall/quality cue → fires (EN + KO paraphrases)
    assert "brain_recall_quality" in _routes("how good is brain recall and prefetch quality")
    assert "brain_recall_quality" in _routes("브레인 리콜 품질 평가에서 노이즈 줄이는 기준")
    # summarization of recall tuning still routes here (brain + recall + tuning)
    assert "brain_recall_quality" in _routes("summarize recent brain recall tuning work")
    # 'brain' with no recall/quality cue must NOT over-fire
    assert "brain_recall_quality" not in _routes("where does the brain server store backups")
    # a recall/quality cue with no brain subject must NOT fire
    assert "brain_recall_quality" not in _routes("how do I improve search quality on my blog")


def test_brain_recall_quality_fact_carries_quality_and_noise_terms():
    facts = [
        g
        for g in rg.match_route_guarantees("brain recall prefetch quality right now")
        if g.route == "brain_recall_quality"
    ]
    assert facts, "brain_recall_quality guarantee should fire"
    text = facts[0].text.lower()
    # EN consumers need prefetch/memory-context; KO consumers need noise/노이즈.
    for term in ("recall", "prefetch", "memory context", "quality", "noise", "eval"):
        assert term in text, f"brain-quality fact missing EN term: {term}"
    assert "노이즈" in facts[0].text and "리콜" in facts[0].text


# ── Image-generation billing route (subscription, not new paid API/local) ──


def test_image_generation_billing_route_matches_image_billing_query_not_generic():
    assert "image_generation_billing" in _routes(
        "what should we use for image generation without extra billing"
    )
    assert "image_generation_billing" in _routes("이미지 생성은 추가 과금 없이 뭐가 맞아")
    # image recall with no billing/generation cue must NOT fire
    assert "image_generation_billing" not in _routes("what was the image I sent you yesterday")
    # billing query with no image subject must NOT fire
    assert "image_generation_billing" not in _routes("recommend a tool without extra api billing")


def test_image_generation_billing_fact_routes_to_subscription_no_separate_billing():
    facts = [
        g
        for g in rg.match_route_guarantees("image generation without extra billing")
        if g.route == "image_generation_billing"
    ]
    assert facts, "image_generation_billing guarantee should fire"
    text = facts[0].text.lower()
    for term in ("image", "openai", "subscription", "separate"):
        assert term in text, f"image fact missing term: {term}"
    # The fact is the direct truth, so it must not itself read as a banned phrase.
    assert "local image model" not in text and "paid api spend" not in text


# ── Holdout regression (t_c7453635): cost class without a spend noun ───────


def test_cost_billing_route_matches_avoid_paid_self_hosting_paraphrase():
    """Cost class expressed WITHOUT a spend noun (no billing/cost/budget/
    subscription) — via 'paid' + tooling/model/hosting framing — must still route
    to cost_billing so the durable cost guarantee can be injected. Holdout cost
    failure class; class-level tokens, no probe string."""
    assert "cost_billing" in _routes(
        "Choose an AI tooling path that avoids new paid APIs and avoids self-hosting generation models."
    )
    assert "cost_billing" in _routes("Pick an approach with no paid API and no locally hosted model.")
    assert "cost_billing" in _routes("새 유료 API 없이 구독으로 쓸 LLM 모델 골라줘")
    # Gated: a spend token with NO tooling/model context must NOT fire.
    assert "cost_billing" not in _routes("how much did the trip cost me")
    # Gated: a tooling token with NO spend/pricing context must NOT fire.
    assert "cost_billing" not in _routes("recommend a workflow for code review")


# ── Negation / positive-evidence route arbitration (REQUEST_CHANGES f4) ────


def test_route_not_matched_when_primary_token_negated_en_ko():
    """Explicit negation of the primary route token must defeat the route even
    when support tokens (quality/workflow) — common in frontend/design critique —
    are present. Route guarantees require POSITIVE route evidence, not keyword
    residue inside a negation. EN + KO."""
    assert "codex_workflow" not in _routes(
        "This is not about codex — give me a frontend design critique on the workflow quality."
    )
    assert "codex_workflow" not in _routes(
        "코덱스 얘기 아니라 프론트엔드 디자인 품질을 워크플로우 관점에서만 봐줘"
    )
    # positive controls: a NON-negated codex mention with support still routes
    assert "codex_workflow" in _routes("should I use codex through hermes for quality steering")
    assert "codex_workflow" in _routes("코덱스 품질 중요할 때 어떻게 써")
    # constraint-style "no X" phrasing is NOT topic negation — the cost route is
    # DEFINED by it and must still fire (regression guard for the negation fix).
    assert "cost_billing" in _routes("pick an approach with no paid API and no local models")


def test_keyword_matches_skips_negated_occurrences():
    assert rg._keyword_matches("codex", lowered="this is not about codex") is False
    assert rg._keyword_matches("codex", lowered="i did not choose codex here") is False
    assert rg._keyword_matches("codex", lowered="use codex for quality steering") is True
    # a later positive mention still counts as a match
    assert rg._keyword_matches("codex", lowered="not claude; should i use codex?") is True
    # bare "no" is constraint phrasing, not topic exclusion → still a match
    assert rg._keyword_matches("codex", lowered="no codex frills, just use codex") is True
