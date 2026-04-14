"""brain_core/atoms_store.py — SQLite truth layer for knowledge units (Phase 3).

Schema lives in brain.db (a new isolated SQLite file). Tables:
  raw_events    — append-only landing for ingested records (UNIQUE content_hash)
  atoms         — distilled knowledge units with SM-2 state and tier
  entities      — SQL mirror of Neo4j entity nodes for hot-path joins
  atom_entity   — M:N atom ↔ entity
  provenance    — relation tree (parent/child/relation)
  action_audit  — retrieved-atom traceability for closed-loop feedback

All writes are best-effort during dual-write: failures here NEVER fail the
primary Chroma upsert. Reads are gated by BRAIN_ATOMS_READ (Phase 6 cutover).

Master kill switch: BRAIN_ATOMS_ENABLED env var. When false, every public
function short-circuits as a no-op and returns a sentinel. This makes the
shim safe to land before the migration runs.

The schema_versions migration runner registers `brain_db` component (versions
0→1→2→3) gated by BRAIN_ENABLE_ATOMS_MIGRATION.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_ATOMS_ENABLED, BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    BRAIN_ATOMS_ENABLED = False


_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS raw_events (
  id              TEXT PRIMARY KEY,
  content_hash    TEXT NOT NULL UNIQUE,
  timestamp       TEXT NOT NULL,
  source_type     TEXT NOT NULL,
  source_ref      TEXT NOT NULL DEFAULT '',
  actor           TEXT NOT NULL DEFAULT 'unknown',
  visibility      TEXT NOT NULL DEFAULT 'private',
  scrub_status    TEXT NOT NULL DEFAULT 'scrubbed',
  content         TEXT NOT NULL,
  attachments_json TEXT NOT NULL DEFAULT '[]',
  entities_json   TEXT NOT NULL DEFAULT '[]',
  json_path       TEXT,
  created_at      TEXT NOT NULL,
  processed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_events_ts     ON raw_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_raw_events_src    ON raw_events(source_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_raw_events_unproc ON raw_events(processed_at) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS atoms (
  id                  TEXT PRIMARY KEY,
  text                TEXT NOT NULL,
  kind                TEXT NOT NULL DEFAULT 'fact',
  confidence          REAL NOT NULL DEFAULT 0.5,
  tier                TEXT NOT NULL DEFAULT 'episodic',
  canonical           INTEGER NOT NULL DEFAULT 0,
  version_of          TEXT,
  supersedes          TEXT,
  superseded_by       TEXT,
  distilled_by        TEXT,
  quality_score       REAL,
  raw_event_id        TEXT REFERENCES raw_events(id),
  chroma_id           TEXT NOT NULL UNIQUE,
  collection_hint     TEXT NOT NULL DEFAULT 'semantic_memory',
  simhash             INTEGER,
  easiness_factor     REAL NOT NULL DEFAULT 2.5,
  interval_days       REAL NOT NULL DEFAULT 0,
  reinforcement_count INTEGER NOT NULL DEFAULT 0,
  last_reviewed_at    TEXT,
  next_review_at      TEXT,
  decay_weight        REAL NOT NULL DEFAULT 1.0,
  valid_from          TEXT NOT NULL,
  valid_until         TEXT,
  provenance_json     TEXT NOT NULL DEFAULT '{}',
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atoms_kind        ON atoms(kind);
CREATE INDEX IF NOT EXISTS idx_atoms_tier        ON atoms(tier) WHERE tier != 'obsolete';
CREATE INDEX IF NOT EXISTS idx_atoms_review_due  ON atoms(next_review_at) WHERE tier != 'obsolete' AND next_review_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_atoms_raw_event   ON atoms(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_atoms_version_of  ON atoms(version_of);
CREATE INDEX IF NOT EXISTS idx_atoms_canonical   ON atoms(canonical) WHERE canonical = 1;
CREATE INDEX IF NOT EXISTS idx_atoms_simhash     ON atoms(simhash) WHERE simhash IS NOT NULL;

CREATE TABLE IF NOT EXISTS entities (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  entity_type     TEXT NOT NULL DEFAULT 'concept',
  first_seen_at   TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  mention_count   INTEGER NOT NULL DEFAULT 1,
  neo4j_synced_at TEXT,
  UNIQUE(name, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS atom_entity (
  atom_id   TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  role      TEXT NOT NULL DEFAULT 'subject',
  PRIMARY KEY (atom_id, entity_id, role)
);
CREATE INDEX IF NOT EXISTS idx_atom_entity_entity ON atom_entity(entity_id);

CREATE TABLE IF NOT EXISTS provenance (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_kind  TEXT NOT NULL,
  parent_id    TEXT NOT NULL,
  child_kind   TEXT NOT NULL,
  child_id     TEXT NOT NULL,
  relation     TEXT NOT NULL,
  confidence   REAL,
  created_at   TEXT NOT NULL,
  UNIQUE(parent_kind, parent_id, child_kind, child_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_prov_parent ON provenance(parent_kind, parent_id);
CREATE INDEX IF NOT EXISTS idx_prov_child  ON provenance(child_kind,  child_id);

CREATE TABLE IF NOT EXISTS action_audit (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  route               TEXT NOT NULL,
  tool                TEXT NOT NULL DEFAULT '',
  actor               TEXT NOT NULL DEFAULT 'unknown',
  query_text          TEXT,
  retrieved_atom_ids  TEXT NOT NULL DEFAULT '[]',
  retrieved_chroma_ids TEXT,
  outcome             TEXT,
  outcome_reason      TEXT,
  session_id          TEXT,
  created_at          TEXT NOT NULL,
  resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_audit_session ON action_audit(session_id);
CREATE INDEX IF NOT EXISTS idx_action_audit_pending ON action_audit(outcome) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS idx_action_audit_actor_ts ON action_audit(actor, created_at);
CREATE INDEX IF NOT EXISTS idx_action_audit_tool_ts  ON action_audit(tool,  created_at);
"""


