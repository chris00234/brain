from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_active_recall_reexports_openclaw_hermes_classifiers():
    import active_recall
    from recall_governance import openclaw_hermes

    assert (
        active_recall._looks_like_openclaw_hermes_distinction_prompt
        is openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt
    )
    assert (
        active_recall._is_openclaw_hermes_distinction_result
        is openclaw_hermes.is_openclaw_hermes_distinction_result
    )
    assert active_recall._is_openclaw_hermes_handoff_noise is openclaw_hermes.is_openclaw_hermes_handoff_noise
    assert (
        active_recall._is_broad_openclaw_hermes_theme_noise
        is openclaw_hermes.is_broad_openclaw_hermes_theme_noise
    )


def test_routes_recall_reexports_openclaw_hermes_classifiers():
    import routes.recall as recall_route
    from recall_governance import openclaw_hermes

    assert (
        recall_route._is_openclaw_hermes_distinction_query
        is openclaw_hermes.is_openclaw_hermes_distinction_query
    )
    assert (
        recall_route._is_openclaw_hermes_distinction_result
        is openclaw_hermes.is_openclaw_hermes_distinction_token_result
    )
    assert recall_route._is_openclaw_setup_noise_result is openclaw_hermes.is_openclaw_setup_noise_result
    assert (
        recall_route._is_openclaw_hermes_handoff_noise_result
        is openclaw_hermes.is_openclaw_hermes_handoff_noise_result
    )


def test_distinction_prompt_positives_and_negatives():
    from recall_governance import openclaw_hermes

    assert openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt(
        "what's the OpenClaw vs Hermes distinction"
    )
    assert openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt(
        "is openclaw historical and hermes the current runtime?"
    )
    assert not openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt("openclaw hermes handoff")
    assert not openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt("hermes current runtime")
    assert not openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt("")


def test_distinction_query_positives_and_negatives():
    from recall_governance import openclaw_hermes
    from recall_governance.normalization import tokenize

    assert openclaw_hermes.is_openclaw_hermes_distinction_query(
        tokenize("openclaw vs hermes current runtime")
    )
    assert openclaw_hermes.is_openclaw_hermes_distinction_query(tokenize("openclaw hermes history"))
    assert not openclaw_hermes.is_openclaw_hermes_distinction_query(tokenize("openclaw hermes handoff"))
    assert not openclaw_hermes.is_openclaw_hermes_distinction_query(tokenize("hermes current runtime"))
    assert not openclaw_hermes.is_openclaw_hermes_distinction_query(set())


def test_active_distinction_result_positives_and_negatives():
    from recall_governance import openclaw_hermes

    assert openclaw_hermes.is_openclaw_hermes_distinction_result(
        "Runtime provenance",
        "OpenClaw is historical; the Hermes agent is the current runtime",
        None,
    )
    # Path participates in the haystack.
    assert openclaw_hermes.is_openclaw_hermes_distinction_result(
        "Agent runtimes",
        "openclaw and hermes",
        "/notes/runtime-distinction.md",
    )
    # No distinction marker → not distinction evidence.
    assert not openclaw_hermes.is_openclaw_hermes_distinction_result(
        "Roadmap",
        "openclaw and hermes roadmap discussion",
        None,
    )
    # No openclaw → not distinction evidence.
    assert not openclaw_hermes.is_openclaw_hermes_distinction_result(
        "Hermes",
        "the hermes agent is the current runtime",
        None,
    )


def test_route_distinction_token_result_positives_and_negatives():
    from recall_governance import openclaw_hermes
    from recall_governance.normalization import tokenize

    text = "openclaw is the historical runtime and hermes is current"
    assert openclaw_hermes.is_openclaw_hermes_distinction_token_result(tokenize(text), text)

    # Phrase branch fires without any marker token.
    phrase_text = "openclaw notes: the hermes agent is jenna's replacement"
    assert openclaw_hermes.is_openclaw_hermes_distinction_token_result(tokenize(phrase_text), phrase_text)

    no_marker = "openclaw and hermes roadmap discussion"
    assert not openclaw_hermes.is_openclaw_hermes_distinction_token_result(tokenize(no_marker), no_marker)

    no_openclaw = "hermes is the current runtime"
    assert not openclaw_hermes.is_openclaw_hermes_distinction_token_result(tokenize(no_openclaw), no_openclaw)


def test_setup_noise_result_positives_and_negatives():
    from recall_governance import openclaw_hermes

    assert openclaw_hermes.is_openclaw_setup_noise_result(
        {"title": "OpenClaw multi-agent setup documentation"}, "agent roster"
    )
    assert openclaw_hermes.is_openclaw_setup_noise_result(
        {"path": "/Users/chris/.openclaw/workspace-jenna/agents.md"}, "jenna instructions"
    )
    # Path falls back to result metadata.
    assert openclaw_hermes.is_openclaw_setup_noise_result(
        {"metadata": {"source_path": "~/docs/openclaw-setup/guide.md"}}, "setup guide"
    )
    assert openclaw_hermes.is_openclaw_setup_noise_result({}, "sub-agent configuration for jenna")
    assert openclaw_hermes.is_openclaw_setup_noise_result({}, "openclaw active hours for heartbeat are 9-6")
    # "openclaw setup" rows are exempt when they carry the current-runtime fact.
    assert openclaw_hermes.is_openclaw_setup_noise_result({}, "openclaw setup notes")
    assert not openclaw_hermes.is_openclaw_setup_noise_result(
        {}, "openclaw setup is archived; current runtime is hermes"
    )
    assert not openclaw_hermes.is_openclaw_setup_noise_result(
        {"title": "Runtime fact"}, "openclaw is historical, hermes is current"
    )


