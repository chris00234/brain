"""Behavioral unit tests for learn.py pure helpers.

learn.py is 1517 lines and previously had only smoke-test coverage. The
pure-function helpers below have no I/O and can be tested directly:
  - _has_negation: Korean + English negation detection
  - _has_correction_signals: regex-based correction phrase matching
  - _digest: deterministic sha256-prefix hash
  - _tokenize: alnum-3+ token extraction
  - _jaccard: set-overlap ratio
  - _cosine: vector cosine similarity (incl. zero-vector edge case)
  - _heuristic_summary: last-resort summary fallback

These guard the regex/heuristic surface that drives the SessionEnd
learning pipeline. Behavior changes here would silently degrade the
brain's learning signal — exactly the regression smoke tests can't catch.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── _digest ──────────────────────────────────────────────────────────────


def test_digest_is_deterministic_16_chars():
    from learn import _digest

    d = _digest("hello world")
    assert len(d) == 16
    assert d == _digest("hello world")
    assert d != _digest("hello world!")


# ── _tokenize ────────────────────────────────────────────────────────────


def test_tokenize_alnum_only_min_length_3():
    from learn import _tokenize

    toks = _tokenize("Hello, the BRAIN says: hi-five! a x12 ya")
    # 'hi-five' is one token (hyphen allowed), 'x12' is one token,
    # short tokens 'hi'? actually 'hi-five' matches the regex
    assert "hello" in toks
    assert "brain" in toks
    assert "says" in toks
    assert "hi-five" in toks
    assert "x12" in toks
    # Below 3 chars: dropped
    assert "a" not in toks
    assert "ya" not in toks


def test_tokenize_empty_string_returns_empty_set():
    from learn import _tokenize

    assert _tokenize("") == set()


# ── _jaccard ─────────────────────────────────────────────────────────────


def test_jaccard_identical_sets_is_one():
    from learn import _jaccard

    a = {"x", "y", "z"}
    assert _jaccard(a, a) == 1.0


def test_jaccard_disjoint_sets_is_zero():
    from learn import _jaccard

    assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0


def test_jaccard_partial_overlap():
    from learn import _jaccard

    a = {"a", "b", "c"}
    b = {"b", "c", "d"}
    # Intersection: {b, c}=2, Union: {a, b, c, d}=4 → 0.5
    assert _jaccard(a, b) == 0.5


def test_jaccard_empty_set_returns_zero():
    from learn import _jaccard

    assert _jaccard(set(), {"x"}) == 0.0
    assert _jaccard({"x"}, set()) == 0.0
    assert _jaccard(set(), set()) == 0.0


# ── _cosine ──────────────────────────────────────────────────────────────


def test_cosine_identical_vectors_is_one():
    from learn import _cosine

    v = [1.0, 0.5, -0.25]
    assert abs(_cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_is_zero():
    from learn import _cosine

    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_opposite_is_negative_one():
    from learn import _cosine

    assert abs(_cosine([1.0, 0.0], [-1.0, 0.0]) - (-1.0)) < 1e-9


def test_cosine_zero_vector_returns_zero():
    from learn import _cosine

    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert _cosine([1.0, 1.0], [0.0, 0.0]) == 0.0


def test_cosine_mismatched_lengths_returns_zero():
    from learn import _cosine

    assert _cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_empty_returns_zero():
    from learn import _cosine

    assert _cosine([], [1.0]) == 0.0


# ── _has_negation ────────────────────────────────────────────────────────


def test_has_negation_english_token():
    from learn import _has_negation, _tokenize

    raw = "Don't store this"
    toks = _tokenize(raw)
    # English negation via 'not' regex or n't substring
    assert _has_negation(toks, raw) is True


def test_has_negation_korean_substring():
    """Korean negation is detected via raw-text substring, not tokens —
    _tokenize's [a-z0-9_\\-] regex strips all hangul characters."""
    from learn import _has_negation, _tokenize

    # '없어' is in the Korean substring list
    raw = "이건 별로 없어"
    toks = _tokenize(raw)
    assert _has_negation(toks, raw) is True

    # '싫어' too
    raw2 = "그거 싫어"
    assert _has_negation(_tokenize(raw2), raw2) is True


def test_has_negation_clean_positive_text():
    from learn import _has_negation, _tokenize

    raw = "I like this approach and it works well"
    toks = _tokenize(raw)
    assert _has_negation(toks, raw) is False


# ── _has_correction_signals ──────────────────────────────────────────────


def test_correction_signal_explicit_wrong():
    from learn import _has_correction_signals

    assert _has_correction_signals("Actually that's wrong, it should be Y.") is True


def test_correction_signal_no_actually():
    from learn import _has_correction_signals

    assert _has_correction_signals("No, actually the answer is X.") is True


def test_correction_signal_stale_data_phrase():
    from learn import _has_correction_signals

    assert _has_correction_signals("This is stale information from last week") is True


def test_correction_signal_neutral_transcript():
    from learn import _has_correction_signals

    assert _has_correction_signals("Let's ship this and move on to the next thing.") is False


# ── _heuristic_summary ───────────────────────────────────────────────────


def test_heuristic_summary_picks_last_user_message():
    from learn import _heuristic_summary

    transcript = (
        "Human: First, let's start with the database design.\n"
        "Assistant: OK, planning the schema now.\n"
        "Human: Actually, let's switch to GraphQL instead of REST."
    )
    out = _heuristic_summary(transcript)
    assert out is not None
    assert "GraphQL" in out or "switch" in out


def test_heuristic_summary_too_short_returns_none():
    from learn import _heuristic_summary

    assert _heuristic_summary("hi") is None
    assert _heuristic_summary("") is None


def test_heuristic_summary_truncates_to_max_length():
    from learn import SESSION_SUMMARY_MAX_LEN, _heuristic_summary

    msg = "x" * 500
    transcript = f"Human: {msg}"
    out = _heuristic_summary(transcript)
    assert out is not None
    assert len(out) <= SESSION_SUMMARY_MAX_LEN
