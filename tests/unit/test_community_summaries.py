from __future__ import annotations

import importlib
import json
import sqlite3


def _reload(monkeypatch, *, enabled: bool = True, max_communities: str | None = None):
    if enabled:
        monkeypatch.setenv("BRAIN_COMMUNITY_SUMMARIES", "1")
    else:
        monkeypatch.delenv("BRAIN_COMMUNITY_SUMMARIES", raising=False)
    if max_communities is None:
        monkeypatch.delenv("BRAIN_COMMUNITY_SUMMARIES_MAX_COMMUNITIES", raising=False)
    else:
        monkeypatch.setenv("BRAIN_COMMUNITY_SUMMARIES_MAX_COMMUNITIES", max_communities)

    import brain_core.community_summaries as community_summaries

    return importlib.reload(community_summaries)


def test_env_cap_controls_max_communities(monkeypatch):
    community_summaries = _reload(monkeypatch, max_communities="3")

    assert community_summaries.stats()["enabled"] is True
    assert community_summaries.stats()["max_communities"] == 3


def test_invalid_env_cap_falls_back_to_default(monkeypatch):
    community_summaries = _reload(monkeypatch, max_communities="not-an-int")

    assert community_summaries.stats()["max_communities"] == 20


def test_get_summaries_matching_respects_enabled_and_token_overlap(tmp_path, monkeypatch):
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE community_summaries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "community_hash TEXT NOT NULL UNIQUE, "
        "entities_json TEXT NOT NULL, "
        "summary TEXT NOT NULL, "
        "atom_count INTEGER NOT NULL DEFAULT 0, "
        "generated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO community_summaries "
        "(community_hash, entities_json, summary, atom_count, generated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "h1",
            json.dumps(["brain system", "Hermes profiles"]),
            "brain/hermes summary",
            2,
            "2026-04-29T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    import config

    monkeypatch.setattr(config, "BRAIN_DB", db_path)

    disabled = _reload(monkeypatch, enabled=False)
    assert disabled.get_summaries_matching("compare brain and agents") == []

    enabled = _reload(monkeypatch, enabled=True)
    matches = enabled.get_summaries_matching("compare brain and agents", limit=1)

    assert len(matches) == 1
    assert matches[0]["summary"] == "brain/hermes summary"
    assert "brain" in matches[0]["matched_tokens"]


def test_clean_entity_name_rejects_operational_noise(monkeypatch):
    community_summaries = _reload(monkeypatch)

    noisy = [
        "--skip-git-repo-check",
        "-32001 request timed out",
        "/brain/ingest/image",
        "/Users/chrischo/server/brain/",
        "endpoint smoke tests",
        "pr",
    ]
    assert [community_summaries._clean_entity_name(v) for v in noisy] == [None] * len(noisy)
    assert community_summaries._clean_entity_name("brain system") == "brain system"
    assert community_summaries._clean_entity_name("Hermes profiles") == "Hermes profiles"


def test_rank_entities_caps_and_prefers_concise_semantic_labels(monkeypatch):
    monkeypatch.setenv("BRAIN_COMMUNITY_SUMMARIES_MAX_ENTITIES", "3")
    community_summaries = _reload(monkeypatch)

    ranked = community_summaries._rank_entities_for_summary(
        {
            "/recall/v2",
            "brain system",
            "Hermes profiles",
            "canonical memory",
            "this is a very long entity label with many words",
        }
    )

    assert ranked == ["brain system", "Hermes profiles", "canonical memory"]


def test_persist_summary_stores_clean_capped_entities(tmp_path, monkeypatch):
    community_summaries = _reload(monkeypatch)
    db_path = tmp_path / "brain.db"

    import config

    monkeypatch.setattr(config, "BRAIN_DB", db_path)
    community_summaries._ensure_schema()

    ok = community_summaries._persist_summary(
        "hash1",
        {"brain system", "Hermes profiles", "/recall/v2", "--flag"},
        "summary",
        2,
    )

    assert ok is True
    rows = sqlite3.connect(db_path).execute("SELECT entities_json FROM community_summaries").fetchall()
    assert json.loads(rows[0][0]) == ["brain system", "Hermes profiles"]
