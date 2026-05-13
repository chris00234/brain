"""tests/unit/test_db_maintenance.py — retention + VACUUM safety net.

Asserts:
- run_autonomy_decisions_retention drops rows older than the configured
  window and leaves recent rows intact.
- run_metrics_history_retention behaves the same against metrics_snapshots.
- Both fail-closed (status == "db_missing") when the DB file is absent
  rather than crashing the scheduler.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain_core import db_maintenance


def _iso(offset_days: int) -> str:
    """Return an ISO UTC timestamp `offset_days` from now (negative = past)."""
    return (datetime.now(UTC) + timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@pytest.fixture()
def autonomy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE autonomy_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                kind TEXT NOT NULL,
                level TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                reason TEXT NOT NULL,
                breaker_state TEXT NOT NULL,
                context_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO autonomy_decisions (ts_utc, kind, level, allowed, reason, breaker_state) "
            "VALUES (?, 't', 'L1', 1, 'r', 'closed')",
            [
                (_iso(-365),),  # ancient
                (_iso(-25),),  # 25d old (older than 14d retention)
                (_iso(-1),),  # 1d old (kept)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db_maintenance, "AUTONOMY_DB", db)
    return db


@pytest.fixture()
def metrics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "metrics_history.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE metrics_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO metrics_snapshots (timestamp, payload) VALUES (?, '{}')",
            [
                (_iso(-365),),
                (_iso(-25),),
                (_iso(-1),),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db_maintenance, "METRICS_HISTORY_DB", db)
    return db


def test_autonomy_decisions_retention_drops_older_rows(autonomy_db: Path) -> None:
    summary = db_maintenance.run_autonomy_decisions_retention(days=14)
    assert summary["status"] == "ok"
    assert summary["deleted"] == 2  # ancient + ~25d old
    assert summary["remaining"] == 1
    conn = sqlite3.connect(str(autonomy_db))
    try:
        rows = conn.execute("SELECT ts_utc FROM autonomy_decisions").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0].startswith(datetime.now(UTC).strftime("%Y-%m"))


def test_metrics_history_retention_drops_older_rows(metrics_db: Path) -> None:
    summary = db_maintenance.run_metrics_history_retention(days=14)
    assert summary["status"] == "ok"
    assert summary["deleted"] == 2
    assert summary["remaining"] == 1


def test_autonomy_decisions_retention_db_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db_maintenance, "AUTONOMY_DB", tmp_path / "missing.db")
    summary = db_maintenance.run_autonomy_decisions_retention(days=14)
    assert summary["status"] == "db_missing"
    assert "deleted" not in summary  # no DELETE attempted


def test_metrics_history_retention_db_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db_maintenance, "METRICS_HISTORY_DB", tmp_path / "missing.db")
    summary = db_maintenance.run_metrics_history_retention(days=14)
    assert summary["status"] == "db_missing"


def test_wal_checkpoint_runs_and_reports_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The daily WAL checkpoint must succeed on a WAL-mode DB.

    SQLite auto-checkpoints aggressively (default wal_autocheckpoint=1000)
    so the WAL is rarely large at the moment we observe it in tests. The
    production contract is "the call returns ok and reports a wal_size_after",
    which is enough for the scheduler to log a successful run.
    """

    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_maintenance, "BRAIN_DB", db)
    monkeypatch.setattr(db_maintenance, "AUTONOMY_DB", tmp_path / "missing_autonomy.db")
    monkeypatch.setattr(db_maintenance, "LLM_USAGE_DB", tmp_path / "missing_llm.db")
    monkeypatch.setattr(db_maintenance, "METRICS_HISTORY_DB", tmp_path / "missing_metrics.db")
    monkeypatch.setattr(db_maintenance, "BRAIN_LOGS_DIR", tmp_path)
    monkeypatch.setattr(db_maintenance, "_HOT_DBS", (("brain.db", db),))

    summary = db_maintenance.run_wal_checkpoint()
    assert summary["dbs"][0]["status"] == "ok"
    assert "wal_size_after_mb" in summary["dbs"][0]
    assert db_maintenance.WAL_JOURNAL_SIZE_LIMIT_BYTES > 0


def test_wal_checkpoint_intraday_runs_without_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The intraday checkpoint must truncate WALs but skip the dir snapshot.

    Why no snapshot: the 24h-delta SLO baseline pairs daily snapshots, so
    appending a sample every 4h would compress the pairing window and
    distort the growth-rate signal.
    """

    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_maintenance, "_HOT_DBS", (("brain.db", db),))
    summary = db_maintenance.run_wal_checkpoint_intraday()
    assert summary["dbs"][0]["status"] == "ok"
    assert "logs_dir_snapshot" not in summary


def test_record_logs_dir_snapshot_skips_zero_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A zero-mb measurement must NOT be appended — it would poison the
    24h-delta baseline by giving the SLO a fake floor of 0."""
    monkeypatch.setattr(db_maintenance, "_logs_dir_total_mb", lambda: 0.0)
    captured: dict[str, str] = {}

    class _FakeStore:
        def get(self, _key: str) -> str:
            return "[]"

        def set(self, key: str, value: str, **_) -> None:
            captured[key] = value

    sys_modules_patch = {"brain_config_store": _FakeStore()}
    monkeypatch.setitem(__import__("sys").modules, "brain_config_store", _FakeStore())
    summary = db_maintenance.record_logs_dir_size_snapshot()
    assert summary["status"] == "skipped:zero_mb_measurement"
    assert "brain_config_store" not in captured  # no write attempted
    _ = sys_modules_patch  # silence unused warning


