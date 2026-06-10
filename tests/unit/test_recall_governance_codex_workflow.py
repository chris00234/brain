from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_active_recall_reexports_codex_workflow_classifiers():
    import active_recall
    from recall_governance import codex_workflow

    assert active_recall._looks_like_codex_workflow_prompt is codex_workflow.looks_like_codex_workflow_prompt
    assert (
        active_recall._is_codex_current_preference_result is codex_workflow.is_codex_current_preference_result
    )
    assert active_recall._is_codex_skill_sync_noise is codex_workflow.is_codex_skill_sync_noise


def test_routes_recall_reexports_codex_workflow_classifiers():
    import routes.recall as recall_route
    from recall_governance import codex_workflow

    assert recall_route._is_codex_skill_sync_noise_result is codex_workflow.is_codex_skill_sync_noise_result
    assert recall_route._is_codex_hermes_tui_query is codex_workflow.is_codex_hermes_tui_query
    assert recall_route._is_codex_hermes_tui_result is codex_workflow.is_codex_hermes_tui_result


def test_codex_workflow_prompt_classifier_positives_and_negatives():
    from recall_governance import codex_workflow

    assert codex_workflow.looks_like_codex_workflow_prompt("how should I run codex through hermes tmux")
    assert codex_workflow.looks_like_codex_workflow_prompt("codex coding recommendation")
    assert codex_workflow.looks_like_codex_workflow_prompt("코덱스 어떻게 써야 해")
    assert not codex_workflow.looks_like_codex_workflow_prompt("what is codex")
    assert not codex_workflow.looks_like_codex_workflow_prompt("hermes tmux quality steering")
    assert not codex_workflow.looks_like_codex_workflow_prompt("")


def test_codex_hermes_tui_query_positives_and_negatives():
    from recall_governance import codex_workflow
    from recall_governance.normalization import tokenize

    assert codex_workflow.is_codex_hermes_tui_query(tokenize("should I use codex through hermes tmux"))
    assert codex_workflow.is_codex_hermes_tui_query(tokenize("codex quality steering"))
    assert not codex_workflow.is_codex_hermes_tui_query(tokenize("what is codex"))
    assert not codex_workflow.is_codex_hermes_tui_query(tokenize("hermes tmux quality steering"))
    assert not codex_workflow.is_codex_hermes_tui_query(set())


def test_codex_current_preference_result_requires_preference_marker():
    from recall_governance import codex_workflow

    assert codex_workflow.is_codex_current_preference_result(
        "Codex via Hermes TUI",
        "Chris prefers running Codex inside the Hermes tmux TUI",
        None,
    )
    assert codex_workflow.is_codex_current_preference_result(
        "Workflow note",
        "코덱스 codex hermes terminal-like 선호",
        None,
    )
    # No prefers/preference/선호 marker → not a current-preference row.
    assert not codex_workflow.is_codex_current_preference_result(
        "Codex in Hermes",
        "Codex runs inside the Hermes tmux pane",
        None,
    )
    # No hermes → not a current-preference row.
    assert not codex_workflow.is_codex_current_preference_result(
        "Codex preference",
        "Chris prefers headless codex",
        None,
    )


def test_codex_hermes_tui_result_positives_and_negatives():
    from recall_governance import codex_workflow
    from recall_governance.normalization import tokenize

    text = "Chris runs codex inside the hermes tmux pane"
    assert codex_workflow.is_codex_hermes_tui_result(tokenize(text), text)

    substring_text = "codex and hermes terminal-like steering"
    assert codex_workflow.is_codex_hermes_tui_result(tokenize(substring_text), substring_text)

    no_hermes = "codex tmux session notes"
    assert not codex_workflow.is_codex_hermes_tui_result(tokenize(no_hermes), no_hermes)

    no_tui_marker = "codex and hermes roadmap discussion"
    assert not codex_workflow.is_codex_hermes_tui_result(tokenize(no_tui_marker), no_tui_marker)


def test_active_skill_sync_noise_flags_sync_rows_and_exempts_preference():
    from recall_governance import codex_workflow

    assert codex_workflow.is_codex_skill_sync_noise(
        "Codex/Claude Code skill sync",
        "Synced skills/autonomous-ai-agents into both runtimes",
        None,
    )
    assert codex_workflow.is_codex_skill_sync_noise(
        "Runtime notes",
        "codex and claude code skill comparison",
        None,
    )
    # A row that also reads as a current-preference result is exempt.
    assert not codex_workflow.is_codex_skill_sync_noise(
        "Codex skill sync",
        "Chris prefers codex inside the hermes tmux tui; skill sync details follow",
        None,
    )
    assert not codex_workflow.is_codex_skill_sync_noise("Brain backup", "nightly cron job", None)


def test_route_skill_sync_noise_flags_sync_rows_and_exempts_preference():
    from recall_governance import codex_workflow

    assert codex_workflow.is_codex_skill_sync_noise_result(
        {"title": "Codex/Claude Code skill sync"},
        "synced agent skills",
    )
    # Title/path fall back to result metadata.
    assert codex_workflow.is_codex_skill_sync_noise_result(
        {"metadata": {"document_title": "Codex/Claude Code skill sync map"}},
        "sync ledger",
    )
    # TUI-preference rows are exempt.
    assert not codex_workflow.is_codex_skill_sync_noise_result(
        {"title": "Codex preference"},
        "Chris prefers codex inside the hermes tmux tui; skill sync details follow",
    )
    assert not codex_workflow.is_codex_skill_sync_noise_result(
        {"title": "Brain backup"},
        "nightly cron job",
    )


def test_known_drift_route_result_classifier_skips_preference_marker():
    """/recall/v2 boosts codex/hermes TUI rows without a preference marker;
    /recall/active requires prefers/preference/선호. Documented, not unified."""
    from recall_governance import codex_workflow
    from recall_governance.normalization import tokenize

    title = "Codex in Hermes"
    content = "Codex runs inside the Hermes tmux pane"
    text = f"{title}\n{content}"

    assert codex_workflow.is_codex_hermes_tui_result(tokenize(text), text)
    assert not codex_workflow.is_codex_current_preference_result(title, content, None)


def test_known_drift_prompt_and_query_classifiers_disagree_on_korean_and_markers():
    """Active prompt gate accepts Korean 코덱스 and 코딩/복잡한 markers; the route
    query gate needs a literal "codex" token and adds 좋아 instead."""
    from recall_governance import codex_workflow
    from recall_governance.normalization import tokenize

    korean_prompt = "코덱스 품질 중요할 때 어떻게 써"
    assert codex_workflow.looks_like_codex_workflow_prompt(korean_prompt)
    assert not codex_workflow.is_codex_hermes_tui_query(tokenize(korean_prompt))

    preference_slang = "codex 좋아?"
    assert codex_workflow.is_codex_hermes_tui_query(tokenize(preference_slang))
    assert not codex_workflow.looks_like_codex_workflow_prompt(preference_slang)
