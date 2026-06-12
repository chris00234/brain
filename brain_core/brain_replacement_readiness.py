"""Brain replacement capability/readiness manifest.

This is intentionally deterministic and evidence-backed: each capability is
scored against code/storage surfaces that exist in the repo, not wishful design
claims. It gives evals a stable gate for Chris's goal that Brain eventually
substitutes for his own memory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CapabilityReadiness:
    key: str
    label: str
    status: str
    daily_use_impact: int
    implementation_risk: int
    score: float
    evidence: list[str]
    gap: str
    next_contract: str

    def to_dict(self) -> dict:
        return asdict(self)


CAPABILITIES: tuple[CapabilityReadiness, ...] = (
    CapabilityReadiness(
        key="prospective_memory",
        label="Prospective memory / time-context intentions",
        status="partial",
        daily_use_impact=5,
        implementation_risk=3,
        score=0.35,
        evidence=[
            "brain_core/task_queue.py: tasks/goals/dependencies store autonomous work",
            "brain_core/open_loops.py: deterministic due/follow-up surfacing from atoms",
        ],
        gap="No first-class reminders/calendar trigger store or agent-visible due queue yet.",
        next_contract="Add commitment/reminder table with due_at/context triggers and gateway-safe notification policy.",
    ),
    CapabilityReadiness(
        key="open_loop_tracking",
        label="Open-loop / commitment tracking",
        status="implemented_v1",
        daily_use_impact=5,
        implementation_risk=2,
        score=0.62,
        evidence=[
            "brain_core/open_loops.py: classify_open_loop_text, scan_atom_open_loops, scan_task_open_loops",
            "brain_core/routes/memory.py:/brain/doubt includes open_loops",
            "tests/unit/test_open_loops.py: durable-vs-chatter regression gate",
        ],
        gap="Detector is deterministic and read-only; no resolution workflow or human feedback calibration yet.",
        next_contract="Create resolve/snooze/assign workflow for open loops and collect false-positive/false-negative feedback.",
    ),
    CapabilityReadiness(
        key="temporal_autobiographical_memory",
        label="Temporal autobiographical memory",
        status="partial",
        daily_use_impact=5,
        implementation_risk=3,
        score=0.55,
        evidence=[
            "brain_core/atoms_store.py: valid_from, valid_until, created_at, updated_at, superseded_by",
            "brain_core/recall_governance/temporal_resolution.py: temporal conflict demotion",
            "brain_core/routes/governance.py:/brain/changes and /brain/evolution",
        ],
        gap="Good atom-level temporal fields, but no unified timeline projection of what changed/why/current-truth rationale.",
        next_contract="Build current-belief timeline API that joins supersession, provenance, and change reasons per topic/entity.",
    ),
    CapabilityReadiness(
        key="entity_property_model",
        label="Entity relationship/property model",
        status="partial",
        daily_use_impact=4,
        implementation_risk=3,
        score=0.58,
        evidence=[
            "brain_core/atoms_store.py: entities and atom_entity tables",
            "brain_core/entity_graph.py: Neo4j + SQLite fallback entity_relations",
            "tests/unit/test_ontology.py: relation aliases/constraints and one-hop expansion",
        ],
        gap="Aliases/properties/scopes are split across atoms, facts, Neo4j, and fallback SQLite; no single personal-ontology query API.",
        next_contract="Unify entity aliases/properties into one read model and add arbitrary personal fact query tests.",
    ),
    CapabilityReadiness(
        key="confidence_uncertainty_doubt",
        label="Confidence / uncertainty / doubt",
        status="partial",
        daily_use_impact=5,
        implementation_risk=2,
        score=0.68,
        evidence=[
            "brain_core/routes/memory.py:/brain/doubt low_confidence_atoms + pending_contradictions + open_loops",
            "brain_core/conflict_surfacer.py: candidate atom-pair conflicts",
            "brain_core/confidence_calibration.py: calibration metadata",
        ],
        gap="Doubt exists but still separated by surface; needs ranked actionability and feedback on whether doubt helped.",
        next_contract="Rank doubt items by harm/urgency and add usefulness/false-alarm feedback evals.",
    ),
    CapabilityReadiness(
        key="permission_privacy_model",
        label="Permission / privacy by agent, channel, context",
        status="partial",
        daily_use_impact=4,
        implementation_risk=4,
        score=0.42,
        evidence=[
            "brain_core/atoms_store.py: speaker_entity and scope fields",
            "hermes_integration/brain_memory_provider/__init__.py: prefetch policy gates profile context",
            "brain_core/routes/recall.py: actor-aware recall route plumbing",
        ],
        gap="No auditable policy matrix defining which memory classes may surface to each agent/channel.",
        next_contract="Add policy matrix + negative tests for private/sensitive memories across Telegram/CLI/Hermes profiles.",
    ),
    CapabilityReadiness(
        key="sensory_document_ingestion_quality",
        label="Sensory/document ingestion as evidence, not noise",
        status="partial",
        daily_use_impact=4,
        implementation_risk=3,
        score=0.5,
        evidence=[
            "brain_core/routes/knowledge.py:/brain/ingest manual document extraction",
            "brain_core/atoms_store.py: raw_events + provenance tables",
            "pyproject.toml: docling dependency for PDF/OCR ingestion",
        ],
        gap="Email/calendar/reminders/files/session logs are not all normalized into evidence-weighted atoms with source-specific noise gates.",
        next_contract="Add ingestion-quality eval slices per source type with evidence/noise/PII leakage scoring.",
    ),
    CapabilityReadiness(
        key="active_consolidation_forgetting",
        label="Active consolidation, forgetting, decay, supersession",
        status="partial",
        daily_use_impact=4,
        implementation_risk=3,
        score=0.64,
        evidence=[
            "brain_core/atoms_store.py: supersedes/superseded_by, next_review_at, decay_weight",
            "brain_core/routes/memory.py:/brain/consolidate and /memory/{id} delete",
            "brain_core/atom_deboost_cleanup.py and bridge cleanup tests",
        ],
        gap="Forgetting is possible but not governed by a single lifecycle policy with archival safety and user-visible reasons.",
        next_contract="Create lifecycle policy manifest and tests for promote/decay/archive/delete decisions with rollback evidence.",
    ),
    CapabilityReadiness(
        key="metacognitive_evals",
        label="Metacognitive evals: usefulness, harm, stale-context, top-k cleanliness",
        status="partial",
        daily_use_impact=5,
        implementation_risk=2,
        score=0.6,
        evidence=[
            "cli/retrieval_regression.py and cli/eval_set_stable.json: recall quality gate",
            "tests/unit/test_eval_set_stable_quality_slice.py: stable eval quality slice",
            "brain_core/eval_regression_diff.py: before/after regression diffing",
        ],
        gap="Readiness categories beyond recall are now enumerated but need ongoing CI/nightly gates and negative controls.",
        next_contract="Promote brain_replacement_readiness to CI/nightly with thresholds for open-loop, privacy, temporal, and doubt utility.",
    ),
    CapabilityReadiness(
        key="answer_interface",
        label="Compact actionable memory projections",
        status="partial",
        daily_use_impact=4,
        implementation_risk=2,
        score=0.52,
        evidence=[
            "brain_mcp_server.py: compact brain_recall MCP shape and brain_doubt tool",
            "brain_core/routes/recall.py: /recall/v2 meta_note and compact result fields",
            "AGENT_HARNESS.md: documented agent-facing response contracts",
        ],
        gap="Recall still often returns row-like evidence; no universal projection by intent (commitments, timeline, entities, doubts).",
        next_contract="Add projection builders that return task/timeline/entity/doubt-specific compact packs with source handles.",
    ),
)


def readiness_snapshot() -> dict:
    items = [cap.to_dict() for cap in CAPABILITIES]
    weighted = sum(cap.score * cap.daily_use_impact for cap in CAPABILITIES)
    total_weight = sum(cap.daily_use_impact for cap in CAPABILITIES)
    ranked_gaps = sorted(
        items,
        key=lambda item: (
            -item["daily_use_impact"],
            item["score"],
            item["implementation_risk"],
        ),
    )
    return {
        "status": "partial",
        "overall_score": round(weighted / total_weight, 3),
        "capabilities": items,
        "ranked_gaps": ranked_gaps,
        "implemented_now": ["open_loop_tracking", "brain_replacement_readiness_eval"],
        "next_3_contracts": [item["next_contract"] for item in ranked_gaps[:3]],
        "gate": {
            "required_capabilities": len(CAPABILITIES),
            "implemented_or_partial": sum(
                1 for cap in CAPABILITIES if cap.status in {"partial", "implemented_v1"}
            ),
            "hard_missing": [cap.key for cap in CAPABILITIES if cap.status == "missing"],
        },
    }