def test_record_logs_dir_snapshot_prunes_prior_zero_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stale mb=0 history rows from earlier bugs must be pruned on the
    next successful snapshot so the SLO baseline picks a real measurement."""
    state: dict[str, str] = {
        "slo.logs_dir_history": '[{"ts":"2026-05-12T05:00:00Z","mb":0.0},'
        '{"ts":"2026-05-12T11:55:00Z","mb":1701.9}]'
    }

    class _FakeStore:
        def get(self, key: str) -> str | None:
            return state.get(key)

        def set(self, key: str, value: str, **_) -> None:
            state[key] = value

    monkeypatch.setattr(db_maintenance, "_logs_dir_total_mb", lambda: 2050.0)
    monkeypatch.setitem(__import__("sys").modules, "brain_config_store", _FakeStore())
    summary = db_maintenance.record_logs_dir_size_snapshot()
    assert summary["status"] == "ok"
    assert summary["mb"] == 2050.0
    import json as _json

    pruned = _json.loads(state["slo.logs_dir_history"])
    assert all(entry["mb"] > 0 for entry in pruned)
    assert {entry["mb"] for entry in pruned} == {1701.9, 2050.0}


def test_raw_events_retention_keeps_referenced_and_protected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """raw_events retention contract: drop old unreferenced rows, keep
    rows that are (a) referenced by an atom, (b) protected by source_type,
    or (c) still inside the retention window.
    """

    db = tmp_path / "brain.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE raw_events (id TEXT PRIMARY KEY, source_type TEXT NOT NULL, "
            "content TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE atoms (id TEXT PRIMARY KEY, raw_event_id TEXT)")
        old = _iso(-30)
        recent = _iso(-1)
        rows = [
            ("re_old_unref", "openclaw_session", old),
            ("re_old_ref", "openclaw_session", old),
            ("re_old_protected_coding", "coding_event", old),
            ("re_old_protected_hotpath", "atoms_hot_path", old),
            ("re_recent_unref", "openclaw_session", recent),
        ]
        conn.executemany(
            "INSERT INTO raw_events (id, source_type, created_at) VALUES (?, ?, ?)",
            rows,
        )
        conn.execute("INSERT INTO atoms (id, raw_event_id) VALUES ('atm_1', 're_old_ref')")

    monkeypatch.setattr(db_maintenance, "BRAIN_DB", db)
    summary = db_maintenance.run_raw_events_retention(days=14)
    assert summary["status"] == "ok"
    assert summary["deleted"] == 1
    assert summary["remaining"] == 4

    with sqlite3.connect(db) as conn:
        kept = {row[0] for row in conn.execute("SELECT id FROM raw_events").fetchall()}
    assert "re_old_unref" not in kept
    assert {"re_old_ref", "re_old_protected_coding", "re_old_protected_hotpath", "re_recent_unref"} == kept


def test_apply_hot_db_pragmas_sets_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The shared PRAGMA helper must set journal_size_limit on a live connection.

    Long-lived brain-server connections call this helper at open time so the
    96 MiB ceiling applies during the day, not only during the daily checkpoint.
    """

    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        db_maintenance.apply_hot_db_pragmas(conn)
        limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    finally:
        conn.close()
    assert limit == db_maintenance.WAL_JOURNAL_SIZE_LIMIT_BYTES


# ── obsolete_expired_atoms ────────────────────────────────────────────


@pytest.fixture()
def atoms_db_with_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A brain.db with three atoms exercising every branch of the
    obsolete-expired-atoms job:
      atm_safe   — eligible: superseded + 90d expired + reinforce=0
      atm_recent — ineligible: only 5d expired
      atm_used   — ineligible: superseded + 90d expired but reinforce=2
      atm_no_chain — ineligible: 90d expired but no superseded_by
    """
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'episodic',
                superseded_by TEXT,
                valid_until TEXT,
                reinforcement_count INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'fact',
                text TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '2026-01-01'
            )
            """
        )
        conn.executemany(
            "INSERT INTO atoms (id, tier, superseded_by, valid_until, reinforcement_count, kind, text) VALUES (?, ?, ?, ?, ?, 'fact', ?)",
            [
                ("atm_safe", "episodic", "atm_new", "2026-01-01T00:00:00Z", 0, "expired+chain+unused"),
                ("atm_recent", "episodic", "atm_new", "2026-04-25T00:00:00Z", 0, "5d expired"),
                ("atm_used", "episodic", "atm_new", "2026-01-01T00:00:00Z", 2, "expired but accessed"),
                ("atm_no_chain", "episodic", None, "2026-01-01T00:00:00Z", 0, "expired no chain"),
                ("atm_new", "episodic", None, None, 0, "the active replacement"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db_maintenance, "BRAIN_DB", db)
    return db


def test_obsolete_expired_atoms_only_targets_eligible(atoms_db_with_expired: Path) -> None:
    summary = db_maintenance.run_obsolete_expired_atoms(days=60)
    assert summary["status"] == "ok"
    assert summary["obsoleted"] == ["atm_safe"]
    conn = sqlite3.connect(str(atoms_db_with_expired))
    try:
        rows = dict(conn.execute("SELECT id, tier FROM atoms").fetchall())
    finally:
        conn.close()
    assert rows["atm_safe"] == "obsolete"
    assert rows["atm_recent"] == "episodic"
    assert rows["atm_used"] == "episodic"
    assert rows["atm_no_chain"] == "episodic"
    assert rows["atm_new"] == "episodic"


def test_obsolete_expired_atoms_db_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db_maintenance, "BRAIN_DB", tmp_path / "missing.db")
    summary = db_maintenance.run_obsolete_expired_atoms(days=60)
    assert summary["status"] == "db_missing"
