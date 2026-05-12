"""Manifest of readiness surfaces that must stay API/UI visible.

This is the contract layer for world-level readiness observability. Audits can
use it instead of re-encoding expected readiness fields by hand.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

READINESS_SURFACE_MANIFEST_VERSION = "readiness-surface-manifest-v1"


@dataclass(frozen=True)
class ReadinessSurface:
    id: str
    label: str
    readiness_fields: tuple[str, ...]
    rationale: str


READINESS_SURFACES: tuple[ReadinessSurface, ...] = (
    ReadinessSurface(
        id="ops_readiness",
        label="ops readiness aggregate",
        readiness_fields=("release_readiness",),
        rationale="Top-level readiness and release hygiene must be visible.",
    ),
    ReadinessSurface(
        id="slo_incidents",
        label="SLO roster and remediation ledger",
        readiness_fields=("slo_incident_ledger", "slo_escalation_ledger"),
        rationale="Operations failures need dashboard evidence and remediation traceability.",
    ),
    ReadinessSurface(
        id="retrieval_eval_gates",
        label="retrieval and answer evaluation gates",
        readiness_fields=(
            "crag_regression",
            "crag_correction_regression",
            "ragas_eval",
            "adversarial_eval",
            "holdout_eval",
        ),
        rationale="Retrieval quality cannot be world-level if gates are hidden or stale.",
    ),
    ReadinessSurface(
        id="source_governance",
        label="high-value source governance",
        readiness_fields=("source_governance",),
        rationale="High-value ingestion and pollution controls must be visible.",
    ),
    ReadinessSurface(
        id="skill_promotion",
        label="skill promotion and outcome loop",
        readiness_fields=("skill_promotion",),
        rationale="Auto-skill promotion needs provenance, rollback, and outcome visibility.",
    ),
    ReadinessSurface(
        id="failure_lesson_outcome",
        label="failure lesson outcome loop",
        readiness_fields=("failure_lesson_outcome",),
        rationale="Reflexion lessons need post-use outcome evidence, not write-only storage.",
    ),
    ReadinessSurface(
        id="openclaw_gateway",
        label="OpenClaw gateway health",
        readiness_fields=("openclaw_gateway",),
        rationale="Autonomous agent work depends on gateway health being explicit.",
    ),
    ReadinessSurface(
        id="autonomous_work",
        label="autonomous/background work visibility",
        readiness_fields=("autonomous_work",),
        rationale="No-consent background work needs visible action, status, and evidence.",
    ),
)


def readiness_fields_for(surface_id: str) -> tuple[str, ...]:
    for surface in READINESS_SURFACES:
        if surface.id == surface_id:
            return surface.readiness_fields
    return ()


def manifest_snapshot() -> dict[str, Any]:
    return {
        "version": READINESS_SURFACE_MANIFEST_VERSION,
        "surface_count": len(READINESS_SURFACES),
        "surfaces": [asdict(surface) for surface in READINESS_SURFACES],
    }
