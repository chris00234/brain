"""Ontology registry for Brain semantic contracts.

This module is intentionally lightweight: it defines the shared vocabulary and
validation helpers used by graph/canonical/fact/audit code without introducing a
new source of truth or a new runtime store. Canonical notes + atoms remain the
truth layer; Neo4j/Qdrant/RDF exports are projections.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Status = Literal["known", "alias", "deprecated", "unknown", "blank"]
Severity = Literal["info", "warning", "error"]

_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# Keep these small and explicit. They are the contract; downstream code should
# import this module rather than re-declaring vocabularies.
ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "agent",
        "atom",
        "concept",
        "decision",
        "event",
        "fact",
        "goal",
        "incident",
        "memory",
        "note",
        "person",
        "policy",
        "preference",
        "procedure",
        "project",
        "repo",
        "service",
        "task",
        "tool",
        "workflow",
    }
)

MEMORY_KINDS: frozenset[str] = frozenset(
    {
        "correction",
        "decision",
        "entity",
        "fact",
        "incident",
        "other",
        "preference",
        "procedure",
    }
)

SCOPES: frozenset[str] = frozenset({"global", "project", "session", "time_bounded"})

FACT_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "confidence",
        "location",
        "model",
        "owner",
        "path",
        "port",
        "preference",
        "role",
        "status",
        "test_fixture",
        "timezone",
        "url",
        "valid_from",
        "valid_to",
        "version",
    }
)

FACT_ATTRIBUTE_ALIASES: dict[str, str] = {
    "endpoint": "url",
    "file": "path",
    "owner_name": "owner",
    "service_port": "port",
    "tz": "timezone",
}

DEPRECATED_FACT_ATTRIBUTES: dict[str, str] = {
    # Present in the current facts.db from historical tests. Keep it classified
    # so audits do not confuse test fixture residue with relation drift.
    "test_attr": "test_fixture",
    "test_attr_r11": "test_fixture",
}

RELATION_TYPES: frozenset[str] = frozenset(
    {
        "affects",
        "aligns_with",
        "assigned_to",
        "co_mention",
        "co_retrieved",
        "complements",
        "configures",
        "contradicts",
        "created",
        "describes",
        "depends_on",
        "documents",
        "has_agent",
        "has_event",
        "implements",
        "informs",
        "managed_by",
        "manages",
        "mentions",
        "owned_by",
        "owns",
        "part_of",
        "prefers",
        "proposes",
        "proxies",
        "related_to",
        "runs_on",
        "supports",
        "superseded_by",
        "supersedes",
        "uses",
    }
)

# Direction matters. Do not map directional relations to their inverse.
RELATION_ALIASES: dict[str, str] = {
    "mitigated_by": "supports",
    "configured_by": "configures",
    "created_by": "created",
    "depends": "depends_on",
    "governed_by": "informs",
    "has_owner": "owned_by",
    "owner": "owned_by",
    "owner_of": "owns",
    "relates_to": "related_to",
    "requires": "depends_on",
    "source": "supports",
}

DEPRECATED_RELATIONS: dict[str, str] = {
    # Migration imported original ontology relations as this fallback and lost
    # the original predicate. Keep it readable, but require future writes to use
    # a specific relation.
    "ontology_related": "related_to",
}

ENTITY_TYPE_ALIASES: dict[str, str] = {
    "app": "service",
    "application": "service",
    "assistant": "agent",
    "canonical": "note",
    "canonical_note": "note",
    "codebase": "repo",
    "repository": "repo",
    "rule": "policy",
    "user": "person",
}

MEMORY_KIND_ALIASES: dict[str, str] = {
    "workflow": "procedure",
}

SCOPE_ALIASES: dict[str, str] = {
    "time-bounded": "time_bounded",
    "timebounded": "time_bounded",
}

# Only enforce constraints for relations where invalid direction is dangerous.
# Empty source/target sets mean "allow any known entity type".
RELATION_CONSTRAINTS: dict[str, dict[str, frozenset[str]]] = {
    "has_agent": {"source": frozenset({"person"}), "target": frozenset({"agent"})},
    "owned_by": {
        "source": frozenset({"project", "repo", "service", "tool", "workflow"}),
        "target": frozenset({"person", "agent"}),
    },
    "owns": {
        "source": frozenset({"person", "agent"}),
        "target": frozenset({"project", "repo", "service", "tool", "workflow"}),
    },
    "mentions": {"source": frozenset({"atom", "memory", "note", "fact"}), "target": frozenset()},
    "supersedes": {"source": frozenset({"atom", "memory", "note", "decision"}), "target": frozenset()},
}

EXPANSION_RELATION_CONSTRAINTS: dict[str, dict[str, frozenset[str]]] = {
    **RELATION_CONSTRAINTS,
    # Retrieval expansion is stricter than write-time validation: broad
    # relations are only safe when endpoint types make the fan-out bounded.
    "proxies": {"source": frozenset({"service"}), "target": frozenset({"service"})},
    "depends_on": {
        "source": frozenset({"project", "repo", "service", "tool", "workflow"}),
        "target": frozenset({"concept", "project", "repo", "service", "tool", "workflow"}),
    },
    "manages": {
        "source": frozenset({"person", "agent"}),
        "target": frozenset({"project", "repo", "service", "tool", "workflow"}),
    },
}

REGISTRY_VERSION = "2026-04-24.1"
LEGACY_OPENCLAW_ONTOLOGY_GRAPH = Path.home() / ".openclaw" / "memory" / "ontology" / "graph.jsonl"
DEFAULT_ONTOLOGY_GRAPH = Path(
    os.getenv(
        "BRAIN_ONTOLOGY_GRAPH", str(Path.home() / "server" / "brain" / "data" / "ontology" / "graph.jsonl")
    )
)


@dataclass(frozen=True)
class TermResolution:
    raw: str
    normalized: str
    canonical: str | None
    status: Status
    note: str = ""


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    raw: str
    normalized: str
    severity: Severity
    message: str
    canonical: str | None = None


@dataclass(frozen=True)
class RelationRecord:
    source: str = ""
    relation: str = ""
    target: str = ""
    source_type: str = ""
    target_type: str = ""
    source_id: str = ""
    target_id: str = ""
    origin: str = ""


@dataclass
class AuditReport:
    registry_version: str = REGISTRY_VERSION
    total_relations: int = 0
    status_counts: Counter[str] = field(default_factory=Counter)
    canonical_counts: Counter[str] = field(default_factory=Counter)
    raw_counts: Counter[str] = field(default_factory=Counter)
    issue_counts: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def add(self, record: RelationRecord, issues: Iterable[ValidationIssue]) -> None:
        self.total_relations += 1
        rel = resolve_relation_type(record.relation)
        self.status_counts[rel.status] += 1
        self.raw_counts[rel.normalized or "<blank>"] += 1
        self.canonical_counts[rel.canonical or "<unknown>"] += 1
        for issue in issues:
            key = f"{issue.severity}:{issue.field}:{issue.normalized or '<blank>'}"
            self.issue_counts[key] += 1
            bucket = self.examples[key]
            if len(bucket) < 5:
                bucket.append(
                    {
                        "origin": record.origin,
                        "source": record.source or record.source_id,
                        "source_type": record.source_type,
                        "relation": record.relation,
                        "target": record.target or record.target_id,
                        "target_type": record.target_type,
                        "message": issue.message,
                    }
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_version": self.registry_version,
            "total_relations": self.total_relations,
            "status_counts": dict(sorted(self.status_counts.items())),
            "canonical_counts": dict(sorted(self.canonical_counts.items())),
            "raw_counts": dict(sorted(self.raw_counts.items())),
            "issue_counts": dict(sorted(self.issue_counts.items())),
            "examples": {k: v for k, v in sorted(self.examples.items())},
        }


def normalize_token(value: Any) -> str:
    """Normalize labels from LLMs/frontmatter/graph properties to registry tokens."""
    if value is None:
        return ""
    text = str(value).strip().lower().replace("-", "_")
    text = _TOKEN_RE.sub("_", text)
    return text.strip("_")


def _resolve(
    raw: Any, known: frozenset[str], aliases: dict[str, str], deprecated: dict[str, str] | None = None
) -> TermResolution:
    normalized = normalize_token(raw)
    if not normalized:
        return TermResolution(str(raw or ""), normalized, None, "blank", "blank term")
    if normalized in known:
        return TermResolution(str(raw), normalized, normalized, "known")
    if normalized in aliases:
        return TermResolution(
            str(raw), normalized, aliases[normalized], "alias", f"alias for {aliases[normalized]}"
        )
    if deprecated and normalized in deprecated:
        return TermResolution(
            str(raw),
            normalized,
            deprecated[normalized],
            "deprecated",
            f"deprecated; use {deprecated[normalized]}",
        )
    return TermResolution(str(raw), normalized, None, "unknown", "not in ontology registry")


def resolve_entity_type(raw: Any) -> TermResolution:
    return _resolve(raw, ENTITY_TYPES, ENTITY_TYPE_ALIASES)


def resolve_relation_type(raw: Any) -> TermResolution:
    return _resolve(raw, RELATION_TYPES, RELATION_ALIASES, DEPRECATED_RELATIONS)


def resolve_fact_attribute(raw: Any) -> TermResolution:
    return _resolve(raw, FACT_ATTRIBUTES, FACT_ATTRIBUTE_ALIASES, DEPRECATED_FACT_ATTRIBUTES)


def resolve_memory_kind(raw: Any) -> TermResolution:
    return _resolve(raw, MEMORY_KINDS, MEMORY_KIND_ALIASES)


def resolve_scope(raw: Any) -> TermResolution:
    return _resolve(raw, SCOPES, SCOPE_ALIASES)


def validate_relation(record: RelationRecord, *, strict_unknown: bool = False) -> list[ValidationIssue]:
    """Validate a semantic edge without writing to any store.

    Unknown/deprecated relations are warnings by default so Phase 1 can land as
    observability first. Set strict_unknown=True in future blocking paths.
    """
    issues: list[ValidationIssue] = []
    rel = resolve_relation_type(record.relation)
    if rel.status in {"blank", "unknown", "deprecated"}:
        severity: Severity = "error" if strict_unknown and rel.status != "deprecated" else "warning"
        issues.append(
            ValidationIssue(
                field="relation",
                raw=rel.raw,
                normalized=rel.normalized,
                canonical=rel.canonical,
                severity=severity,
                message=rel.note,
            )
        )
    elif rel.status == "alias":
        issues.append(
            ValidationIssue(
                field="relation",
                raw=rel.raw,
                normalized=rel.normalized,
                canonical=rel.canonical,
                severity="info",
                message=rel.note,
            )
        )

    src_type = resolve_entity_type(record.source_type) if record.source_type else None
    tgt_type = resolve_entity_type(record.target_type) if record.target_type else None
    for field_name, resolved in (("source_type", src_type), ("target_type", tgt_type)):
        if resolved and resolved.status in {"unknown", "deprecated", "blank"}:
            issues.append(
                ValidationIssue(
                    field=field_name,
                    raw=resolved.raw,
                    normalized=resolved.normalized,
                    canonical=resolved.canonical,
                    severity="warning",
                    message=resolved.note,
                )
            )

    canonical_rel = rel.canonical
    if canonical_rel and canonical_rel in RELATION_CONSTRAINTS:
        constraints = RELATION_CONSTRAINTS[canonical_rel]
        if src_type and src_type.canonical and constraints.get("source"):
            allowed = constraints["source"]
            if src_type.canonical not in allowed:
                issues.append(
                    ValidationIssue(
                        field="source_type",
                        raw=src_type.raw,
                        normalized=src_type.normalized,
                        canonical=src_type.canonical,
                        severity="warning",
                        message=f"{canonical_rel} source should be one of {sorted(allowed)}",
                    )
                )
        if tgt_type and tgt_type.canonical and constraints.get("target"):
            allowed = constraints["target"]
            if tgt_type.canonical not in allowed:
                issues.append(
                    ValidationIssue(
                        field="target_type",
                        raw=tgt_type.raw,
                        normalized=tgt_type.normalized,
                        canonical=tgt_type.canonical,
                        severity="warning",
                        message=f"{canonical_rel} target should be one of {sorted(allowed)}",
                    )
                )
    return issues


def relation_types_compatible(
    relation: Any,
    source_type: Any = "",
    target_type: Any = "",
    *,
    require_known_types: bool = False,
    expansion_policy: bool = False,
) -> bool:
    """Return whether an edge's endpoint types satisfy the central relation contract.

    This is the cheap boolean form used by retrieval-time policy gates. It shares
    RELATION_CONSTRAINTS with validate_relation so hot-path expansion does not
    grow a second, drifting ontology rule table.
    """
    rel = resolve_relation_type(relation)
    if rel.status not in {"known", "alias"} or not rel.canonical:
        return False
    constraint_table = EXPANSION_RELATION_CONSTRAINTS if expansion_policy else RELATION_CONSTRAINTS
    constraints = constraint_table.get(rel.canonical)
    if not constraints:
        return True

    for side, raw_type in (("source", source_type), ("target", target_type)):
        allowed = constraints.get(side)
        if not allowed:
            continue
        resolved = resolve_entity_type(raw_type) if raw_type else None
        if not resolved or not resolved.canonical:
            if require_known_types:
                return False
            continue
        if resolved.canonical not in allowed:
            return False
    return True


def normalize_relation_record(record: RelationRecord) -> RelationRecord:
    rel = resolve_relation_type(record.relation)
    src_type = resolve_entity_type(record.source_type) if record.source_type else None
    tgt_type = resolve_entity_type(record.target_type) if record.target_type else None
    return RelationRecord(
        source=record.source,
        relation=rel.canonical or rel.normalized,
        target=record.target,
        source_type=(src_type.canonical if src_type else record.source_type) or "",
        target_type=(tgt_type.canonical if tgt_type else record.target_type) or "",
        source_id=record.source_id,
        target_id=record.target_id,
        origin=record.origin,
    )


def relation_records_from_metadata(metadata: dict[str, Any], *, origin: str = "") -> list[RelationRecord]:
    """Convert canonical/distilled metadata relations[] to validation records."""
    source = str(metadata.get("id") or metadata.get("title") or origin or "")
    records: list[RelationRecord] = []
    for rel in metadata.get("relations") or []:
        if not isinstance(rel, dict):
            records.append(
                RelationRecord(
                    source=source,
                    source_type="note",
                    relation="",
                    target=str(rel),
                    origin=origin,
                )
            )
            continue
        records.append(
            RelationRecord(
                source=source,
                source_type="note",
                relation=str(rel.get("type") or ""),
                target=str(rel.get("target") or ""),
                origin=origin,
            )
        )
    return records


def issue_summary(issues: Iterable[ValidationIssue]) -> dict[str, int]:
    """Small stable summary key for logs/CLI output."""
    counts: Counter[str] = Counter()
    for issue in issues:
        counts[f"{issue.severity}:{issue.field}:{issue.normalized or '<blank>'}"] += 1
    return dict(sorted(counts.items()))


def validate_metadata_relations(metadata: dict[str, Any], *, origin: str = "") -> list[ValidationIssue]:
    """Validate frontmatter relations[] without modifying metadata."""
    issues: list[ValidationIssue] = []
    for record in relation_records_from_metadata(metadata, origin=origin):
        issues.extend(validate_relation(record))
    return issues


def load_openclaw_ontology(
    path: Path = DEFAULT_ONTOLOGY_GRAPH,
) -> tuple[dict[str, dict[str, str]], list[RelationRecord]]:
    """Load OpenClaw graph.jsonl preserving original relation predicates."""
    entities: dict[str, dict[str, str]] = {}
    pending_relations: list[dict[str, str]] = []
    if not path.exists():
        return entities, []
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("op") == "create" and isinstance(payload.get("entity"), dict):
            ent = payload["entity"]
            props = ent.get("properties") if isinstance(ent.get("properties"), dict) else {}
            ent_id = str(ent.get("id") or "")
            if ent_id:
                entities[ent_id] = {
                    "id": ent_id,
                    "name": str(props.get("name") or ent_id),
                    "type": str(ent.get("type") or "concept"),
                    "origin": f"{path}:{line_no}",
                }
        elif payload.get("op") == "relate":
            pending_relations.append(
                {
                    "from": str(payload.get("from") or ""),
                    "rel": str(payload.get("rel") or ""),
                    "to": str(payload.get("to") or ""),
                    "origin": f"{path}:{line_no}",
                }
            )

    relations: list[RelationRecord] = []
    for rel in pending_relations:
        src = entities.get(rel["from"], {})
        tgt = entities.get(rel["to"], {})
        relations.append(
            RelationRecord(
                source=src.get("name", rel["from"]),
                source_id=rel["from"],
                source_type=src.get("type", ""),
                relation=rel["rel"],
                target=tgt.get("name", rel["to"]),
                target_id=rel["to"],
                target_type=tgt.get("type", ""),
                origin=rel["origin"],
            )
        )
    return entities, relations


def audit_relations(records: Iterable[RelationRecord], *, strict_unknown: bool = False) -> AuditReport:
    report = AuditReport()
    for record in records:
        report.add(record, validate_relation(record, strict_unknown=strict_unknown))
    return report


def registry_summary() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "default_graph": str(DEFAULT_ONTOLOGY_GRAPH),
        "legacy_openclaw_graph": str(LEGACY_OPENCLAW_ONTOLOGY_GRAPH),
        "entity_types": sorted(ENTITY_TYPES),
        "relation_types": sorted(RELATION_TYPES),
        "fact_attributes": sorted(FACT_ATTRIBUTES),
        "memory_kinds": sorted(MEMORY_KINDS),
        "scopes": sorted(SCOPES),
        "fact_attribute_aliases": dict(sorted(FACT_ATTRIBUTE_ALIASES.items())),
        "deprecated_fact_attributes": dict(sorted(DEPRECATED_FACT_ATTRIBUTES.items())),
        "relation_aliases": dict(sorted(RELATION_ALIASES.items())),
        "deprecated_relations": dict(sorted(DEPRECATED_RELATIONS.items())),
        "constrained_relations": {
            rel: {side: sorted(values) for side, values in sides.items() if values}
            for rel, sides in sorted(RELATION_CONSTRAINTS.items())
        },
    }
