"""Tests for brain_core/pipeline/ subpackage modules.

Most of these are cron job entry points with run() → dict contracts.
We verify import safety + a few key public helpers without live
ChromaDB / Ollama dependencies where possible.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core" / "pipeline"))


# ── proactive_linker ────────────────────────────────────────────
def test_proactive_linker_imports():
    import proactive_linker

    assert proactive_linker is not None


# ── gap_detector ────────────────────────────────────────────────
def test_gap_detector_imports():
    import gap_detector

    assert gap_detector is not None


# ── memory_nudge ────────────────────────────────────────────────
def test_memory_nudge_imports():
    import memory_nudge

    assert memory_nudge is not None


# ── training_pair_generator ─────────────────────────────────────
def test_training_pair_generator_imports():
    import training_pair_generator

    assert training_pair_generator is not None


# ── reembed_migrator ────────────────────────────────────────────
def test_reembed_migrator_imports():
    import reembed_migrator

    assert reembed_migrator is not None


# ── event_compressor ────────────────────────────────────────────
def test_event_compressor_imports():
    import event_compressor

    assert event_compressor is not None


# ── memory_consolidation ────────────────────────────────────────
def test_memory_consolidation_imports():
    import memory_consolidation

    assert memory_consolidation is not None


# ── memory_leak_detector ────────────────────────────────────────
def test_memory_leak_detector_imports():
    import memory_leak_detector

    assert memory_leak_detector is not None


# ── habituation_prune ───────────────────────────────────────────
def test_habituation_prune_imports():
    import habituation_prune

    assert habituation_prune is not None


# ── sleep_consolidate ───────────────────────────────────────────
def test_sleep_consolidate_imports():
    import sleep_consolidate

    assert sleep_consolidate is not None


# ── schema_learner ──────────────────────────────────────────────
def test_schema_learner_imports():
    import schema_learner

    assert schema_learner is not None


# ── skill_extractor ─────────────────────────────────────────────
def test_skill_extractor_imports():
    import skill_extractor

    assert skill_extractor is not None


def test_skill_extractor_digest_uses_direct_telegram(monkeypatch):
    import sys
    from types import SimpleNamespace

    import skill_extractor

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "telegram_alert",
        SimpleNamespace(
            send_chris_telegram=lambda body, source, severity: calls.append(
                {"body": body, "source": source, "severity": severity}
            )
            or True
        ),
    )

    assert skill_extractor.send_digest_to_telegram("weekly digest") is True
    assert calls == [
        {
            "body": "weekly digest",
            "source": "skill_extractor:weekly_digest",
            "severity": "info",
        }
    ]


# ── hnsw_tuner ──────────────────────────────────────────────────
def test_hnsw_tuner_imports():
    import hnsw_tuner

    assert hnsw_tuner is not None


# ── focus_aggregator ────────────────────────────────────────────
def test_focus_aggregator_imports():
    import focus_aggregator

    assert focus_aggregator is not None


# ── episode_binder ──────────────────────────────────────────────
def test_episode_binder_imports():
    import episode_binder

    assert episode_binder is not None


# ── llm_usage_purge ─────────────────────────────────────────────
def test_llm_usage_purge_imports():
    import llm_usage_purge

    assert llm_usage_purge is not None
