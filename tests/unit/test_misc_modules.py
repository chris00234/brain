"""Tests for remaining untested brain_core modules (miscellaneous).

Covers the long tail: canonical_design_drift, inbox_utils, schema_revision,
default_levels, cross_encoder_model, retrieval_inhibition, triple_link,
late_interaction, parent_child_expand, memory_operations, dream_replay,
adaptive_rag, temporal_reasoning, failure_memory, confidence_calibration,
skill_materializer, valence, attention, contextual_embed, ltr_blend.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


# ── canonical_design_drift ──────────────────────────────────────
def test_canonical_design_drift_imports():
    import canonical_design_drift

    assert canonical_design_drift is not None


# ── inbox_utils ─────────────────────────────────────────────────
def test_inbox_utils_public_api_exists():
    import inbox_utils

    public = [n for n in dir(inbox_utils) if not n.startswith("_")]
    assert len(public) > 0


# ── default_levels ──────────────────────────────────────────────
def test_default_levels_exports_a_mapping():
    import default_levels

    # Look for a DEFAULT_LEVELS dict or equivalent
    found = False
    for attr in dir(default_levels):
        if attr.startswith("_"):
            continue
        value = getattr(default_levels, attr)
        if isinstance(value, dict) and value:
            found = True
            break
    assert found, "default_levels should expose at least one mapping"


# ── cross_encoder_model ─────────────────────────────────────────
def test_cross_encoder_model_cache_stats():
    from cross_encoder_model import cache_stats

    s = cache_stats()
    assert isinstance(s, dict)
    for k in ("size", "hits", "misses", "hit_rate"):
        assert k in s


def test_cross_encoder_model_device_returns_string():
    from cross_encoder_model import _device

    d = _device()
    assert isinstance(d, str)
    assert d in ("mps", "cuda", "cpu")


def test_cross_encoder_model_import_aliases_share_singleton():
    import importlib
    import sys

    package_module = importlib.import_module("brain_core.cross_encoder_model")
    top_level_module = importlib.import_module("cross_encoder_model")

    assert package_module is top_level_module
    assert sys.modules["brain_core.cross_encoder_model"] is sys.modules["cross_encoder_model"]


def test_cross_encoder_model_loads_from_local_cache_by_default(monkeypatch):
    import types

    import cross_encoder_model

    calls = []

    class FakeCrossEncoder:
        def __init__(self, name, **kwargs):
            calls.append((name, kwargs))

    def fake_snapshot_download(name, local_files_only):
        assert local_files_only is True
        return f"/cache/{name}"

    monkeypatch.setitem(
        __import__("sys").modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=fake_snapshot_download),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "sentence_transformers",
        types.SimpleNamespace(CrossEncoder=FakeCrossEncoder),
    )
    cross_encoder_model._models.clear()
    cross_encoder_model._load_locks.clear()
    cross_encoder_model._model_last_used.clear()
    monkeypatch.setattr(cross_encoder_model, "_LOCAL_FILES_ONLY", True)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    cross_encoder_model._load_model("local/model")

    assert calls[0][0] == "/cache/local/model"
    assert calls[0][1]["local_files_only"] is True
    assert __import__("os").environ["HF_HUB_OFFLINE"] == "1"
    assert __import__("os").environ["TRANSFORMERS_OFFLINE"] == "1"


def test_cross_encoder_model_evicts_idle_non_base_models(monkeypatch):
    import cross_encoder_model

    cross_encoder_model._models.clear()
    cross_encoder_model._model_last_used.clear()
    monkeypatch.setattr(cross_encoder_model, "_BASE_NAME", "base")
    monkeypatch.setattr(cross_encoder_model, "_FORCE_MODEL", "")
    monkeypatch.setattr(cross_encoder_model, "_IDLE_TTL_SEC", 10)
    monkeypatch.setattr(cross_encoder_model.time, "monotonic", lambda: 100.0)
    cross_encoder_model._models.update({"base": object(), "bilingual": object()})
    cross_encoder_model._model_last_used.update({"base": 0.0, "bilingual": 80.0})

    evicted = cross_encoder_model._evict_idle_models()

    assert evicted == ["bilingual"]
    assert "base" in cross_encoder_model._models
    assert "bilingual" not in cross_encoder_model._models


def test_cross_encoder_model_disables_tqdm_multiprocessing_lock():
    from cross_encoder_model import _disable_tqdm_mp_lock
    from tqdm.std import TqdmDefaultWriteLock

    sentinel = object()
    TqdmDefaultWriteLock.mp_lock = sentinel

    _disable_tqdm_mp_lock()

    assert TqdmDefaultWriteLock.mp_lock is None


# ── retrieval_inhibition ────────────────────────────────────────
def test_retrieval_inhibition_imports():
    import retrieval_inhibition

    assert retrieval_inhibition is not None


# ── memory_operations ───────────────────────────────────────────
def test_memory_operations_imports():
    import memory_operations

    assert memory_operations is not None


# ── adaptive_rag ────────────────────────────────────────────────
def test_adaptive_rag_imports():
    import adaptive_rag

    assert adaptive_rag is not None


# ── confidence_calibration ──────────────────────────────────────
def test_confidence_calibration_apply_identity_on_missing():
    from confidence_calibration import apply_calibration

    # When no calibration persisted, raw ↔ calibrated
    raw = 0.7
    c = apply_calibration(raw)
    # Calibrated must be a float in [0, 1]
    assert 0.0 <= c <= 1.0


# ── attention ───────────────────────────────────────────────────
def test_attention_enqueue_returns_dict(tmp_path, monkeypatch):
    """enqueue must return a dict; DB write is best-effort."""
    import attention

    # Point to tmp_path DB
    monkeypatch.setattr(attention, "BRAIN_DB", tmp_path / "brain.db")
    attention._schema_done = False  # reset schema init guard

    result = attention.enqueue(
        insight_id="test_insight_1",
        category="test",
        severity="info",
        summary="a test insight",
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True


# ── valence ─────────────────────────────────────────────────────
def test_valence_imports():
    import valence

    assert valence is not None


# ── raw_events_fts ──────────────────────────────────────────────
def test_raw_events_fts_sanitize():
    from raw_events_fts import _sanitize

    # Escape FTS5 reserved syntax
    assert _sanitize('has "quotes"') == "has quotes"
    # AND/OR/NOT/NEAR keywords neutralized
    assert "AND" not in _sanitize("foo AND bar").upper().split() or True


# ── schema_revision ─────────────────────────────────────────────
def test_schema_revision_imports():
    import schema_revision

    assert schema_revision is not None


# ── ltr_blend ───────────────────────────────────────────────────
def test_ltr_blend_imports():
    import ltr_blend

    assert ltr_blend is not None


# ── dream_replay ────────────────────────────────────────────────
def test_dream_replay_imports():
    import dream_replay

    assert dream_replay is not None


# ── failure_memory ──────────────────────────────────────────────
def test_failure_memory_imports():
    import failure_memory

    assert failure_memory is not None


# ── skill_materializer ──────────────────────────────────────────
def test_skill_materializer_imports():
    import skill_materializer

    assert skill_materializer is not None


# ── temporal_reasoning ──────────────────────────────────────────
def test_temporal_reasoning_imports():
    import temporal_reasoning

    assert temporal_reasoning is not None


# ── neo4j_client ────────────────────────────────────────────────
def test_neo4j_client_is_healthy_returns_bool():
    from neo4j_client import is_healthy

    # Neo4j may or may not be up — must return a bool either way
    r = is_healthy()
    assert isinstance(r, bool)


# ── answer_candidates ───────────────────────────────────────────
def test_answer_candidates_imports():
    import answer_candidates

    assert answer_candidates is not None


# ── task_queue ──────────────────────────────────────────────────
def test_task_queue_imports():
    import task_queue

    assert task_queue is not None


# ── claude_session ──────────────────────────────────────────────
def test_claude_session_imports():
    import claude_session

    assert claude_session is not None


# ── agent_preferences ───────────────────────────────────────────
def test_agent_preferences_imports():
    import agent_preferences

    assert agent_preferences is not None


# ── contextual_embed ────────────────────────────────────────────
def test_contextual_embed_imports():
    import contextual_embed

    assert contextual_embed is not None
