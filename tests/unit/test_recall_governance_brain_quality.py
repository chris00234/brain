from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_routes_recall_reexports_brain_quality_constants():
    import routes.recall as recall_route
    from recall_governance import brain_quality

    assert recall_route._BRAIN_QUALITY_SUBSYSTEM_TOKENS is brain_quality.BRAIN_QUALITY_SUBSYSTEM_TOKENS
    assert recall_route._BRAIN_QUALITY_BROAD_TOKENS is brain_quality.BRAIN_QUALITY_BROAD_TOKENS
    assert recall_route._BRAIN_QUALITY_GENERIC_MARKERS is brain_quality.BRAIN_QUALITY_GENERIC_MARKERS


def test_brain_quality_query_classifier_detects_eval_quality_prompts():
    from recall_governance import brain_quality

    assert brain_quality.is_brain_quality_query_text("brain recall quality eval score")
    assert brain_quality.is_brain_quality_query_text("brain_decide")
    assert brain_quality.is_brain_quality_query_text("브레인 리콜 품질 평가")
    assert not brain_quality.is_brain_quality_query_text("brain server port")
    assert not brain_quality.is_brain_quality_query_text("generic eval rubric")


def test_routes_wrapper_preserves_brain_quality_query_contract():
    from routes.recall import _is_brain_quality_query

    assert _is_brain_quality_query("brain prefetch noisy context")
    assert _is_brain_quality_query("brain_decide")
    assert _is_brain_quality_query("브레인 리콜 노이즈")
    assert not _is_brain_quality_query("what port does brain use")


def test_stale_generic_quality_result_drops_system_dependency_noise():
    from routes.recall import _is_stale_generic_quality_result

    result = {
        "title": "Brain system dependency",
        "content": "Knowledge gap bridge: Brain system dependency. Brain depends on FastAPI brain-server.",
    }

    assert _is_stale_generic_quality_result(result, "brain recall quality score")
    assert not _is_stale_generic_quality_result(result, "what port does brain use")


def test_stale_generic_quality_result_keeps_marker_when_query_asks_for_it():
    from routes.recall import _is_stale_generic_quality_result

    result = {
        "title": "Brain dependency",
        "content": "Brain depends on FastAPI brain-server.",
    }

    assert not _is_stale_generic_quality_result(
        result,
        "brain recall quality: brain depends on fastapi brain-server",
    )


def test_stale_generic_quality_result_respects_summary_intent():
    from routes.recall import _is_stale_generic_quality_result

    result = {
        "title": "Session summary",
        "content": "Weekly brain recall quality notes.",
    }

    assert _is_stale_generic_quality_result(result, "brain recall quality noise")
    assert not _is_stale_generic_quality_result(result, "summarize brain recall quality noise")
