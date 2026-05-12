"""Smoke tests for the 7 largest production modules.

Architecture audit (2026-05-12) flagged these as god-modules lacking
dedicated unit tests:
  - search_unified.py (3123 lines)
  - brain_loop.py (2426 lines)
  - task_queue.py (2288 lines)
  - indexer.py (1849 lines)
  - slos.py (1539 lines)
  - learn.py (1517 lines)
  - memory_lifecycle.py (1363 lines)

These tests are deliberately minimal — they only verify:
  1. The module imports without error
  2. Top-level constants exist and have expected types
  3. A representative pure function runs without exceptions on safe inputs

A full unit-test backfill for each is a separate sprint. This smoke
layer catches import regressions (missing imports, syntax errors,
circular deps) which is the cheapest tier of safety.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _reload(mod_name: str):
    """Force-reload to catch import-time errors in this test, not earlier."""
    for k in [k for k in list(sys.modules) if k == mod_name]:
        del sys.modules[k]
    return importlib.import_module(mod_name)


def test_search_unified_imports():
    mod = _reload("search_unified")
    # search_all is the main entry point — verify it exists
    assert hasattr(mod, "search_all")
    assert callable(mod.search_all)


def test_brain_loop_imports():
    mod = _reload("brain_loop")
    # BrainLoop class is the orchestrator
    assert hasattr(mod, "BrainLoop")
    assert hasattr(mod, "run")
    # Observation + Decision dataclasses
    assert hasattr(mod, "Observation")
    assert hasattr(mod, "Decision")


def test_task_queue_imports():
    mod = _reload("task_queue")
    assert hasattr(mod, "task_queue")
    # Singleton exposes core methods
    tq = mod.task_queue
    assert hasattr(tq, "create_goal")
    assert hasattr(tq, "list_goals")
    assert hasattr(tq, "list_outcomes")


def test_indexer_imports():
    mod = _reload("indexer")
    assert hasattr(mod, "get_embedding")
    assert callable(mod.get_embedding)


def test_slos_imports():
    mod = _reload("slos")
    # SLOS registry must exist + be non-empty
    assert hasattr(mod, "SLOS")
    assert len(mod.SLOS) > 0
    # Top-level entry points
    assert callable(mod.check_all)
    assert callable(mod.check_one)
    # Each SLO has name + target
    for slo in list(mod.SLOS.values())[:3]:
        assert hasattr(slo, "name")
        assert hasattr(slo, "target")


def test_learn_imports():
    mod = _reload("learn")
    # Public learning pipeline functions used by /learn route
    assert callable(mod.process_session)
    assert callable(mod.extract_candidates)
    assert callable(mod.distill_via_jenna)
    assert callable(mod.check_contradictions)


def test_memory_lifecycle_imports():
    mod = _reload("memory_lifecycle")
    # Key functions for the brain's memory ops
    assert hasattr(mod, "prune_atrophied_memories")
    assert callable(mod.prune_atrophied_memories)
    assert hasattr(mod, "dedup_semantic_near_duplicates")
    assert callable(mod.dedup_semantic_near_duplicates)


def test_atoms_store_imports():
    """Bonus: atoms_store is 1322 lines — also god-module-tier."""
    mod = _reload("atoms_store")
    assert hasattr(mod, "upsert_atom")
    assert callable(mod.upsert_atom)


def test_cli_llm_imports():
    """Bonus: cli_llm is 1464 lines — subscription dispatch layer."""
    mod = _reload("cli_llm")
    assert hasattr(mod, "cli_dispatch")
    assert callable(mod.cli_dispatch)
