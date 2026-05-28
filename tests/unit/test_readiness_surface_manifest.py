from __future__ import annotations

from brain_core import readiness_surface_manifest as manifest


def test_readiness_surface_manifest_covers_world_level_observability_surfaces():
    snapshot = manifest.manifest_snapshot()

    assert snapshot["version"] == "readiness-surface-manifest-v1"
    assert snapshot["surface_count"] >= 7
    ids = {surface["id"] for surface in snapshot["surfaces"]}
    assert {
        "ops_readiness",
        "slo_incidents",
        "retrieval_eval_gates",
        "source_governance",
        "skill_promotion",
        "failure_lesson_outcome",
        "hermes_gateway",
    } <= ids
    assert manifest.readiness_fields_for("retrieval_eval_gates") == (
        "crag_regression",
        "crag_correction_regression",
        "ragas_eval",
        "adversarial_eval",
        "holdout_eval",
    )
    assert manifest.readiness_fields_for("missing") == ()
