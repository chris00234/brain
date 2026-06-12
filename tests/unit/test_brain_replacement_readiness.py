from __future__ import annotations

from brain_replacement_readiness import readiness_snapshot

REQUIRED_KEYS = {
    "prospective_memory",
    "open_loop_tracking",
    "temporal_autobiographical_memory",
    "entity_property_model",
    "confidence_uncertainty_doubt",
    "permission_privacy_model",
    "sensory_document_ingestion_quality",
    "active_consolidation_forgetting",
    "metacognitive_evals",
    "answer_interface",
}


def test_brain_replacement_readiness_covers_required_capabilities() -> None:
    snapshot = readiness_snapshot()
    keys = {item["key"] for item in snapshot["capabilities"]}

    assert keys >= REQUIRED_KEYS
    assert snapshot["gate"]["required_capabilities"] >= len(REQUIRED_KEYS)
    assert snapshot["overall_score"] > 0


def test_brain_replacement_readiness_records_shipped_open_loop_evidence() -> None:
    snapshot = readiness_snapshot()
    by_key = {item["key"]: item for item in snapshot["capabilities"]}
    open_loop = by_key["open_loop_tracking"]

    assert open_loop["status"] == "implemented_v1"
    assert any("brain_core/open_loops.py" in evidence for evidence in open_loop["evidence"])
    assert any("test_open_loops.py" in evidence for evidence in open_loop["evidence"])
    assert "open_loop_tracking" in snapshot["implemented_now"]


def test_brain_replacement_readiness_has_ranked_next_contracts() -> None:
    snapshot = readiness_snapshot()

    assert len(snapshot["ranked_gaps"]) >= 3
    assert len(snapshot["next_3_contracts"]) == 3
    assert all(contract for contract in snapshot["next_3_contracts"])
