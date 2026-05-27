from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _seed_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE atoms (
                id                  TEXT PRIMARY KEY,
                text                TEXT NOT NULL,
                kind                TEXT NOT NULL DEFAULT 'fact',
                confidence          REAL NOT NULL DEFAULT 0.5,
                tier                TEXT NOT NULL DEFAULT 'episodic',
                canonical           INTEGER NOT NULL DEFAULT 0,
                chroma_id           TEXT NOT NULL UNIQUE,
                collection_hint     TEXT NOT NULL DEFAULT 'semantic_memory',
                easiness_factor     REAL NOT NULL DEFAULT 2.5,
                interval_days       REAL NOT NULL DEFAULT 0,
                reinforcement_count INTEGER NOT NULL DEFAULT 0,
                decay_weight        REAL NOT NULL DEFAULT 1.0,
                valid_from          TEXT NOT NULL,
                valid_until         TEXT,
                provenance_json     TEXT NOT NULL DEFAULT '{}',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                provisional         INTEGER NOT NULL DEFAULT 0,
                trust_score         REAL NOT NULL DEFAULT 0.5,
                speaker_entity      TEXT NOT NULL DEFAULT 'chris',
                scope               TEXT NOT NULL DEFAULT 'global'
            );
            CREATE TABLE atom_evidence (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                atom_id       TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                weight        REAL NOT NULL,
                evidence_ref  TEXT,
                cluster_size  INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_atom(
    db_path: Path,
    *,
    atom_id: str,
    text: str,
    kind: str = "fact",
    tier: str = "semantic",
    confidence: float = 0.7,
    valid_from: str,
    provenance: dict | None = None,
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO atoms (id, text, kind, confidence, tier, chroma_id, valid_from, "
            "provenance_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                atom_id,
                text,
                kind,
                confidence,
                tier,
                f"chroma:{atom_id}",
                valid_from,
                json.dumps(provenance or {}),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _reload_module(tmp_path: Path, monkeypatch):
    for mod in [m for m in list(sys.modules) if m == "conjecture_validator"]:
        del sys.modules[mod]
    import conjecture_validator as cv

    monkeypatch.setattr(cv, "BRAIN_DB", tmp_path / "brain.db")
    monkeypatch.setattr(cv, "AUDIT_LOG", tmp_path / "conjecture_validator.jsonl")
    return cv


def test_conjecture_with_evidence_gets_promoted(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _seed_schema(db)
    long_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
    today = datetime.now(UTC).isoformat(timespec="seconds")

    _insert_atom(
        db,
        atom_id="atm_conj_1",
        text="Dream conjecture (entity_alpha x entity_beta):\nNovel hypothesis text linking them.",
        kind="conjecture",
        tier="episodic",
        confidence=0.3,
        valid_from=long_ago,
        provenance={"origin": "dream_replay", "entity_a": "entity_alpha", "entity_b": "entity_beta"},
    )
    for i, body in enumerate(
        [
            "Working with entity_alpha and entity_beta we noticed a real link.",
            "entity_beta is increasingly relevant to entity_alpha pipelines.",
            "entity_alpha logging suggests integration with entity_beta is needed.",
            "After deploying entity_alpha, entity_beta consumers became stable.",
            "entity_alpha + entity_beta migration completed without issues.",
            "Test coverage now spans entity_alpha to entity_beta boundary.",
        ]
    ):
        _insert_atom(
            db,
            atom_id=f"atm_supp_{i}",
            text=body,
            valid_from=today,
        )

    cv = _reload_module(tmp_path, monkeypatch)
    result = cv.run()

    assert result["status"] == "ok"
    assert result["scanned"] == 1
    assert result["new_supports"] == 6
    assert result["promoted_count"] == 1
    assert result["promoted"][0]["id"] == "atm_conj_1"

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT confidence, tier FROM atoms WHERE id = 'atm_conj_1'").fetchone()
        assert row[1] == "semantic"
        assert row[0] >= 0.55
    finally:
        conn.close()


def test_validator_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _seed_schema(db)
    long_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat(timespec="seconds")
    today = datetime.now(UTC).isoformat(timespec="seconds")

    _insert_atom(
        db,
        atom_id="atm_conj_2",
        text="Dream conjecture (alpha_proj x beta_proj):\nalpha_proj and beta_proj might combine well.",
        kind="conjecture",
        tier="episodic",
        confidence=0.3,
        valid_from=long_ago,
        provenance={"origin": "dream_replay", "entity_a": "alpha_proj", "entity_b": "beta_proj"},
    )
    _insert_atom(
        db,
        atom_id="atm_supp_x",
        text="A single mention of alpha_proj and beta_proj in the same context.",
        valid_from=today,
    )

    cv = _reload_module(tmp_path, monkeypatch)
    first = cv.run()
    second = cv.run()

    assert first["new_supports"] == 1
    assert second["new_supports"] == 0


def test_barren_conjecture_expires_after_21d(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _seed_schema(db)
    ancient = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")

    _insert_atom(
        db,
        atom_id="atm_conj_old",
        text="Dream conjecture (lonely x abandoned):\nNo evidence ever appeared.",
        kind="conjecture",
        tier="episodic",
        confidence=0.3,
        valid_from=ancient,
        provenance={"origin": "dream_replay", "entity_a": "lonely", "entity_b": "abandoned"},
    )

    cv = _reload_module(tmp_path, monkeypatch)
    result = cv.run()

    assert result["expired_count"] == 1
    conn = sqlite3.connect(str(db))
    try:
        tier = conn.execute("SELECT tier FROM atoms WHERE id = 'atm_conj_old'").fetchone()[0]
        assert tier == "obsolete"
    finally:
        conn.close()


def test_soft_cap_archives_oldest_unsupported_excess(tmp_path, monkeypatch):
    """P3-9: when unsupported episodic conjectures exceed SOFT_CAP_UNSUPPORTED,
    the oldest excess must be moved to tier=obsolete with
    expire_reason='soft_cap_exceeded'. Conjectures with at least one
    supporter row never count toward the cap and are not touched.
    """
    db = tmp_path / "brain.db"
    _seed_schema(db)

    cv = _reload_module(tmp_path, monkeypatch)
    # Tighten cap for the test so we don't have to seed dozens of rows.
    monkeypatch.setattr(cv, "SOFT_CAP_UNSUPPORTED", 3)

    # Five recent unsupported conjectures (so none expire via the 21d TTL),
    # plus one supported one that must NOT be archived.
    now = datetime.now(UTC)
    for i in range(5):
        _insert_atom(
            db,
            atom_id=f"atm_conj_unsupported_{i}",
            text=f"Dream conjecture (alpha_{i} x beta_{i}):\nUnsupported guess #{i}.",
            kind="conjecture",
            tier="episodic",
            confidence=0.3,
            valid_from=(now - timedelta(days=10 - i)).isoformat(timespec="seconds"),
            provenance={"origin": "dream_replay", "entity_a": f"alpha_{i}", "entity_b": f"beta_{i}"},
        )

    _insert_atom(
        db,
        atom_id="atm_conj_supported",
        text="Dream conjecture (gamma_x x delta_y):\nThis one will have evidence.",
        kind="conjecture",
        tier="episodic",
        confidence=0.3,
        valid_from=(now - timedelta(days=15)).isoformat(timespec="seconds"),
        provenance={"origin": "dream_replay", "entity_a": "gamma_x", "entity_b": "delta_y"},
    )
    # Manually insert a supporter row so this conjecture is "supported".
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO atom_evidence (atom_id, event_type, weight, evidence_ref, created_at) "
            "VALUES (?, 'conjecture_support', 1.0, 'atm_evidence', ?)",
            ("atm_conj_supported", now.isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()

    result = cv.run()

    # 5 unsupported - 3 cap = 2 archived. Supported one untouched.
    assert result["cap_expired_count"] == 2
    archived_ids = {e["id"] for e in result["expired"] if e.get("expire_reason") == "soft_cap_exceeded"}
    # Oldest two (i=0 → 10d ago, i=1 → 9d ago) should be the targets.
    assert archived_ids == {"atm_conj_unsupported_0", "atm_conj_unsupported_1"}

    conn = sqlite3.connect(str(db))
    try:
        # Newer unsupported survive; supported one untouched.
        for keeper in (
            "atm_conj_unsupported_2",
            "atm_conj_unsupported_3",
            "atm_conj_unsupported_4",
            "atm_conj_supported",
        ):
            tier = conn.execute("SELECT tier FROM atoms WHERE id = ?", (keeper,)).fetchone()[0]
            assert tier == "episodic", f"{keeper} should remain episodic"
        for archived in archived_ids:
            tier = conn.execute("SELECT tier FROM atoms WHERE id = ?", (archived,)).fetchone()[0]
            assert tier == "obsolete"
    finally:
        conn.close()


def test_soft_cap_does_not_archive_non_dream_replay_conjectures(tmp_path, monkeypatch):
    """Codex review: the soft-cap query previously scanned ALL unsupported
    episodic conjectures, which would archive manually-authored or
    other-origin conjectures as collateral damage. Scope it to
    `provenance_json.origin == 'dream_replay'` only.
    """
    db = tmp_path / "brain.db"
    _seed_schema(db)

    cv = _reload_module(tmp_path, monkeypatch)
    monkeypatch.setattr(cv, "SOFT_CAP_UNSUPPORTED", 2)

    now = datetime.now(UTC)

    # 4 dream-replay conjectures (over the cap of 2).
    for i in range(4):
        _insert_atom(
            db,
            atom_id=f"dream_{i}",
            text=f"Dream conjecture (alpha_{i} x beta_{i}):\n#{i}",
            kind="conjecture",
            tier="episodic",
            confidence=0.3,
            valid_from=(now - timedelta(days=10 - i)).isoformat(timespec="seconds"),
            provenance={"origin": "dream_replay", "entity_a": f"alpha_{i}", "entity_b": f"beta_{i}"},
        )

    # 1 manually authored conjecture — older than any dream one, no
    # supporters. MUST NOT be archived even though it's the oldest unsupported.
    _insert_atom(
        db,
        atom_id="manual_old",
        text="Manually authored hypothesis we want to keep.",
        kind="conjecture",
        tier="episodic",
        confidence=0.3,
        valid_from=(now - timedelta(days=30)).isoformat(timespec="seconds"),
        provenance={"origin": "manual", "entity_a": "manual_alpha", "entity_b": "manual_beta"},
    )

    cv.run()

    conn = sqlite3.connect(str(db))
    try:
        manual_tier = conn.execute("SELECT tier FROM atoms WHERE id = 'manual_old'").fetchone()[0]
        assert manual_tier == "episodic", "non-dream conjecture must not be archived by soft-cap"
        # And dream-replay conjectures over the cap ARE archived.
        archived = conn.execute("SELECT id FROM atoms WHERE id LIKE 'dream_%' AND tier='obsolete'").fetchall()
        assert len(archived) == 2, "dream-replay excess should still be archived"
    finally:
        conn.close()


def test_soft_cap_noop_below_threshold(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    _seed_schema(db)

    cv = _reload_module(tmp_path, monkeypatch)
    monkeypatch.setattr(cv, "SOFT_CAP_UNSUPPORTED", 100)
    now = datetime.now(UTC)
    for i in range(3):
        _insert_atom(
            db,
            atom_id=f"atm_below_cap_{i}",
            text=f"Dream conjecture (a_{i} x b_{i}):\nBelow cap.",
            kind="conjecture",
            tier="episodic",
            confidence=0.3,
            valid_from=(now - timedelta(days=2)).isoformat(timespec="seconds"),
            provenance={"origin": "dream_replay", "entity_a": f"a_{i}", "entity_b": f"b_{i}"},
        )
    result = cv.run()
    assert result["cap_expired_count"] == 0
