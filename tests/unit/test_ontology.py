from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ontology import (
    RelationRecord,
    audit_relations,
    load_openclaw_ontology,
    normalize_relation_record,
    registry_summary,
    resolve_entity_type,
    resolve_fact_attribute,
    resolve_relation_type,
    validate_relation,
)


def test_relation_alias_preserves_direction():
    resolved = resolve_relation_type("has-owner")
    assert resolved.status == "alias"
    assert resolved.canonical == "owned_by"


def test_deprecated_ontology_related_warns_to_related_to():
    resolved = resolve_relation_type("ontology_related")
    assert resolved.status == "deprecated"
    assert resolved.canonical == "related_to"

    issues = validate_relation(RelationRecord(source="a", relation="ontology_related", target="b"))
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].canonical == "related_to"


def test_entity_type_aliases_normalize():
    assert resolve_entity_type("Repository").canonical == "repo"
    assert resolve_entity_type("assistant").canonical == "agent"


def test_relation_constraints_catch_wrong_direction():
    issues = validate_relation(
        RelationRecord(
            source="MCC",
            source_type="project",
            relation="owns",
            target="Chris",
            target_type="person",
        )
    )
    assert any(issue.field == "source_type" for issue in issues)
    assert any(issue.field == "target_type" for issue in issues)


def test_owned_by_relation_allows_project_to_person():
    issues = validate_relation(
        RelationRecord(
            source="MCC",
            source_type="project",
            relation="has_owner",
            target="Chris",
            target_type="person",
        )
    )
    assert [issue.severity for issue in issues] == ["info"]
    normalized = normalize_relation_record(
        RelationRecord(
            source="MCC", source_type="Project", relation="has_owner", target="Chris", target_type="Person"
        )
    )
    assert normalized.relation == "owned_by"
    assert normalized.source_type == "project"
    assert normalized.target_type == "person"


def test_load_openclaw_ontology_preserves_original_relation(tmp_path: Path):
    path = tmp_path / "graph.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "person_chris", "type": "Person", "properties": {"name": "Chris"}},
                    }
                ),
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "proj_mcc", "type": "Project", "properties": {"name": "MCC"}},
                    }
                ),
                json.dumps({"op": "relate", "from": "proj_mcc", "rel": "has_owner", "to": "person_chris"}),
            ]
        )
        + "\n"
    )
    entities, relations = load_openclaw_ontology(path)
    assert set(entities) == {"person_chris", "proj_mcc"}
    assert relations[0].relation == "has_owner"
    assert relations[0].source_type == "Project"
    assert relations[0].target_type == "Person"

    report = audit_relations(relations).to_dict()
    assert report["status_counts"] == {"alias": 1}
    assert report["canonical_counts"] == {"owned_by": 1}


def test_registry_summary_has_full_export_primitives():
    summary = registry_summary()
    assert "entity_types" in summary
    assert "relation_types" in summary
    assert "fact_attributes" in summary
    assert "deprecated_relations" in summary
    assert "owned_by" in summary["relation_types"]
    assert "role" in summary["fact_attributes"]


def test_fact_attribute_contract_is_separate_from_relation_contract():
    assert resolve_fact_attribute("role").status == "known"
    assert resolve_fact_attribute("test_attr").status == "deprecated"
    assert resolve_fact_attribute("test_attr").canonical == "test_fixture"
    assert resolve_relation_type("role").status == "unknown"


def test_audit_collect_facts_uses_fact_attribute_contract(tmp_path: Path):
    from audit_ontology import collect_facts

    facts_db = tmp_path / "facts.db"
    with sqlite3.connect(str(facts_db)) as conn:
        conn.execute("CREATE TABLE facts (attribute TEXT)")
        conn.executemany(
            "INSERT INTO facts(attribute) VALUES (?)", [("role",), ("test_attr",), ("test_attr",)]
        )

    report = collect_facts(facts_db)
    assert report["attributes"]["role"]["attribute_status"] == "known"
    assert report["attributes"]["test_attr"]["attribute_status"] == "deprecated"
    assert report["attributes"]["test_attr"]["canonical_attribute"] == "test_fixture"


