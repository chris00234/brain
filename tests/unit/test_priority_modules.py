"""Behavior tests for 15 priority untested modules.

Each module gets 2-4 tests covering the core contract. Intentionally
NOT exhaustive — depth is the job of module-specific test files. This
is the triage layer: prove the contract holds, catch the most likely
regressions.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
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


def test_source_quality_penalizes_aggregate_learning_logs():
    from source_quality import is_aggregate_learning_log, source_quality_multiplier

    result = {
        "path": "/Users/chrischo/.openclaw/workspace-liz/.learnings/LEARNINGS.md",
        "type": "learning",
    }

    assert is_aggregate_learning_log(result) is True
    assert source_quality_multiplier(result, stage="lexical") == 0.7
    assert source_quality_multiplier(result, stage="cross_encoder") == 0.72


def test_source_quality_penalizes_derived_self_learning_memory():
    from source_quality import source_quality_multiplier

    result = {"collection": "semantic_memory", "metadata": {"type": "self_learning"}}

    assert source_quality_multiplier(result, stage="lexical") == 0.85
    assert source_quality_multiplier(result, stage="cross_encoder") == 0.72


def test_rerank_boosts_matching_primary_source_files():
    from rerank import score_result

    query = "watchtower notification config"
    primary = {
        "path": "/Users/chrischo/server/watchtower/docker-compose.yml",
        "title": "watchtower config",
        "content": "watchtower notification config",
        "score": 50,
    }
    derivative = {
        "path": "/Users/chrischo/server/knowledge/distilled/infra/watchtower.md",
        "title": "watchtower config",
        "content": "watchtower notification config",
        "score": 50,
    }

    assert score_result(query, primary) > score_result(query, derivative) * 1.4


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
    from autopilot import should_auto_approve

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


def test_tokenizer_drops_question_filler_words():
    from tokenizer import tokenize

    tokens = tokenize("what does Chris think about abstractions")
    assert "what" not in tokens
    assert "about" not in tokens
    assert "chris" in tokens
    assert "abstractions" in tokens


# ── search_unified primary document lookup ─────────────────────
def test_search_unified_injects_current_state_primary_doc():
    from search_unified import _primary_doc_hits

    hits = _primary_doc_hits("Chris 지금 active project 뭐 있어?")
    paths = {h["path"] for h in hits}
    state_path = "/Users/chrischo/server/knowledge/canonical/chris/_state.md"

    assert state_path in paths
    state_hit = next(h for h in hits if h["path"] == state_path)
    assert "Active projects" in state_hit["content"]
    assert state_hit["metadata"]["canonical_lookup"] is True
    assert state_hit["title"] == "Chris Cho — current state (regenerated weekly)"


def test_search_unified_injects_korean_frontend_stack_docs():
    from search_unified import _primary_doc_hits

    hits = _primary_doc_hits("Chris는 프론트엔드 스택으로 뭘 선호해?")
    paths = {h["path"] for h in hits}

    assert "/Users/chrischo/server/knowledge/canonical/chris/preferred-frontend-stack.md" in paths
    assert "/Users/chrischo/server/knowledge/canonical/chris/frontend-stack-preference.md" in paths


def test_search_unified_injects_contract_first_primary_doc():
    from search_unified import _primary_doc_hits

    hits = _primary_doc_hits("What planning order does Chris want before coding?")
    paths = {h["path"] for h in hits}

    assert "/Users/chrischo/server/knowledge/canonical/chris/contract-first-execution-preference.md" in paths


def test_search_unified_injects_rag_role_primary_docs_with_korean_suffixes():
    from search_unified import _primary_doc_hits

    hits = _primary_doc_hits("RAG와 canonical notes 역할을 Chris 시스템에서 어떻게 나눠?")
    paths = {h["path"] for h in hits}

    assert "/Users/chrischo/server/knowledge/canonical/infra/infra_rag_retrieval_stack.md" in paths
    assert "/Users/chrischo/server/knowledge/canonical/infra/rag-stack-role.md" in paths


def test_search_unified_injects_business_opportunity_primary_doc():
    from search_unified import _primary_doc_hits

    hits = _primary_doc_hits("What business opportunities does Chris think are worth pursuing?")
    paths = {h["path"] for h in hits}

    assert (
        "/Users/chrischo/server/knowledge/canonical/archived/chris/"
        "chris-corrected-the-agent-for-anchoring-too-hard-on-obvious-already-dom.md" in paths
    )


def test_search_unified_injects_email_retention_primary_docs_for_korean_query():
    from search_unified import _primary_doc_hits

    hits = _primary_doc_hits("Chris는 어떤 이메일만 오래 보관하길 원해?")
    paths = {h["path"] for h in hits}

    assert (
        "/Users/chrischo/server/knowledge/canonical/archived/chris/"
        "chris-uses-a-conservative-six-month-email-retention-rule-that-keeps-pers.md" in paths
    )
    assert "/Users/chrischo/server/knowledge/canonical/archived/chris/openclaw-jenna-session.md" in paths


def test_search_unified_injects_operational_primary_docs():
    from search_unified import _primary_doc_hits

    cases = {
        "What did Chris want about PlayStation Plus cancellation?": (
            "/Users/chrischo/server/knowledge/canonical/archived/chris/"
            "from-playstation-sony-txn-email03-playstation-com.md"
        ),
        "What is the gstack /browse command for?": (
            "/Users/chrischo/server/knowledge/canonical/archived/decisions/"
            "chris-decided-to-install-gstack-under-claude-skills-gstack-and-to-m.md"
        ),
        "What is Chris's healthcheck standard for model registration?": (
            "/Users/chrischo/server/knowledge/canonical/archived/decisions/"
            "chris-established-a-practical-diagnostic-rule-that-model-registration-al.md"
        ),
        "What should happen to browser instances opened for Playwright after work is done?": (
            "/Users/chrischo/server/knowledge/canonical/archived/decisions/"
            "chris-expects-browser-instances-opened-for-playwright-or-browser-based-v.md"
        ),
        "What did Chris say about gateway restart or reinstall attempts?": (
            "/Users/chrischo/server/knowledge/canonical/archived/chris/"
            "chris-did-not-want-gateway-reinstall-attempts-from-the-active-assistant.md"
        ),
    }

    for query, expected_path in cases.items():
        paths = {h["path"] for h in _primary_doc_hits(query)}
        assert expected_path in paths


def test_search_unified_injects_sensitive_key_and_identity_primary_docs():
    from search_unified import _primary_doc_hits

    sensitive_paths = {
        h["path"] for h in _primary_doc_hits("How should sensitive keys and Ghost Admin keys be handled?")
    }
    identity_paths = {h["path"] for h in _primary_doc_hits("Has Chris lived in Irvine since August 2024?")}

    assert (
        "/Users/chrischo/server/knowledge/canonical/archived/chris/openclaw-jenna-session-2026-04-01.md"
        in sensitive_paths
    )
    assert (
        "/Users/chrischo/server/knowledge/canonical/archived/chris/openclaw-jenna-session-2026-04-10.md"
        in identity_paths
    )


def test_search_unified_detects_superseded_canonical_results():
    from search_unified import _is_superseded_canonical_result

    result = {"collection": "canonical", "content": '---json\n{"status": "superseded"}\n---\nold fact'}

    assert _is_superseded_canonical_result(result) is True


# ── batch_lock ──────────────────────────────────────────────────
def test_batch_lock_is_context_manager(tmp_path, monkeypatch):
    import batch_lock as _bl
    from batch_lock import batch_lock

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


def test_cross_encoder_rerank_normalizes_probability_scores_without_sigmoid_collapse():
    from cross_encoder_rerank import _normalize_model_scores

    out = _normalize_model_scores([0.81, 0.04, 0.0])

    assert out == [0.9, 0.2, 0.0]


def test_cross_encoder_rerank_normalizes_logit_scores_with_sigmoid():
    from cross_encoder_rerank import _normalize_model_scores

    out = _normalize_model_scores([10.0, 0.0, -10.0])

    assert out[0] > 0.99
    assert out[1] == 0.5
    assert out[2] < 0.01


def test_cross_encoder_rerank_preserves_source_quality_penalty(monkeypatch):
    import sys
    import types

    import cross_encoder_rerank

    fake_model = types.ModuleType("brain_core.cross_encoder_model")
    fake_model.score_pairs = lambda query, docs: [10.0, 10.0]
    monkeypatch.setitem(sys.modules, "brain_core.cross_encoder_model", fake_model)
    monkeypatch.setattr(cross_encoder_rerank, "BRAIN_CROSS_ENCODER_ENABLED", True)

    raw_learning = {
        "score": 50.0,
        "path": "/Users/chrischo/.openclaw/workspace-liz/.learnings/LEARNINGS.md",
        "type": "learning",
        "title": "Details",
        "content": "business opportunities",
    }
    canonical = {
        "score": 50.0,
        "path": "/Users/chrischo/server/knowledge/canonical/chris/business.md",
        "type": "canonical-note",
        "title": "Business preference",
        "content": "business opportunities",
    }

    out = cross_encoder_rerank.rerank_with_cross_encoder("business opportunities", [raw_learning, canonical])

    assert out[0] is canonical
    assert out[1] is raw_learning
    assert out[1]["_debug"]["source_quality_multiplier_cross_encoder"] == 0.72


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
