"""Contract tests for extracted recall governance quality helpers."""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_routes_recall_reexports_quality_helper_seams():
    import routes.recall as recall_route
    from recall_governance import quality

    assert recall_route._normalize_recall_signature is quality.normalize_recall_signature
    assert recall_route._near_duplicate_key is quality.near_duplicate_key
    assert recall_route._is_near_duplicate_signature is quality.is_near_duplicate_signature
    assert recall_route._quality_rank_tuple is quality.quality_rank_tuple
    assert recall_route._is_conversation_transcript_row is quality.is_conversation_transcript_row


def test_normalize_recall_signature_strips_dates_numbers_urls_and_stopwords():
    from routes.recall import _normalize_recall_signature

    signature = _normalize_recall_signature(
        "Chris wants the Brain eval score improvement from https://example.com on 2026-06-09 at 100%"
    )

    assert signature == "brain eval score improvement"


def test_near_duplicate_key_collapses_brain_eval_score_preference():
    from routes.recall import _near_duplicate_key

    result = {"content": "Chris prefers Brain eval score improvements to reduce recall noise."}

    assert _near_duplicate_key(result) == "brain-eval-score-improvement-preference"


def test_is_near_duplicate_signature_uses_overlap_for_long_signatures():
    from routes.recall import _is_near_duplicate_signature

    kept = ["brain recall quality current useful context noise suppression"]

    assert _is_near_duplicate_signature("brain recall quality useful context noise", kept)
    assert not _is_near_duplicate_signature("calendar tooling preference apple events", kept)


def test_quality_rank_tuple_prefers_accepted_canonical_truth():
    from routes.recall import _quality_rank_tuple

    canonical = {
        "collection": "canonical",
        "score": 10,
        "metadata": {"review_state": "accepted", "category": "preference"},
    }
    raw = {"collection": "semantic_memory", "score": 99, "metadata": {"category": "other"}}

    assert _quality_rank_tuple(canonical) > _quality_rank_tuple(raw)


def test_quality_rank_tuple_truth_categories_match_source_authority():
    from recall_governance import quality, source_authority

    assert quality._TRUTH_CATEGORIES is source_authority._TRUTH_CATEGORIES

    from routes.recall import _quality_rank_tuple

    correction = {"collection": "semantic_memory", "score": 1, "metadata": {"category": "correction"}}
    entity = {"collection": "semantic_memory", "score": 1, "metadata": {"category": "entity"}}

    assert _quality_rank_tuple(correction)[0] == 2.0
    assert _quality_rank_tuple(entity)[0] == 0.0


def test_conversation_transcript_row_detects_role_prefixed_capture():
    from routes.recall import _is_conversation_transcript_row

    assert _is_conversation_transcript_row({"content": "User: what is my email?\nAssistant: ..."})
    assert _is_conversation_transcript_row({"title": "사용자: 질문\n어시스턴트: 답"})
    assert not _is_conversation_transcript_row({"content": "Chris prefers concise responses."})
