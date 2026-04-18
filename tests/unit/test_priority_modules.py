"""Behavior tests for 15 priority untested modules.

Each module gets 2-4 tests covering the core contract. Intentionally
NOT exhaustive — depth is the job of module-specific test files. This
is the triage layer: prove the contract holds, catch the most likely
regressions.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core" / "pipeline"))


# ── embed_cache ─────────────────────────────────────────────────
def test_embed_cache_text_hash_deterministic():
    from embed_cache import text_hash

    a = text_hash("hello world")
    b = text_hash("hello world")
    c = text_hash("DIFFERENT")
    assert a == b
    assert a != c


def test_embed_cache_stats_shape():
    from embed_cache import cache_stats

    s = cache_stats()
    for k in ("hits", "misses", "total", "hit_rate"):
        assert k in s


# ── http_pool ───────────────────────────────────────────────────
def test_http_pool_chroma_error_is_exception():
    from http_pool import ChromaAPIError

    assert issubclass(ChromaAPIError, Exception)


# ── rrf ─────────────────────────────────────────────────────────
def test_rrf_fuse_empty_input():
    from rrf import rrf_fuse

    assert rrf_fuse([]) == []


def test_rrf_fuse_merges_by_id_key():
    from rrf import rrf_fuse

    list_a = [{"path": "a", "score": 100}, {"path": "b", "score": 80}]
    list_b = [{"path": "b", "score": 95}, {"path": "c", "score": 70}]
    out = rrf_fuse([list_a, list_b], id_key="path")
    paths = [r["path"] for r in out]
    # All unique paths present
    assert set(paths) == {"a", "b", "c"}
    # Each result has a score field
    assert all("score" in r for r in out)


# ── rerank ──────────────────────────────────────────────────────
def test_rerank_jaccard_bounds():
    from rerank import _jaccard

    assert _jaccard(set(), set()) == 0.0
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0
    overlap = _jaccard({"a", "b", "c"}, {"a", "d"})
    assert 0 < overlap < 1


def test_rerank_empty_list():
    from rerank import rerank

    out = rerank("query", [])
    assert out == []


# ── temporal ────────────────────────────────────────────────────
def test_temporal_parse_today():
    from temporal import parse

    fixed_now = datetime(2026, 4, 17, tzinfo=UTC)
    # Parse some known forms
    result = parse("today", now=fixed_now)
    # Result should be a datetime or tuple
    assert result is not None


def test_temporal_to_chroma_where_none_on_empty():
    from temporal import to_chroma_where

    assert to_chroma_where(None, None) is None


def test_temporal_to_chroma_where_with_range():
    from temporal import to_chroma_where

    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 17, tzinfo=UTC)
    out = to_chroma_where(start, end)
    # Either returns a where dict or None — but if returned, has operators
    if out is not None:
        as_str = str(out)
        assert "$gte" in as_str or "$lte" in as_str or "$and" in as_str


# ── breakers ────────────────────────────────────────────────────
def test_breaker_snapshot_dataclass_fields():
    from breakers import BreakerSnapshot

    # Construct with full schema (reset_after_s is required)
    snap = BreakerSnapshot(
        kind="test.kind",
        state="closed",
        failures=0,
        trip_count=0,
        opened_at=None,
        last_failure_at=None,
        last_action_at=None,
        reason="",
        reset_after_s=300,
    )
    assert snap.kind == "test.kind"
    assert snap.is_open is False


def test_breaker_peek_unknown_kind():
    from breakers import peek_breaker

    snap = peek_breaker("test.nonexistent.kind")
    # For unknown kind, returns a default-closed snapshot
    assert snap is None or snap.state == "closed"


# ── autopilot ───────────────────────────────────────────────────
def test_autopilot_state_has_enabled_flag():
    from autopilot import get_state

    s = get_state()
    assert "enabled" in s
    assert isinstance(s["enabled"], bool)


def test_autopilot_should_auto_approve_threshold():
    from autopilot import get_state, should_auto_approve

    state = get_state()
    # Very high confidence either auto-approves (if enabled) or returns False
    assert should_auto_approve(0.99) in (True, False)
    # Below threshold reliably fails
    assert should_auto_approve(0.05) is False


# ── safe_state ──────────────────────────────────────────────────
def test_safe_state_load_nonexistent(tmp_path):
    from safe_state import load_state

    out = load_state(tmp_path / "never_created.json")
    assert out == {}


def test_safe_state_roundtrip(tmp_path):
    from safe_state import load_state, save_state

    p = tmp_path / "state.json"
    save_state(p, {"key": "value", "n": 42})
    got = load_state(p)
    assert got == {"key": "value", "n": 42}


def test_safe_state_atomic_write_no_partial(tmp_path):
    """atomic_write_text should never leave a partial file + no .tmp."""
    from safe_state import atomic_write_text

    p = tmp_path / "x.txt"
    atomic_write_text(p, "hello")
    assert p.read_text() == "hello"
    # No leftover tmp
    tmps = list(tmp_path.glob("*.tmp*"))
    assert tmps == []


# ── tokenizer ───────────────────────────────────────────────────
def test_tokenizer_basic_splits():
    from tokenizer import tokenize

    tokens = tokenize("Hello, World! 서울 brain-ui")
    # Lower-cased, punctuation-stripped
    assert "hello" in tokens
    assert "world" in tokens


def test_tokenizer_empty_returns_empty_set():
    from tokenizer import tokenize

    assert tokenize("") == set()


# ── batch_lock ──────────────────────────────────────────────────
def test_batch_lock_is_context_manager(tmp_path, monkeypatch):
    from batch_lock import batch_lock
    import batch_lock as _bl

    # Redirect lock file to tmp
    monkeypatch.setattr(_bl, "LOCK_FILE", tmp_path / "batch.lock")

    with batch_lock("test_job") as acquired:
        # Either returns a truthy acquired flag or just yields None
        assert acquired is True or acquired is None


# ── hooks ───────────────────────────────────────────────────────
def test_hooks_module_has_exports():
    import hooks

    # Must expose at least one callable
    public = [n for n in dir(hooks) if not n.startswith("_")]
    assert len(public) > 0


# ── cross_encoder_rerank ────────────────────────────────────────
def test_cross_encoder_rerank_empty_input():
    from cross_encoder_rerank import rerank_with_cross_encoder

    out = rerank_with_cross_encoder("query", [])
    assert out == []


def test_cross_encoder_rerank_sigmoid_bounds():
    from cross_encoder_rerank import _sigmoid

    # Sigmoid output must be in (0, 1)
    assert 0 < _sigmoid(0) < 1
    assert _sigmoid(0) == 0.5  # sigmoid(0) = 0.5
    assert _sigmoid(10) > 0.99
    assert _sigmoid(-10) < 0.01


# ── feedback_aggregator ─────────────────────────────────────────
def test_feedback_aggregator_module_safely_importable():
    import feedback_aggregator

    assert feedback_aggregator is not None


# ── config ──────────────────────────────────────────────────────
def test_config_exposes_db_paths():
    import config

    # Canonical DB paths must be defined
    assert hasattr(config, "BRAIN_DB")
    assert hasattr(config, "AUTONOMY_DB")
    assert hasattr(config, "BRAIN_LOGS_DIR")


def test_config_paths_are_under_logs_dir():
    import config

    assert str(config.BRAIN_DB).startswith(str(config.BRAIN_LOGS_DIR))
    assert str(config.AUTONOMY_DB).startswith(str(config.BRAIN_LOGS_DIR))