_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def init_schema(db_path: Path | None = None) -> None:
    """Create brain.db if missing and apply DDL. Idempotent (uses IF NOT EXISTS)."""
    global _initialized
    target = db_path or BRAIN_DB
    with _init_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target))
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()
        if target == BRAIN_DB:
            _initialized = True


@contextmanager
def _conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Short-lived connection. Opens on demand, closes on exit.

    Always enables `PRAGMA foreign_keys=ON` — this pragma is per-connection,
    so setting it only once in the DDL (via executescript at init) is a
    SQLite footgun. Without it the FK REFERENCES in the DDL are silently
    ignored and orphan rows become possible.
    """
    target = db_path or BRAIN_DB
    if not _initialized and target == BRAIN_DB:
        init_schema(target)
    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def derive_atom_id(content: str) -> str:
    """Deterministic atom id from content sha256.

    Takes the text content (not chroma_id) so that content-identical atoms
    naturally collide at the id level and UPSERT updates the existing row
    instead of creating a parallel atom. 48-bit hash prefix is sufficient
    for the expected O(10^4) atoms corpus.
    """
    return f"atm_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}"


def derive_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def insert_raw_event(
    *,
    event_id: str,
    timestamp: str,
    source_type: str,
    content: str,
    source_ref: str = "",
    actor: str = "unknown",
    visibility: str = "private",
    scrub_status: str = "scrubbed",
    attachments: list | None = None,
    entities: list | None = None,
    json_path: str | None = None,
    db_path: Path | None = None,
) -> str | None:
    """Insert a raw_events row. Returns the row id or None if disabled / dedup hit."""
    if not BRAIN_ATOMS_ENABLED:
        return None
    if not content:
        return None
    content_hash = derive_content_hash(content)
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_events "
                "(id, content_hash, timestamp, source_type, source_ref, actor, "
                " visibility, scrub_status, content, attachments_json, entities_json, "
                " json_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    content_hash,
                    timestamp,
                    source_type,
                    source_ref,
                    actor,
                    visibility,
                    scrub_status,
                    content,
                    json.dumps(attachments or []),
                    json.dumps(entities or []),
                    json_path,
                    _now(),
                ),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            return event_id
    except sqlite3.Error:
        return None


def upsert_atom(
    *,
    text: str,
    chroma_id: str,
    kind: str = "fact",
    confidence: float = 0.5,
    tier: str = "episodic",
    canonical: bool = False,
    version_of: str | None = None,
    distilled_by: str = "manual",
    raw_event_id: str | None = None,
    collection_hint: str = "semantic_memory",
    quality_score: float | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    provenance: dict | None = None,
    db_path: Path | None = None,
) -> str | None:
    """Insert or update an atom row. Returns atom id or None if disabled / failed."""
    if not BRAIN_ATOMS_ENABLED:
        return None
    if not text or not chroma_id:
        return None
    # Historic deploy note (2026-04-13): derivation is keyed on chroma_id,
    # not text, because the deployed corpus was backfilled that way. Two
    # atoms with identical text but different chroma_ids still coexist;
    # dedup happens at the chroma_id UNIQUE constraint instead. Changing
    # this without a migration would break ON CONFLICT(id) for every
    # existing row. Tracked for a Phase K re-backfill.
    atom_id = derive_atom_id(chroma_id)
    now = _now()
    valid_from = valid_from or now
    try:
        with _conn(db_path) as conn:
            conn.execute(
                "INSERT INTO atoms "
                "(id, text, kind, confidence, tier, canonical, version_of, "
                " distilled_by, quality_score, raw_event_id, chroma_id, "
                " collection_hint, valid_from, valid_until, provenance_json, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  text=excluded.text, "
                "  kind=excluded.kind, "
                "  confidence=excluded.confidence, "
                "  tier=excluded.tier, "
                "  canonical=excluded.canonical, "
                "  version_of=excluded.version_of, "
                "  distilled_by=excluded.distilled_by, "
                "  quality_score=excluded.quality_score, "
                "  raw_event_id=COALESCE(excluded.raw_event_id, atoms.raw_event_id), "
                "  collection_hint=excluded.collection_hint, "
                "  valid_from=excluded.valid_from, "
                "  valid_until=excluded.valid_until, "
                "  provenance_json=excluded.provenance_json, "
                "  updated_at=excluded.updated_at",
                (
                    atom_id,
                    text,
                    kind,
                    confidence,
                    tier,
                    1 if canonical else 0,
                    version_of,
                    distilled_by,
                    quality_score,
                    raw_event_id,
                    chroma_id,
                    collection_hint,
                    valid_from,
                    valid_until,
                    json.dumps(provenance or {}),
                    now,
                    now,
                ),
            )
            conn.commit()
            return atom_id
    except sqlite3.Error as exc:
        # Emit an audit event so SLO atoms_write_fail_rate_1h is observable.
        # Best-effort — never raise from the audit path.
        import contextlib

        with contextlib.suppress(Exception):
            from audit_log import log_event

            log_event(
                "atoms_write_fail",
                entity_a=chroma_id,
                reason=str(exc)[:200],
                source_evidence={"kind": kind, "tier": tier},
            )
        return None


def mark_superseded(parent_chroma_id: str, child_chroma_id: str, *, db_path: Path | None = None) -> bool:
    """Mark parent atom as superseded_by child. Best-effort.

    Uses BEGIN IMMEDIATE to serialize concurrent supersession calls on the
    same parent — without it, two concurrent writers each read the parent
    row, both compute identical state, and the last commit silently
    overwrites the first (last-writer-wins data loss).
    """
    if not BRAIN_ATOMS_ENABLED:
        return False
    try:
        with _conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent_row = conn.execute(
                "SELECT id FROM atoms WHERE chroma_id = ?",
                (parent_chroma_id,),
            ).fetchone()
            child_row = conn.execute(
                "SELECT id FROM atoms WHERE chroma_id = ?",
                (child_chroma_id,),
            ).fetchone()
            if not parent_row or not child_row:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE atoms SET superseded_by = ?, updated_at = ? WHERE id = ?",
                (child_row["id"], _now(), parent_row["id"]),
            )
            conn.execute(
                "UPDATE atoms SET supersedes = ?, updated_at = ? WHERE id = ?",
                (parent_row["id"], _now(), child_row["id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO provenance "
                "(parent_kind, parent_id, child_kind, child_id, relation, created_at) "
                "VALUES ('atom', ?, 'atom', ?, 'supersedes', ?)",
                (parent_row["id"], child_row["id"], _now()),
            )
            conn.commit()
            return True
    except sqlite3.Error:
        return False


def reinforce(chroma_id: str, *, success: bool = True, db_path: Path | None = None) -> dict | None:
    """Bump reinforcement_count + apply SM-2 schedule update. SM-2 lives in sm2.py
    (Phase 4). Until then we just bump the counter so the data is captured.

    Uses BEGIN IMMEDIATE to serialize concurrent reinforces on the same
    atom — the read-modify-write pattern is racy without an explicit
    write lock.
    """
    if not BRAIN_ATOMS_ENABLED:
        return None
    try:
        with _conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, reinforcement_count, easiness_factor, interval_days "
                "FROM atoms WHERE chroma_id = ?",
                (chroma_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            new_count = (row["reinforcement_count"] or 0) + (1 if success else 0)
            conn.execute(
                "UPDATE atoms SET reinforcement_count = ?, last_reviewed_at = ?, updated_at = ? "
                "WHERE id = ?",
                (new_count, _now(), _now(), row["id"]),
            )
            conn.commit()
            return {
                "atom_id": row["id"],
                "reinforcement_count": new_count,
                "easiness_factor": row["easiness_factor"],
                "interval_days": row["interval_days"],
            }
    except sqlite3.Error:
        return None


def get_atom_by_chroma_id(chroma_id: str, *, db_path: Path | None = None) -> dict | None:
    if not BRAIN_ATOMS_ENABLED:
        return None
    try:
        with _conn(db_path) as conn:
            row = conn.execute("SELECT * FROM atoms WHERE chroma_id = ?", (chroma_id,)).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        return None


def count_atoms(*, db_path: Path | None = None) -> dict[str, int]:
    """Aggregate counts for /brain/health introspection."""
    if not BRAIN_ATOMS_ENABLED:
        return {"enabled": 0}
    try:
        with _conn(db_path) as conn:
            atoms_total = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
            canonical = conn.execute("SELECT COUNT(*) FROM atoms WHERE canonical=1").fetchone()[0]
            episodic = conn.execute("SELECT COUNT(*) FROM atoms WHERE tier='episodic'").fetchone()[0]
            semantic = conn.execute("SELECT COUNT(*) FROM atoms WHERE tier='semantic'").fetchone()[0]
            core = conn.execute("SELECT COUNT(*) FROM atoms WHERE tier='core'").fetchone()[0]
            obsolete = conn.execute("SELECT COUNT(*) FROM atoms WHERE tier='obsolete'").fetchone()[0]
            raw_events = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
            return {
                "enabled": 1,
                "atoms_total": atoms_total,
                "canonical": canonical,
                "episodic": episodic,
                "semantic": semantic,
                "core": core,
                "obsolete": obsolete,
                "raw_events": raw_events,
            }
    except sqlite3.Error:
        return {"enabled": 1, "error": "sqlite_error"}


def insert_action_audit(
    *,
    route: str,
    query_text: str | None,
    tool: str | None = None,
    actor: str | None = None,
    retrieved_atom_ids: list[str] | None = None,
    retrieved_chroma_ids: list[str] | None = None,
    session_id: str | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Record a retrieval event for closed-loop feedback. Best-effort.

    `tool` is the logical tool name (e.g. 'brain_recall'); `route` is the raw
    HTTP path. `actor` is the originating agent (jenna, liz, ellie, sage,
    market, claude-code, mcp, unknown).
    """
    if not BRAIN_ATOMS_ENABLED:
        return None
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO action_audit "
                "(route, tool, actor, query_text, retrieved_atom_ids, "
                " retrieved_chroma_ids, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    route,
                    tool or route.split("?")[0],
                    actor or "unknown",
                    query_text,
                    json.dumps(retrieved_atom_ids or []),
                    json.dumps(retrieved_chroma_ids or []) if retrieved_chroma_ids else None,
                    session_id,
                    _now(),
                ),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.Error:
        return None


