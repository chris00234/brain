"""tests/unit/test_cross_repo_recall.py — analog-edit lookup."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import cross_repo_recall as crr  # noqa: E402


def _bootstrap(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE raw_events (
            id TEXT PRIMARY KEY,
            content_hash TEXT,
            timestamp TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT DEFAULT '',
            actor TEXT DEFAULT 'chris',
            visibility TEXT DEFAULT 'private',
            scrub_status TEXT DEFAULT 'scrubbed',
            content TEXT NOT NULL,
            attachments_json TEXT DEFAULT '[]',
            entities_json TEXT DEFAULT '[]',
            json_path TEXT,
            created_at TEXT NOT NULL,
            processed_at TEXT
        );
        CREATE VIRTUAL TABLE raw_events_fts USING fts5(content);
        CREATE TRIGGER raw_events_ai AFTER INSERT ON raw_events BEGIN
            INSERT INTO raw_events_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
        CREATE TABLE coding_event_outcomes (
            event_id TEXT PRIMARY KEY,
            outcome TEXT NOT NULL,
            outcome_source TEXT NOT NULL,
            outcome_ts TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _insert(
    db: Path,
    event_id: str,
    cwd: str,
    file_path: str,
    new_preview: str,
    outcome: str = "refined",
    ts: str = "2026-05-10T00:00:00Z",
) -> None:
    payload = {
        "cwd": cwd,
        "file_path": file_path,
        "tool": "Edit",
        "new_preview": new_preview,
        "old_preview": "",
    }
    content = json.dumps(payload)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO raw_events (id, timestamp, source_type, content, created_at) "
            "VALUES (?, ?, 'coding_event', ?, ?)",
            (event_id, ts, content, ts),
        )
        # FTS rowid auto-populated by AFTER INSERT trigger
        conn.execute(
            "INSERT INTO coding_event_outcomes (event_id, outcome, outcome_source, outcome_ts) "
            "VALUES (?, ?, 'chain', ?)",
            (event_id, outcome, ts),
        )


def test_find_analogs_returns_other_repos(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(
        db,
        "ev_a",
        cwd="/Users/c/repo_a",
        file_path="/Users/c/repo_a/auth.py",
        new_preview="def refresh_token(): jwt_decode_unverified return None",
    )
    _insert(
        db,
        "ev_b",
        cwd="/Users/c/repo_b",
        file_path="/Users/c/repo_b/auth.py",
        new_preview="def refresh_token(): jwt_decode_unverified handle expired",
    )
    items = crr.find_analogs(
        query="refresh_token jwt_decode_unverified",
        current_repo="repo_a",
        brain_db_path=db,
        limit=5,
    )
    repos = {i["repo"] for i in items}
    assert "repo_b" in repos
    assert "repo_a" not in repos


def test_find_analogs_excludes_negative_outcomes(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(
        db,
        "ev_rev",
        cwd="/Users/c/repo_b",
        file_path="/Users/c/repo_b/auth.py",
        new_preview="def refresh_token(): jwt_decode_unverified return None",
        outcome="reverted",
    )
    items = crr.find_analogs(
        query="refresh_token jwt_decode_unverified",
        current_repo="repo_a",
        brain_db_path=db,
    )
    assert items == []


def test_find_analogs_dedupes_by_repo(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    for n in range(3):
        _insert(
            db,
            f"ev_b{n}",
            cwd="/Users/c/repo_b",
            file_path=f"/Users/c/repo_b/file_{n}.py",
            new_preview="def refresh_token(): jwt_decode_unverified handle",
            ts=f"2026-05-1{n}T00:00:00Z",
        )
    _insert(
        db,
        "ev_c",
        cwd="/Users/c/repo_c",
        file_path="/Users/c/repo_c/auth.py",
        new_preview="def refresh_token(): jwt_decode_unverified handle",
    )
    items = crr.find_analogs(
        query="refresh_token jwt_decode_unverified",
        current_repo="repo_a",
        brain_db_path=db,
        limit=5,
    )
    repos = [i["repo"] for i in items]
    assert sorted(set(repos)) == ["repo_b", "repo_c"]
    assert len(repos) == 2  # one item per repo


def test_find_analogs_empty_query(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    assert crr.find_analogs(query="", brain_db_path=db) == []


def test_find_analogs_missing_db(tmp_path: Path) -> None:
    assert crr.find_analogs(query="anything", brain_db_path=tmp_path / "missing.db") == []