def test_search_unified_load_ontology_preserves_one_hop_adjacency(tmp_path: Path, monkeypatch):
    import search_unified

    path = tmp_path / "graph.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "person_chris", "type": "Person", "properties": {"name": "Chris"}},
                    }
                ),
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "proj_mcc", "type": "Project", "properties": {"name": "MCC"}},
                    }
                ),
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "agent_ellie", "type": "Agent", "properties": {"name": "Ellie"}},
                    }
                ),
                json.dumps({"op": "relate", "from": "proj_mcc", "rel": "has_owner", "to": "person_chris"}),
                json.dumps({"op": "relate", "from": "person_chris", "rel": "has_agent", "to": "agent_ellie"}),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(search_unified, "ONTOLOGY_GRAPH", path)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_SOURCE", "file")
    monkeypatch.setattr(search_unified, "_ontology_cache", None)
    monkeypatch.setattr(search_unified, "_ontology_cache_ts", 0.0)

    entities, adjacency = search_unified.load_ontology()
    assert "chris" in entities
    assert adjacency["Chris"] == ["Ellie"]
    assert adjacency["MCC"] == ["Chris"]
    assert search_unified.ontology_expansion_terms("ask Chris", adjacency, max_terms=5) == ["Ellie"]


def test_search_unified_load_ontology_prefers_neo4j_with_file_fallback(monkeypatch, tmp_path: Path):
    import search_unified

    path = tmp_path / "graph.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "person_chris", "type": "Person", "properties": {"name": "Chris"}},
                    }
                ),
                json.dumps(
                    {
                        "op": "create",
                        "entity": {"id": "agent_file", "type": "Agent", "properties": {"name": "FileAgent"}},
                    }
                ),
                json.dumps({"op": "relate", "from": "person_chris", "rel": "has_agent", "to": "agent_file"}),
            ]
        )
        + "\n"
    )

    monkeypatch.setattr(search_unified, "ONTOLOGY_GRAPH", path)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_SOURCE", "neo4j")
    monkeypatch.setattr(search_unified, "_ontology_cache", None)
    monkeypatch.setattr(search_unified, "_ontology_cache_ts", 0.0)
    monkeypatch.setattr(
        search_unified,
        "_load_neo4j_ontology",
        lambda relations=None: (
            {"chris": {"name": "Chris", "type": "person"}, "jenna": {"name": "Jenna", "type": "agent"}},
            {"Chris": ["Jenna"], "Jenna": []},
        ),
    )

    _entities, adjacency = search_unified.load_ontology()
    assert adjacency == {"Chris": ["Jenna"], "Jenna": []}

    monkeypatch.setattr(search_unified, "_ontology_cache", None)
    monkeypatch.setattr(search_unified, "_load_neo4j_ontology", lambda relations=None: ({}, {}))
    _entities, adjacency = search_unified.load_ontology()
    assert adjacency == {"Chris": ["FileAgent"], "FileAgent": []}


def test_search_unified_neo4j_loader_applies_type_guards(monkeypatch):
    import sys
    import types

    import search_unified

    fake = types.SimpleNamespace(
        is_healthy=lambda: True,
        run_query=lambda *_args, **_kwargs: [
            {
                "source": "nginx",
                "source_type": "service",
                "relation": "proxies",
                "target": "searxng",
                "target_type": "service",
            },
            {
                "source": "Brain",
                "source_type": "project",
                "relation": "proxies",
                "target": "searxng",
                "target_type": "service",
            },
        ],
    )
    monkeypatch.setitem(sys.modules, "neo4j_client", fake)

    _entities, adjacency = search_unified._load_neo4j_ontology(("proxies",))
    assert adjacency == {"nginx": [], "searxng": ["nginx"]}


def test_search_unified_ontology_expansion_is_feature_flagged(monkeypatch):
    import search_unified

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(search_unified, "load_ontology", lambda *args, **kwargs: ({}, {"Chris": ["MCC"]}))
    query, terms, elapsed_ms = search_unified.maybe_expand_query_with_ontology("ask Chris")
    assert query == "ask Chris"
    assert terms == []
    assert elapsed_ms == 0

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    query, terms, elapsed_ms = search_unified.maybe_expand_query_with_ontology("ask Chris")
    assert query == "ask Chris MCC"
    assert terms == ["MCC"]
    assert elapsed_ms >= 0


def test_search_unified_sidecar_mode_preserves_primary_query(monkeypatch):
    import search_unified

    seen_queries: list[tuple[str, int]] = []

    def fake_search_rag(query, limit, where=None, collections=None):
        seen_queries.append((query, limit))
        return []

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "sidecar")
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_SIDECAR_LIMIT", 2)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", 5)
    monkeypatch.setattr(search_unified, "load_ontology", lambda *args, **kwargs: ({}, {"Chris": ["Jenna"]}))
    monkeypatch.setattr(search_unified, "search_rag", fake_search_rag)

    payload = search_unified.search_all("ask Chris", limit=5, sources=["rag"], collections=["knowledge"])

    assert ("ask Chris", 10) in seen_queries
    assert ("ask Chris Jenna", 2) in seen_queries
    assert payload["source_timing"]["ontology_expansion_sidecar_mode"] == 1
    assert payload["source_timing"]["ontology_sidecar_limit"] == 2
    assert payload["source_timing"]["ontology_sidecar_skipped_specific_lookup"] == 0


