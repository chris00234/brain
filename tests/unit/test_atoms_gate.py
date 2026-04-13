"""Unit tests for brain_core.atoms_gate - 30-word atom discipline."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def atoms_gate(monkeypatch):
    if "atoms_gate" in sys.modules:
        del sys.modules["atoms_gate"]
    import atoms_gate

    yield atoms_gate


def test_count_words_english(atoms_gate):
    assert atoms_gate.count_words("hello world") == 2
    assert atoms_gate.count_words("Chris uses uv for Python builds") == 6


def test_count_words_korean(atoms_gate):
    # Each ~2-syllable Hangul block counts as 1 word
    n = atoms_gate.count_words("크리스는 파이썬을 좋아한다")
    assert n >= 3


def test_count_words_empty(atoms_gate):
    assert atoms_gate.count_words("") == 0
    assert atoms_gate.count_words(None) == 0  # type: ignore[arg-type]


def test_classify_ok_short(atoms_gate):
    assert atoms_gate.classify("Chris uses uv.") == "ok"


def test_classify_warned_at_31_words(atoms_gate):
    text = " ".join(["word"] * 35)
    assert atoms_gate.classify(text) == "warned"


def test_classify_needs_redistill_above_50(atoms_gate):
    text = " ".join(["word"] * 70)
    assert atoms_gate.classify(text) == "needs_redistill"


def test_quality_for_status(atoms_gate):
    assert atoms_gate.quality_for("ok") == 1.0
    assert atoms_gate.quality_for("warned") == 0.7
    assert atoms_gate.quality_for("needs_redistill") == 0.3


def test_enforce_short_passthrough(atoms_gate):
    text, status, q = atoms_gate.enforce("Chris likes FastAPI.", allow_redistill=False)
    assert status == "ok"
    assert q == 1.0
    assert text == "Chris likes FastAPI."


def test_enforce_warned_passthrough(atoms_gate):
    long_text = " ".join(["word"] * 35)
    text, status, q = atoms_gate.enforce(long_text, allow_redistill=False)
    assert status == "warned"
    assert q == 0.7


def test_enforce_long_no_redistill_stores_long(atoms_gate):
    long_text = " ".join(["word"] * 70)
    text, status, q = atoms_gate.enforce(long_text, allow_redistill=False)
    assert status == "stored_long"
    assert q == 0.3
    assert text == long_text


def test_enforce_long_redistill_failure_falls_back(atoms_gate, monkeypatch):
    long_text = " ".join(["word"] * 70)
    monkeypatch.setattr(atoms_gate, "redistill_via_jenna", lambda _t: None)
    text, status, q = atoms_gate.enforce(long_text)
    assert status == "stored_long"
    assert q == 0.3


def test_enforce_long_redistill_success(atoms_gate, monkeypatch):
    long_text = " ".join(["word"] * 70)
    monkeypatch.setattr(atoms_gate, "redistill_via_jenna", lambda _t: "Compressed fact.")
    text, status, q = atoms_gate.enforce(long_text)
    assert status == "redistilled"
    assert q == 1.0
    assert text == "Compressed fact."
