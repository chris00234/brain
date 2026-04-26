#!/usr/bin/env python3
"""Read-only ontology audit for Brain semantic relation drift.

Phase 0/1 tool: reports current relation/type usage without changing Neo4j,
SQLite, Qdrant, canonical files, or recall behavior.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = ROOT / "brain_core"
PIPELINE = ROOT / "pipeline"
for path in (BRAIN_CORE, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ontology import (  # noqa: E402
    DEFAULT_ONTOLOGY_GRAPH,
    RelationRecord,
    audit_relations,
    load_openclaw_ontology,
    normalize_relation_record,
    registry_summary,
    resolve_fact_attribute,
    resolve_relation_type,
)

KNOWLEDGE_ROOT = ROOT.parent / "knowledge"
BRAIN_DB = ROOT / "logs" / "brain.db"
AUTONOMY_DB = ROOT / "logs" / "autonomy.db"
FACTS_DB = ROOT / "logs" / "facts.db"


def _counter_dict(counter: Any) -> dict[str, int]:
    return {str(k): int(v) for k, v in sorted(dict(counter).items())}


def _report_to_dict(report: Any) -> dict[str, Any]:
    payload = report.to_dict()
    for key in ("status_counts", "canonical_counts", "raw_counts", "issue_counts"):
        payload[key] = _counter_dict(payload.get(key, {}))
    return payload


def collect_ontology_graph(path: Path) -> dict[str, Any]:
    entities, relations = load_openclaw_ontology(path)
    report = audit_relations(relations)
    return {"path": str(path), "entities": len(entities), "relations": _report_to_dict(report)}


def collect_markdown_relations(knowledge_root: Path) -> dict[str, Any]:
    from common import ValidationError, parse_markdown_frontmatter

    records: list[RelationRecord] = []
    files_scanned = 0
    parse_errors: list[str] = []
    for folder in ("canonical", "distilled"):
        base = knowledge_root / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            files_scanned += 1
            try:
                metadata, _body = parse_markdown_frontmatter(path)
            except (ValidationError, json.JSONDecodeError, OSError) as exc:
                if len(parse_errors) < 10:
                    parse_errors.append(f"{path}: {exc}")
                continue
            source_id = str(metadata.get("id") or path.relative_to(knowledge_root))
            for rel in metadata.get("relations") or []:
                if not isinstance(rel, dict):
                    continue
                records.append(
                    RelationRecord(
                        source=source_id,
                        source_type="note",
                        relation=str(rel.get("type") or ""),
                        target=str(rel.get("target") or ""),
                        origin=str(path),
                    )
                )
    return {
        "knowledge_root": str(knowledge_root),
        "files_scanned": files_scanned,
        "parse_errors": parse_errors,
        "relations": _report_to_dict(audit_relations(records)),
    }


def collect_sqlite_relations(autonomy_db: Path) -> dict[str, Any]:
    if not autonomy_db.exists():
        return {"path": str(autonomy_db), "available": False}
    records: list[RelationRecord] = []
    with sqlite3.connect(str(autonomy_db)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "entity_relations" not in tables:
            return {"path": str(autonomy_db), "available": False, "reason": "no entity_relations"}
        for row in conn.execute(
            """
            SELECT r.relationship, s.name AS source_name, s.entity_type AS source_type,
                   t.name AS target_name, t.entity_type AS target_type
            FROM entity_relations r
            LEFT JOIN entities s ON r.source_entity = s.id
            LEFT JOIN entities t ON r.target_entity = t.id
            """
        ):
            records.append(
                RelationRecord(
                    source=row["source_name"] or "",
                    source_type=row["source_type"] or "",
                    relation=row["relationship"] or "",
                    target=row["target_name"] or "",
                    target_type=row["target_type"] or "",
                    origin=str(autonomy_db),
                )
            )
    return {
        "path": str(autonomy_db),
        "available": True,
        "relations": _report_to_dict(audit_relations(records)),
    }


def collect_neo4j_relations() -> dict[str, Any]:
    try:
        from neo4j_client import is_healthy, run_query

        if not is_healthy():
            return {"available": False, "reason": "neo4j unavailable"}
        rows = run_query(
            """
            MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
            RETURN coalesce(r.relationship, '') AS relationship,
                   coalesce(s.name, '') AS source_name,
                   coalesce(s.entity_type, '') AS source_type,
                   coalesce(t.name, '') AS target_name,
                   coalesce(t.entity_type, '') AS target_type,
                   count(*) AS count
            ORDER BY count DESC
            """
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:300]}

    records: list[RelationRecord] = []
    relation_counts: dict[str, int] = {}
    for row in rows:
        count = int(row.get("count") or 1)
        relation = str(row.get("relationship") or "")
        relation_counts[relation or "<blank>"] = relation_counts.get(relation or "<blank>", 0) + count
        # Feed one sample to validation; counts are preserved separately.
        records.append(
            RelationRecord(
                source=str(row.get("source_name") or ""),
                source_type=str(row.get("source_type") or ""),
                relation=relation,
                target=str(row.get("target_name") or ""),
                target_type=str(row.get("target_type") or ""),
                origin="neo4j://RELATES_TO",
            )
        )
    return {
        "available": True,
        "raw_relation_counts": dict(sorted(relation_counts.items(), key=lambda item: (-item[1], item[0]))),
        "relation_types": _report_to_dict(audit_relations(records)),
    }


def _openclaw_relation_lookup(path: Path) -> dict[tuple[str, str], list[RelationRecord]]:
    _entities, relations = load_openclaw_ontology(path)
    lookup: dict[tuple[str, str], list[RelationRecord]] = {}
    for relation in relations:
        normalized = normalize_relation_record(relation)
        key = (relation.source.strip().lower(), relation.target.strip().lower())
        lookup.setdefault(key, []).append(normalized)
    return lookup


def collect_neo4j_migration_plan(ontology_path: Path) -> dict[str, Any]:
    """Build a read-only, reversible migration plan for relation drift.

    This intentionally emits examples and counts only. It does not execute any
    write and does not add an --apply mode, because production graph rewrites
    need an explicit backup/rollback step after review.
    """
    try:
        from neo4j_client import is_healthy, run_query

        if not is_healthy():
            return {"available": False, "reason": "neo4j unavailable"}
        rows = run_query(
            """
            MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
            RETURN coalesce(r.relationship, '') AS relationship,
                   coalesce(s.name, '') AS source_name,
                   coalesce(t.name, '') AS target_name,
                   count(*) AS count
            ORDER BY relationship, source_name, target_name
            """
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:300]}

    relation_rewrites: dict[str, dict[str, Any]] = {}
    ontology_lookup = _openclaw_relation_lookup(ontology_path)
    ontology_related_recovery: list[dict[str, Any]] = []
    unrecoverable_ontology_related: list[dict[str, Any]] = []

    for row in rows:
        relationship = str(row.get("relationship") or "")
        count = int(row.get("count") or 0)
        resolved = resolve_relation_type(relationship)
        if resolved.status in {"alias", "deprecated"}:
            bucket = relation_rewrites.setdefault(
                relationship or "<blank>",
                {
                    "status": resolved.status,
                    "canonical": resolved.canonical,
                    "count": 0,
                    "safe_default_action": "set relationship property to canonical value with provenance",
                },
            )
            bucket["count"] += count

        if resolved.normalized != "ontology_related":
            continue
        source_name = str(row.get("source_name") or "")
        target_name = str(row.get("target_name") or "")
        candidates = ontology_lookup.get((source_name.strip().lower(), target_name.strip().lower()), [])
        recovery_payload = {
            "source": source_name,
            "target": target_name,
            "count": count,
        }
        if candidates:
            canonical_relations = sorted(
                {candidate.relation for candidate in candidates if candidate.relation}
            )
            ontology_related_recovery.append(
                {
                    **recovery_payload,
                    "suggested_relations": canonical_relations,
                    "confidence": "high" if len(canonical_relations) == 1 else "manual_review",
                }
            )
        else:
            unrecoverable_ontology_related.append(
                {
                    **recovery_payload,
                    "suggested_relations": ["related_to"],
                    "confidence": "low",
                    "reason": "no matching source/target pair found in OpenClaw ontology graph",
                }
            )

    return {
        "available": True,
        "mode": "dry_run_only",
        "writes_performed": False,
        "relation_rewrites": dict(sorted(relation_rewrites.items())),
        "ontology_related_recovery": ontology_related_recovery,
        "unrecoverable_ontology_related": unrecoverable_ontology_related,
        "backup_required_before_apply": True,
        "apply_guardrails": [
            "export current RELATES_TO edges before any rewrite",
            "write ontology_migrated_from and ontology_migrated_at on every changed edge",
            "rewrite only r.relationship property; do not delete nodes or relationships",
            "run audit before/after and compare edge counts",
        ],
        "cypher_templates": {
            "backup_export": (
                "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
                "RETURN s.id AS source_id, s.name AS source, r.relationship AS relationship, "
                "t.id AS target_id, t.name AS target"
            ),
            "rewrite_by_relationship": (
                "MATCH ()-[r:RELATES_TO {relationship: $from}]->() "
                "SET r.relationship = $to, "
                "r.ontology_migrated_from = $from, "
                "r.ontology_migrated_at = timestamp() "
                "RETURN count(r) AS changed"
            ),
            "rewrite_specific_edge": (
                "MATCH (s:Entity)-[r:RELATES_TO {relationship: $from}]->(t:Entity) "
                "WHERE s.name = $source AND t.name = $target "
                "SET r.relationship = $to, "
                "r.ontology_migrated_from = $from, "
                "r.ontology_migrated_at = timestamp() "
                "RETURN count(r) AS changed"
            ),
        },
    }


def collect_facts(facts_db: Path) -> dict[str, Any]:
    if not facts_db.exists():
        return {"path": str(facts_db), "available": False}
    with sqlite3.connect(str(facts_db)) as conn:
        rows = conn.execute(
            "SELECT attribute, COUNT(*) AS count FROM facts GROUP BY attribute ORDER BY count DESC, attribute"
        ).fetchall()
    attributes = {str(attr or "<blank>"): int(count) for attr, count in rows}
    classified = {
        attr: {
            "count": count,
            "attribute_status": resolve_fact_attribute(attr).status,
            "canonical_attribute": resolve_fact_attribute(attr).canonical,
        }
        for attr, count in attributes.items()
    }
    return {"path": str(facts_db), "available": True, "attributes": classified}


def collect_counts() -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for name, path, tables in (
        ("brain_db", BRAIN_DB, ("atoms", "entities", "atom_entity", "provenance", "raw_events")),
        ("autonomy_db", AUTONOMY_DB, ("entities", "entity_relations", "memory_access", "decision_ledger")),
    ):
        if not path.exists():
            counts[name] = {"path": str(path), "available": False}
            continue
        table_counts: dict[str, int] = {}
        count_queries = {
            "atoms": "SELECT COUNT(*) FROM atoms",
            "entities": "SELECT COUNT(*) FROM entities",
            "atom_entity": "SELECT COUNT(*) FROM atom_entity",
            "provenance": "SELECT COUNT(*) FROM provenance",
            "raw_events": "SELECT COUNT(*) FROM raw_events",
            "entity_relations": "SELECT COUNT(*) FROM entity_relations",
            "memory_access": "SELECT COUNT(*) FROM memory_access",
            "decision_ledger": "SELECT COUNT(*) FROM decision_ledger",
        }
        with sqlite3.connect(str(path)) as conn:
            existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in tables:
                if table in existing:
                    table_counts[table] = int(conn.execute(count_queries[table]).fetchone()[0])
        counts[name] = {"path": str(path), "available": True, "tables": table_counts}
    return counts


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "registry": registry_summary(),
        "baseline_counts": collect_counts(),
        "ontology_graph": collect_ontology_graph(Path(args.ontology_path)),
        "knowledge_relations": collect_markdown_relations(Path(args.knowledge_root)),
        "sqlite_entity_relations": collect_sqlite_relations(Path(args.autonomy_db)),
        "neo4j_relations": {"skipped": True} if args.skip_neo4j else collect_neo4j_relations(),
        "neo4j_migration_plan": {"skipped": True}
        if args.skip_neo4j
        else collect_neo4j_migration_plan(Path(args.ontology_path)),
        "facts": collect_facts(Path(args.facts_db)),
    }


def print_text(report: dict[str, Any]) -> None:
    print(f"Ontology registry: {report['registry']['version']}")
    print("\nBaseline counts:")
    for name, payload in report["baseline_counts"].items():
        print(f"  {name}: {payload.get('tables', payload)}")
    for label in ("ontology_graph", "knowledge_relations", "sqlite_entity_relations"):
        section = report[label]
        rel = section.get("relations", {})
        print(f"\n{label}:")
        print(f"  total_relations: {rel.get('total_relations', 0)}")
        print(f"  status_counts: {rel.get('status_counts', {})}")
        print(f"  canonical_counts: {rel.get('canonical_counts', {})}")
        issues = rel.get("issue_counts", {})
        if issues:
            print(f"  issues: {issues}")
    neo = report["neo4j_relations"]
    print("\nneo4j_relations:")
    if neo.get("available"):
        print(f"  raw_relation_counts: {neo.get('raw_relation_counts', {})}")
        print(f"  status_counts: {neo.get('relation_types', {}).get('status_counts', {})}")
    else:
        print(f"  {neo}")
    migration = report["neo4j_migration_plan"]
    print("\nneo4j_migration_plan:")
    if migration.get("available"):
        print(f"  mode: {migration.get('mode')}")
        print(f"  relation_rewrites: {migration.get('relation_rewrites', {})}")
        print(f"  ontology_related_recovery: {len(migration.get('ontology_related_recovery', []))}")
        print(f"  unrecoverable_ontology_related: {len(migration.get('unrecoverable_ontology_related', []))}")
    else:
        print(f"  {migration}")
    print("\nfacts:")
    print(f"  {report['facts']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only ontology drift audit for Brain.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text summary")
    parser.add_argument("--skip-neo4j", action="store_true", help="Do not query Neo4j")
    parser.add_argument("--ontology-path", default=str(DEFAULT_ONTOLOGY_GRAPH))
    parser.add_argument("--knowledge-root", default=str(KNOWLEDGE_ROOT))
    parser.add_argument("--autonomy-db", default=str(AUTONOMY_DB))
    parser.add_argument("--facts-db", default=str(FACTS_DB))
    args = parser.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