def test_search_unified_sidecar_skips_exact_lookup_without_relation_intent(monkeypatch):
    import search_unified

    seen_queries: list[tuple[str, int]] = []

    def fake_search_rag(query, limit, where=None, collections=None):
        seen_queries.append((query, limit))
        return []

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "sidecar")
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_SIDECAR_LIMIT", 2)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", 5)
    monkeypatch.setattr(
        search_unified,
        "load_ontology",
        lambda *args, **kwargs: ({}, {"qdrant": ["brain server", "rag stack"]}),
    )
    monkeypatch.setattr(search_unified, "search_rag", fake_search_rag)

    payload = search_unified.search_all(
        "qdrant native port 6333", limit=5, sources=["rag"], collections=["knowledge"]
    )

    assert any(query == "qdrant native port 6333" for query, _limit in seen_queries)
    assert all(query != "qdrant native port 6333 brain server rag stack" for query, _limit in seen_queries)
    assert payload["source_timing"]["ontology_expansion_applied"] is True
    assert payload["source_timing"]["ontology_sidecar_limit"] == 0
    assert payload["source_timing"]["ontology_sidecar_skipped_specific_lookup"] == 1


def test_search_unified_sidecar_keeps_exact_lookup_with_relation_intent(monkeypatch):
    import search_unified

    seen_queries: list[tuple[str, int]] = []

    def fake_search_rag(query, limit, where=None, collections=None):
        seen_queries.append((query, limit))
        return []

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "sidecar")
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_SIDECAR_LIMIT", 2)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", 5)
    monkeypatch.setattr(search_unified, "load_ontology", lambda *args, **kwargs: ({}, {"searxng": ["nginx"]}))
    monkeypatch.setattr(search_unified, "search_rag", fake_search_rag)

    payload = search_unified.search_all(
        "searxng proxy route 443", limit=5, sources=["rag"], collections=["knowledge"]
    )

    assert ("searxng proxy route 443 nginx", 1) in seen_queries
    assert payload["source_timing"]["ontology_sidecar_limit"] == 1
    assert payload["source_timing"]["ontology_sidecar_skipped_specific_lookup"] == 0


def test_search_unified_rewrite_mode_replaces_primary_query(monkeypatch):
    import search_unified

    seen_queries: list[str] = []

    def fake_search_rag(query, limit, where=None, collections=None):
        seen_queries.append(query)
        return []

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "rewrite")
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", 5)
    monkeypatch.setattr(search_unified, "load_ontology", lambda *args, **kwargs: ({}, {"Chris": ["Jenna"]}))
    monkeypatch.setattr(search_unified, "search_rag", fake_search_rag)

    search_unified.search_all("ask Chris", limit=5, sources=["rag"], collections=["knowledge"])

    assert "ask Chris" not in seen_queries
    assert "ask Chris Jenna" in seen_queries


def test_rollout_live_smoke_retries_transient_latency(monkeypatch, tmp_path: Path):
    import ontology_rollout_gate

    class FakeResponse:
        def __init__(self, payload: dict):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    token_path = tmp_path / "token"
    token_path.write_text("test-token")
    payloads = [
        {
            "latency_ms": 5000,
            "timing": {
                "ontology_expansion_applied": False,
                "ontology_expansion_terms": 0,
                "ontology_expansion_ms": 0,
            },
            "results": [],
        },
        {
            "latency_ms": 50,
            "timing": {
                "ontology_expansion_applied": False,
                "ontology_expansion_terms": 0,
                "ontology_expansion_ms": 0,
            },
            "results": [],
        },
    ]

    def fake_urlopen(_req, timeout=20):
        assert timeout == 20
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(ontology_rollout_gate, "SECRET_FILE", token_path)
    monkeypatch.setattr(
        ontology_rollout_gate, "LIVE_CASES", [{"query": "nginx proxy route", "expected_applied": False}]
    )
    monkeypatch.setattr(ontology_rollout_gate.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ontology_rollout_gate.time, "sleep", lambda _seconds: None)

    result = ontology_rollout_gate._live_smoke(
        "http://brain.test",
        max_live_p95_ms=1000,
        live_retries=1,
    )

    assert result["passed"] is True
    assert result["p95_latency_ms"] == 50
    assert result["results"][0]["attempt"] == 2
    assert [attempt["latency_ms"] for attempt in result["results"][0]["attempts"]] == [5000, 50]


