"""Unit tests for brain_core.web_search (Phase M6 SearXNG learning loop)."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_web_search(tmp_path, monkeypatch):
    """Wire web_search at a tmp brain.db with the M6 schema."""
    for mod in ("web_search", "config"):
        if mod in sys.modules:
            del sys.modules[mod]
    import web_search

    fake_db = tmp_path / "brain.db"

    # Create the M6 schema by hand (avoids needing the full migrations runner)
    conn = sqlite3.connect(str(fake_db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE web_search_attempts (
          id TEXT PRIMARY KEY, query TEXT NOT NULL, ts TEXT NOT NULL,
          agent TEXT NOT NULL DEFAULT 'unknown', intent TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE web_search_results (
          attempt_id TEXT NOT NULL REFERENCES web_search_attempts(id) ON DELETE CASCADE,
          rank INTEGER NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '',
          chosen INTEGER NOT NULL DEFAULT 0, outcome TEXT,
          PRIMARY KEY (attempt_id, rank)
        );
        CREATE TABLE web_source_trust (
          domain TEXT PRIMARY KEY, n_used INTEGER NOT NULL DEFAULT 0,
          n_correct INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0.5,
          last_updated TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(web_search, "BRAIN_DB", fake_db)
    yield web_search, fake_db
    importlib.reload(web_search)


def test_domain_extraction(isolated_web_search):
    web_search, _ = isolated_web_search
    assert web_search._domain_of("https://www.example.com/path?q=1") == "www.example.com"
    assert web_search._domain_of("http://localhost:8080") == "localhost:8080"
    assert web_search._domain_of("not-a-url") == ""
    assert web_search._domain_of("") == ""


def test_load_domain_trust_empty(isolated_web_search):
    web_search, _ = isolated_web_search
    assert web_search._load_domain_trust([]) == {}
    assert web_search._load_domain_trust([""]) == {}


def test_persist_attempt_writes_rows(isolated_web_search):
    web_search, db_path = isolated_web_search
    results = [
        {"rank": 1, "url": "https://a.com/x", "domain": "a.com", "title": "T1", "snippet": "S1"},
        {"rank": 2, "url": "https://b.com/y", "domain": "b.com", "title": "T2", "snippet": "S2"},
    ]
    web_search._persist_attempt("ws_test1", "test query", "pytest", results)
    conn = sqlite3.connect(str(db_path))
    attempts = conn.execute("SELECT id, query, agent FROM web_search_attempts").fetchall()
    rows = conn.execute("SELECT rank, domain FROM web_search_results ORDER BY rank").fetchall()
    conn.close()
    assert len(attempts) == 1 and attempts[0][0] == "ws_test1"
    assert len(rows) == 2
    assert rows[0] == (1, "a.com")
    assert rows[1] == (2, "b.com")


def test_mark_result_outcome_useful(isolated_web_search):
    web_search, db_path = isolated_web_search
    web_search._persist_attempt(
        "ws_outcome_test",
        "q",
        "pytest",
        [{"rank": 1, "url": "https://x.com", "domain": "x.com", "title": "T", "snippet": "S"}],
    )
    assert web_search.mark_result_outcome("ws_outcome_test", 1, useful=True) is True
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT chosen, outcome FROM web_search_results WHERE attempt_id=?",
        ("ws_outcome_test",),
    ).fetchone()
    conn.close()
    assert row == (1, "useful")


def test_mark_result_outcome_wrong(isolated_web_search):
    web_search, _ = isolated_web_search
    web_search._persist_attempt(
        "ws_wrong",
        "q",
        "pytest",
        [{"rank": 1, "url": "https://y.com", "domain": "y.com", "title": "T", "snippet": "S"}],
    )
    assert web_search.mark_result_outcome("ws_wrong", 1, useful=False) is True


def test_recompute_domain_trust_updates_scores(isolated_web_search):
    web_search, db_path = isolated_web_search
    # Seed: a.com had 8 useful + 2 wrong, b.com had 2 useful + 8 wrong
    for i, (domain, outcome) in enumerate(
        [("a.com", "useful")] * 8
        + [("a.com", "wrong")] * 2
        + [("b.com", "useful")] * 2
        + [("b.com", "wrong")] * 8
    ):
        attempt_id = f"ws_seed_{i}"
        web_search._persist_attempt(
            attempt_id,
            f"q{i}",
            "pytest",
            [{"rank": 1, "url": f"https://{domain}/x", "domain": domain, "title": "T", "snippet": "S"}],
        )
        web_search.mark_result_outcome(attempt_id, 1, useful=(outcome == "useful"))

    result = web_search.recompute_domain_trust()
    assert result["updated"] == 2

    conn = sqlite3.connect(str(db_path))
    rows = {r[0]: r[1] for r in conn.execute("SELECT domain, score FROM web_source_trust").fetchall()}
    conn.close()
    # Laplace prior (1, 1): a = (8+1)/(8+2+2) = 9/12 = 0.75
    #                       b = (2+1)/(2+8+2) = 3/12 = 0.25
    assert 0.7 < rows["a.com"] < 0.8
    assert 0.2 < rows["b.com"] < 0.3
