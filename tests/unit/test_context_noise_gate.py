"""Context-noise gate for boot-context surfaces (working_memory + learn + boot_context).

Session summaries and manual focus items are rendered verbatim into every boot
context. Harness artifacts (raw <local-command-stdout> captures, ANSI/SGR
residue, echoed agent-instruction scaffolds) must be rejected at store time AND
suppressed at read time, and an empty boot context must not be wrapped in a
visible memory block. Format/shape signals only — these tests include EN+KO
positives and negative controls that must always be kept.
"""

from __future__ import annotations

import pytest
import working_memory as WM
from boot_context import EMPTY_BOOT_CONTEXT_SENTINEL, format_boot_context

# ── is_context_noise: noise class (must be suppressed) ──────────────────

NOISE_ROWS = [
    "<local-command-stdout>Set model to Opus 4.8 and saved as default</local-command-stdout>",
    "Set model to \x1b[1mOpus 4.8\x1b[22m and saved as your default for new sessions",
    "Set model to [1mOpus 4.8[22m and saved as your default for new sessions",
    "You are running as Claude Code inside tmux session `claude3` for Chris Cho.",
    "you are an assistant that must answer in Korean",
    "당신은 텔레그램 어시스턴트로 동작합니다",
    "<task-notification><task-id>abc</task-id></task-notification>",
    "",
    "   ",
]

# ── is_context_noise: legitimate rows (negative controls, must be kept) ──

CLEAN_ROWS = [
    "Fixed the brain recall governance SLO and shipped eval improvements.",
    "브레인 리콜 품질 개선 작업을 진행하고 eval 점수를 기록함",
    "Reviewed docker-compose changes; deployment stays under the <300ms budget",
    "Chris asked: are you around to review the Hermes telegram fix?",
    "Investigated why hermes agents stopped replying on Telegram after the update.",
]


@pytest.mark.parametrize("content", NOISE_ROWS)
def test_context_noise_detected(content):
    assert WM.is_context_noise(content) is True, content


@pytest.mark.parametrize("content", CLEAN_ROWS)
def test_legit_content_kept(content):
    assert WM.is_context_noise(content) is False, content


# ── store/read integration on a sandboxed DB ────────────────────────────


@pytest.fixture
def wm_db(tmp_path, monkeypatch):
    monkeypatch.setattr(WM, "DB_PATH", tmp_path / "autonomy.db")
    WM._init_db()
    return WM


def test_add_session_summary_rejects_noise(wm_db):
    res = wm_db.add_session_summary(NOISE_ROWS[0], agent="claude")
    assert res.get("skipped") == "context_noise"
    assert wm_db.get_session_summaries() == []


def test_add_session_summary_stores_clean_content(wm_db):
    res = wm_db.add_session_summary(CLEAN_ROWS[0], agent="claude")
    assert res.get("skipped") is None
    contents = [s["content"] for s in wm_db.get_session_summaries()]
    assert contents == [CLEAN_ROWS[0]]


def test_get_session_summaries_filters_legacy_noise_rows(wm_db):
    """Rows stored before the gate existed must be suppressed at read time."""
    wm_db.add_focus(NOISE_ROWS[0], category=WM.SESSION_SUMMARY_CATEGORY)
    wm_db.add_focus(NOISE_ROWS[3], category=WM.SESSION_SUMMARY_CATEGORY)
    wm_db.add_focus(CLEAN_ROWS[1], category=WM.SESSION_SUMMARY_CATEGORY)
    contents = [s["content"] for s in wm_db.get_session_summaries()]
    assert contents == [CLEAN_ROWS[1]]


def test_get_working_context_filters_noise_focus_items(wm_db):
    wm_db.add_focus(NOISE_ROWS[1], category="focus")
    wm_db.add_focus(CLEAN_ROWS[0], category="focus")
    focus_contents = [f["content"] for f in wm_db.get_working_context()["manual_focus"]]
    assert CLEAN_ROWS[0] in focus_contents
    assert all(not WM.is_context_noise(c) for c in focus_contents)


# ── boot context formatting: empty prefetch stays invisible ─────────────


def test_format_boot_context_empty_returns_bare_sentinel():
    out = format_boot_context("claude", [])
    assert out == EMPTY_BOOT_CONTEXT_SENTINEL
    assert "[Unified Boot Context]" not in out
    assert "Loaded 0 context blocks" not in out


def test_format_boot_context_nonempty_keeps_header():
    out = format_boot_context(
        "claude", [{"section": "Current Focus", "content": "- GOAL: x", "source": "brain/working_memory"}]
    )
    assert "[Unified Boot Context]" in out
    assert "### Current Focus" in out


# ── learn fallback summary: noise never becomes the session summary ─────


def test_heuristic_summary_skips_noise_user_messages():
    from learn import _heuristic_summary

    transcript = (
        "User: Investigated why hermes agents stopped replying on Telegram after the update.\n"
        "Assistant: Found the adapter config issue.\n"
        "User: <local-command-stdout>Set model to Opus 4.8 and saved as default</local-command-stdout>\n"
    )
    summary = _heuristic_summary(transcript)
    assert summary is not None
    assert "<local-command-stdout>" not in summary
    assert "Telegram" in summary


def test_heuristic_summary_all_noise_returns_none():
    from learn import _heuristic_summary

    transcript = "User: You are running as Claude Code inside tmux session `claude3` for Chris Cho today.\n"
    assert _heuristic_summary(transcript) is None
