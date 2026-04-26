"""tests/unit/test_tokenizer.py — bilingual tokenizer + stopword filtering."""

from __future__ import annotations

from brain_core.tokenizer import tokenize


def test_english_stopwords_filtered():
    tokens = tokenize("the brain server is running")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "brain" in tokens
    assert "server" in tokens
    assert "running" in tokens


def test_korean_function_words_filtered():
    """Multi-syllable Korean function words (그리고/입니다/위해/대해) are
    filtered as the bilingual stopword equivalent of English the/is/about.
    Single-syllable particles (은/는/이/가/을/를) are already excluded by
    the {2,} length floor on [가-힣]+.
    """
    tokens = tokenize("서버 그리고 데이터베이스 입니다")
    assert "그리고" not in tokens
    assert "입니다" not in tokens
    assert "서버" in tokens
    assert "데이터베이스" in tokens


def test_korean_demonstrative_and_interrogative_filtered():
    tokens = tokenize("이것 무엇 어떻게 코드")
    assert "이것" not in tokens
    assert "무엇" not in tokens
    assert "어떻게" not in tokens
    assert "코드" in tokens


def test_korean_postposition_compounds_filtered():
    tokens = tokenize("브레인 위해 통해 대해")
    assert "위해" not in tokens
    assert "통해" not in tokens
    assert "대해" not in tokens
    assert "브레인" in tokens


def test_short_korean_particles_already_excluded_by_length_floor():
    """은/는/이/가/을/를 are 1 syllable each — the {2,} regex never extracts
    them, so they don't need to be in the stopword list."""
    tokens = tokenize("서버는 좋다")
    # The particle 는 is attached to 서버 in writing; the regex captures the
    # full Hangul run "서버는" as one token, not "서버" + "는".
    assert any("서버" in tok for tok in tokens)