def action_audit_usage(
    since_days: int = 7,
    db_path: Path | None = None,
) -> dict:
    """Return per-actor and per-tool usage counts over the last N days.

    Returns {
      "window_days": 7,
      "total": int,
      "by_actor":  [{"actor": ..., "count": int}, ...],
      "by_tool":   [{"tool": ..., "count": int}, ...],
      "by_actor_tool": [{"actor": ..., "tool": ..., "count": int}, ...],
    }
    """
    if not BRAIN_ATOMS_ENABLED:
        return {"enabled": 0}
    try:
        with _conn(db_path) as conn:
            cutoff = datetime.now(UTC).timestamp() - since_days * 86400
            cutoff_iso = datetime.fromtimestamp(cutoff, UTC).isoformat(timespec="seconds")
            total = conn.execute(
                "SELECT COUNT(*) FROM action_audit WHERE created_at >= ?",
                (cutoff_iso,),
            ).fetchone()[0]
            by_actor = [
                {"actor": r[0], "count": r[1]}
                for r in conn.execute(
                    "SELECT actor, COUNT(*) FROM action_audit "
                    "WHERE created_at >= ? GROUP BY actor ORDER BY 2 DESC",
                    (cutoff_iso,),
                ).fetchall()
            ]
            by_tool = [
                {"tool": r[0], "count": r[1]}
                for r in conn.execute(
                    "SELECT tool, COUNT(*) FROM action_audit "
                    "WHERE created_at >= ? GROUP BY tool ORDER BY 2 DESC",
                    (cutoff_iso,),
                ).fetchall()
            ]
            by_actor_tool = [
                {"actor": r[0], "tool": r[1], "count": r[2]}
                for r in conn.execute(
                    "SELECT actor, tool, COUNT(*) FROM action_audit "
                    "WHERE created_at >= ? GROUP BY actor, tool ORDER BY 3 DESC",
                    (cutoff_iso,),
                ).fetchall()
            ]
            return {
                "enabled": 1,
                "window_days": since_days,
                "total": total,
                "by_actor": by_actor,
                "by_tool": by_tool,
                "by_actor_tool": by_actor_tool,
            }
    except sqlite3.Error as e:
        return {"enabled": 1, "error": f"sqlite_error:{e}"}