def test_search_unified_conditional_relations_are_query_intent_gated(monkeypatch):
    import search_unified

    monkeypatch.setattr(
        search_unified, "BRAIN_ONTOLOGY_EXPANSION_RELATIONS", ("has_agent", "owned_by", "owns")
    )
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED", False)
    assert search_unified.ontology_relations_for_query("nginx proxy route") == (
        "has_agent",
        "owned_by",
        "owns",
    )

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED", True)
    assert search_unified.ontology_relations_for_query("nginx proxy route") == (
        "has_agent",
        "owned_by",
        "owns",
        "proxies",
    )
    assert search_unified.ontology_relations_for_query("service dependency requirements") == (
        "has_agent",
        "owned_by",
        "owns",
        "depends_on",
    )
    assert search_unified.ontology_relations_for_query("Jenna responsibilities") == (
        "has_agent",
        "owned_by",
        "owns",
    )
    assert search_unified.ontology_relations_for_query("MCC owner") == ("has_agent", "owned_by", "owns")


def test_search_unified_relation_direction_policies():
    import search_unified

    adjacency: dict[str, list[str]] = {}
    search_unified._append_ontology_edge(adjacency, "nginx", "searxng", "proxies")
    search_unified._append_ontology_edge(adjacency, "brain system", "neo4j", "depends_on")
    search_unified._append_ontology_edge(adjacency, "chris", "project x", "manages")

    assert search_unified.ontology_expansion_terms("nginx routing", adjacency, max_terms=5) == []
    assert search_unified.ontology_expansion_terms("searxng routing", adjacency, max_terms=5) == ["nginx"]
    assert search_unified.ontology_expansion_terms("neo4j dependency", adjacency, max_terms=5) == [
        "brain system"
    ]
    assert search_unified.ontology_expansion_terms("project x manager", adjacency, max_terms=5) == ["chris"]


def test_search_unified_edge_type_guards_filter_noisy_edges():
    import search_unified

    assert search_unified._ontology_edge_allowed("proxies", "service", "service")
    assert not search_unified._ontology_edge_allowed("proxies", "project", "service")

    assert search_unified._ontology_edge_allowed("depends_on", "project", "service")
    assert search_unified._ontology_edge_allowed("depends_on", "repo", "concept")
    assert not search_unified._ontology_edge_allowed("depends_on", "concept", "service")

    assert search_unified._ontology_edge_allowed("manages", "person", "project")
    assert search_unified._ontology_edge_allowed("manages", "agent", "workflow")
    assert not search_unified._ontology_edge_allowed("manages", "person", "concept")


def test_relation_type_compatibility_uses_central_constraints():
    from ontology import relation_types_compatible

    assert relation_types_compatible("has_owner", "project", "person", require_known_types=True)
    assert not relation_types_compatible("has_owner", "person", "project", require_known_types=True)
    assert relation_types_compatible(
        "proxies", "service", "service", require_known_types=True, expansion_policy=True
    )
    assert not relation_types_compatible(
        "proxies", "", "service", require_known_types=True, expansion_policy=True
    )
    assert relation_types_compatible("proxies", "", "service", require_known_types=True) is True


def test_metadata_relation_validation_reports_unknown():
    from ontology import issue_summary, validate_metadata_relations

    metadata = {"id": "canon_test", "relations": [{"type": "made_up_relation", "target": "x"}]}
    summary = issue_summary(validate_metadata_relations(metadata, origin="unit"))
    assert summary == {"warning:relation:made_up_relation": 1}


def test_pipeline_common_uses_central_ontology_registry():
    from common import ontology_metadata_warning_summary

    metadata = {"id": "canon_test", "relations": [{"type": "made_up_relation", "target": "x"}]}
    assert ontology_metadata_warning_summary(metadata, "unit") == {"warning:relation:made_up_relation": 1}


def test_entity_graph_validation_is_warning_only(brain_env, caplog):
    import sys

    sys.modules.pop("entity_graph", None)
    import entity_graph

    with caplog.at_level("WARNING", logger="brain.entity_graph"):
        summary = entity_graph._ontology_validate_extracted_relations(
            entities=[
                {"name": "Chris", "type": "person"},
                {"name": "Brain", "type": "project"},
            ],
            relations=[{"source": "Chris", "relationship": "made_up_relation", "target": "Brain"}],
            origin="unit_memory",
        )

    assert summary == {"warning:relation:made_up_relation": 1}
    assert "ontology graph validation warnings" in caplog.text