def test_route_handoff_noise_result_text_title_path_and_metadata_markers():
    from recall_governance import openclaw_hermes

    assert openclaw_hermes.is_openclaw_hermes_handoff_noise_result(
        {}, "Acceptance probe passed for recall tuning"
    )
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise_result(
        {"title": "Work kanban task T_123"}, "row body"
    )
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise_result(
        {"path": "/evals/generic_recipe_knowledge_gap.md"}, "row body"
    )
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise_result(
        {"metadata": {"source_name": "review-required handoff"}}, "agent run summary"
    )
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise_result(
        {"metadata": {"source_path": "/runs/verdict: partial/log.txt"}}, "agent run summary"
    )
    # Marker beyond the 1500-char text window is not scanned.
    assert not openclaw_hermes.is_openclaw_hermes_handoff_noise_result({}, " " * 1600 + "acceptance probe")
    assert not openclaw_hermes.is_openclaw_hermes_handoff_noise_result(
        {"title": "OpenClaw vs Hermes"}, "openclaw is historical; current runtime is hermes"
    )


def test_active_handoff_noise_positives_and_negatives():
    from recall_governance import openclaw_hermes

    assert openclaw_hermes.is_openclaw_hermes_handoff_noise("Work kanban task T_42", "", None)
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise(
        "Note", "no setup/live_state rows surfaced in the spot check", None
    )
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise("Run", "", "/logs/dirty patch.txt")
    assert not openclaw_hermes.is_openclaw_hermes_handoff_noise(
        "OpenClaw vs Hermes", "openclaw is historical; current runtime is hermes", None
    )


def test_broad_theme_noise_exempts_distinction_rows():
    from recall_governance import openclaw_hermes

    assert openclaw_hermes.is_broad_openclaw_hermes_theme_noise(
        "Common themes", "These notes share a common theme across agents"
    )
    assert openclaw_hermes.is_broad_openclaw_hermes_theme_noise(
        "Agents", "openclaw and hermes share a common theme"
    )
    # A row that reads as distinction evidence is exempt.
    assert not openclaw_hermes.is_broad_openclaw_hermes_theme_noise(
        "OpenClaw vs Hermes",
        "These notes share a common theme: openclaw is historical, hermes is the current runtime",
    )
    assert not openclaw_hermes.is_broad_openclaw_hermes_theme_noise("Brain backup", "nightly cron job")


def test_route_aggregate_noise_keeps_live_state_snapshot_dependency():
    import routes.recall as recall_route

    live_state_row = {"title": "Live snapshot", "path": "/brain/live_state/active_goals.md"}
    text = "Live state snapshot: active goals and focus"
    # Neither extracted classifier flags it; the route-local live-state helper does.
    assert not recall_route._is_openclaw_setup_noise_result(live_state_row, text)
    assert not recall_route._is_openclaw_hermes_handoff_noise_result(live_state_row, text)
    assert recall_route._is_live_state_snapshot_result(live_state_row, text)
    assert recall_route._is_openclaw_hermes_distinction_noise_result(live_state_row, text)

    durable_row = {"title": "OpenClaw vs Hermes"}
    durable_text = "openclaw is historical; current runtime is hermes"
    assert not recall_route._is_openclaw_hermes_distinction_noise_result(durable_row, durable_text)


def test_known_drift_result_classifiers_disagree_on_markers():
    """Active accepts "distinguish"/"provenance" substrings; the route token
    variant needs a current/runtime/historical/distinction token or a runtime
    phrase. Route accepts a bare "runtime" token; active needs the "current
    runtime" bigram. Documented, not unified."""
    from recall_governance import openclaw_hermes
    from recall_governance.normalization import tokenize

    title = "Agent provenance"
    content = "how to distinguish openclaw provenance from hermes"
    text = f"{title}\n{content}"
    assert openclaw_hermes.is_openclaw_hermes_distinction_result(title, content, None)
    assert not openclaw_hermes.is_openclaw_hermes_distinction_token_result(tokenize(text), text)

    bare_runtime = "openclaw was the legacy runtime before hermes"
    assert openclaw_hermes.is_openclaw_hermes_distinction_token_result(tokenize(bare_runtime), bare_runtime)
    assert not openclaw_hermes.is_openclaw_hermes_distinction_result("Note", bare_runtime, None)


def test_known_drift_prompt_gate_is_substring_query_gate_is_token():
    """The active prompt gate matches "current" inside "currently"; the route
    query gate tokenizes and finds no marker token. Documented, not unified."""
    from recall_governance import openclaw_hermes
    from recall_governance.normalization import tokenize

    prompt = "openclaw hermes currently?"
    assert openclaw_hermes.looks_like_openclaw_hermes_distinction_prompt(prompt)
    assert not openclaw_hermes.is_openclaw_hermes_distinction_query(tokenize(prompt))


def test_known_drift_route_handoff_noise_reads_metadata_active_does_not():
    """The route handoff classifier consults result metadata source_name/
    source_path; the active variant only sees title/path/content."""
    from recall_governance import openclaw_hermes

    row = {"metadata": {"source_name": "review-required handoff"}}
    body = "agent run summary"
    assert openclaw_hermes.is_openclaw_hermes_handoff_noise_result(row, body)
    assert not openclaw_hermes.is_openclaw_hermes_handoff_noise(body, body, None)
