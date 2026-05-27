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

import concurrent.futures
import hashlib
import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.atoms_store")

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
  parent_atom_id      TEXT,
  provisional         INTEGER NOT NULL DEFAULT 0,
  trust_score         REAL NOT NULL DEFAULT 0.5,
  topic_key           TEXT,
  speaker_entity      TEXT NOT NULL DEFAULT 'chris',
  scope               TEXT NOT NULL DEFAULT 'global',
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
CREATE INDEX IF NOT EXISTS idx_atoms_parent_id   ON atoms(parent_atom_id) WHERE parent_atom_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_atoms_provisional ON atoms(provisional) WHERE provisional = 1;
CREATE INDEX IF NOT EXISTS idx_atoms_topic_key   ON atoms(topic_key) WHERE topic_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_atoms_speaker_scope ON atoms(speaker_entity, scope);

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

-- Phase N2 (brain_db@7): mutable Bayesian confidence ledger. One row per
-- observation that shifted an atom's confidence. Append-only + reversible
-- (ROME principle). cluster_size is the Kuhn semantic-uncertainty normalizer.
CREATE TABLE IF NOT EXISTS atom_evidence (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  atom_id       TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  event_type    TEXT NOT NULL,
  weight        REAL NOT NULL,
  evidence_ref  TEXT,
  cluster_size  INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atom_evidence_atom    ON atom_evidence(atom_id);
CREATE INDEX IF NOT EXISTS idx_atom_evidence_type_ts ON atom_evidence(event_type, created_at);

-- Phase N4 (brain_db@8): CLS sleep consolidation co-activation matrix.
-- Sparse top-K per atom, rebuilt daily by sleep_consolidate job. Ordered
-- pair invariant (atom_a_id < atom_b_id) so each edge has one row.
CREATE TABLE IF NOT EXISTS atom_coactivation (
  atom_a_id    TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  atom_b_id    TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  n_events     INTEGER NOT NULL DEFAULT 1,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (atom_a_id, atom_b_id),
  CHECK (atom_a_id < atom_b_id)
);
CREATE INDEX IF NOT EXISTS idx_coact_b ON atom_coactivation(atom_b_id);

-- Phase N4 (brain_db@8): sleep cycle log. One row per sleep_consolidate run
-- so we can post-hoc audit what happened during each "night's consolidation".
CREATE TABLE IF NOT EXISTS sleep_cycles (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  replay_count INTEGER NOT NULL DEFAULT 0,
  edges_added  INTEGER NOT NULL DEFAULT 0,
  consolidated INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sleep_cycles_started ON sleep_cycles(started_at);

-- Phase N3 (brain_db@9): eval holdout lifecycle tracker. Candidates stay in
-- eval_holdout_pending.json for N weeks of nightly auto-evaluation before
-- auto_graduate() merges passing ones into eval_set.json or marks failures.
-- Seals Gap C1 + C2: removes the human Telegram-tap gate and auto-promotes
-- stable eval growth.
CREATE TABLE IF NOT EXISTS eval_holdout_lifecycle (
  candidate_id   TEXT PRIMARY KEY,
  promoted_at    TEXT NOT NULL,
  eval_runs      INTEGER NOT NULL DEFAULT 0,
  eval_passes    INTEGER NOT NULL DEFAULT 0,
  auto_stable_at TEXT,
  rejected_at    TEXT,
  reject_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_lifecycle_promoted ON eval_holdout_lifecycle(promoted_at);
CREATE INDEX IF NOT EXISTS idx_eval_lifecycle_unresolved
  ON eval_holdout_lifecycle(promoted_at)
  WHERE auto_stable_at IS NULL AND rejected_at IS NULL;

-- 2026-04-16 Tier 3 #4 (brain_db@11): retrieval-induced inhibition log
-- (Bjork 1994). Records (winner, loser) atom competitions per query cue
-- so a nightly job can apply small confidence decrements to consistent
-- losers — counters the rich-get-richer spiral where frequently-accessed
-- memories stay dominant whether or not they're the right answer.
CREATE TABLE IF NOT EXISTS retrieval_competition (
  winner_atom_id  TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  loser_atom_id   TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  query_cue_hash  TEXT NOT NULL,
  n_observations  INTEGER NOT NULL DEFAULT 1,
  last_seen_at    TEXT NOT NULL,
  PRIMARY KEY (winner_atom_id, loser_atom_id, query_cue_hash),
  CHECK (winner_atom_id != loser_atom_id)
);
CREATE INDEX IF NOT EXISTS idx_retrcomp_loser     ON retrieval_competition(loser_atom_id);
CREATE INDEX IF NOT EXISTS idx_retrcomp_last_seen ON retrieval_competition(last_seen_at);
"""


_init_lock = threading.Lock()
_initialized = False

# v3 F3 fix (2026-04-14): bounded bg pool for hot-path entity extraction.
# Previously upsert_atom spawned an unbounded daemon Thread per atom, which
# meant a 590-atom backfill burst would pile up 590 threads hammering
# Neo4j+Ollama at once. Now: module-level pool with 4 workers + a semaphore
# that drops new submissions when more than _BG_EXTRACT_INFLIGHT_MAX are
# pending. Dropped extractions get picked up by the nightly entity
# reconciliation cron (F41), so nothing is permanently lost.
_BG_EXTRACT_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_BG_EXTRACT_POOL_LOCK = threading.Lock()
_BG_EXTRACT_WORKERS = max(1, int(os.getenv("BRAIN_ENTITY_EXTRACT_WORKERS", "1")))
_BG_EXTRACT_INFLIGHT_MAX = max(
    _BG_EXTRACT_WORKERS,
    int(os.getenv("BRAIN_ENTITY_EXTRACT_INFLIGHT_MAX", "16")),
)
_BG_EXTRACT_SEM = threading.BoundedSemaphore(_BG_EXTRACT_INFLIGHT_MAX)
_BG_EXTRACT_DROPPED = 0
_BG_EXTRACT_DROPPED_LOCK = threading.Lock()


def _get_bg_extract_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _BG_EXTRACT_POOL
    if _BG_EXTRACT_POOL is None:
        with _BG_EXTRACT_POOL_LOCK:
            if _BG_EXTRACT_POOL is None:
                _BG_EXTRACT_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_BG_EXTRACT_WORKERS,
                    thread_name_prefix="atoms_bg_extract",
                )
    return _BG_EXTRACT_POOL


def _submit_bg_extract(text: str, chroma_id: str) -> bool:
    """Schedule entity extraction on the bounded pool, drop on overflow.

    Returns True if the task was submitted, False if dropped (inflight cap).
    Dropped tasks are enqueued onto llm_backlog for the next drain cycle so
    nothing is permanently lost — the nightly entity_reconcile cron also
    catches these, but the backlog drain picks them up within 30 min of
    the pool having capacity again.
    """
    global _BG_EXTRACT_DROPPED  # needed by both branches below
    if not _BG_EXTRACT_SEM.acquire(blocking=False):
        with _BG_EXTRACT_DROPPED_LOCK:
            _BG_EXTRACT_DROPPED += 1
        # Queue for catch-up
        try:
            from llm_backlog import enqueue as _backlog_enqueue

            _backlog_enqueue("entities", {"text": text, "chroma_id": chroma_id})
        except Exception as _exc:
            log.debug("silenced exception in atoms_store.py: %s", _exc)
        return False

    def _run() -> None:
        try:
            from entity_graph import extract_and_store_entities

            n = extract_and_store_entities(text, chroma_id)
            # CR8 fix: -1 means LLM failure (retryable), queue backlog.
            # Zero or positive means ran — even 0 entities is legit for
            # short/opaque text and shouldn't re-queue.
            if n < 0:
                try:
                    from llm_backlog import enqueue as _backlog_enqueue

                    _backlog_enqueue("entities", {"text": text, "chroma_id": chroma_id})
                except Exception as _exc:
                    log.debug("silenced exception in atoms_store.py: %s", _exc)
        except Exception:
            # Extraction import or unexpected error — queue for backlog
            # so the work isn't lost.
            try:
                from llm_backlog import enqueue as _backlog_enqueue

                _backlog_enqueue("entities", {"text": text, "chroma_id": chroma_id})
            except Exception as _exc:
                log.debug("silenced exception in atoms_store.py: %s", _exc)
        finally:
            _BG_EXTRACT_SEM.release()

    try:
        _get_bg_extract_pool().submit(_run)
        return True
    except Exception:
        # MR4 fix (2026-04-14): on executor shutdown / OOM, release the
        # semaphore AND bump the dropped counter AND enqueue llm_backlog
        # so the entity extraction task isn't silently lost.
        _BG_EXTRACT_SEM.release()
        with _BG_EXTRACT_DROPPED_LOCK:
            _BG_EXTRACT_DROPPED += 1
        try:
            from llm_backlog import enqueue as _backlog_enqueue

            _backlog_enqueue("entities", {"text": text, "chroma_id": chroma_id})
        except Exception as _exc:
            log.debug("silenced exception in atoms_store.py: %s", _exc)
        return False


def bg_extract_dropped_count() -> int:
    """How many extraction tasks were dropped due to inflight cap since boot."""
    with _BG_EXTRACT_DROPPED_LOCK:
        return _BG_EXTRACT_DROPPED


def _now() -> str:
    # Emit Z-suffix so lexicographic comparisons match the Z-normalized
    # timestamps written by memory_lifecycle / entity_graph. Mixed
    # +00:00 vs Z strings broke age ordering in prune / cleanup sweeps.
    # Delegates to db.now_iso(z_suffix=True) — single source of truth.
    from db import now_iso as _db_now_iso

    return _db_now_iso(z_suffix=True)


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
    # brain.db is the hottest SQLite write path. entity_graph._conn already
    # uses the same tuning; matching here cuts typical POST /memory SQLite
    # latency measurably on the busy hour. WAL + synchronous=NORMAL is
    # durable against process crash (loses only the current transaction)
    # and cache_size=-8000 ≈ 8 MB page cache per connection.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
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
    parent_atom_id: str | None = None,
    # v3 Brain Hygiene Stack fields — from ingest_classifier
    provisional: bool = False,
    trust_score: float = 0.5,
    topic_key: str | None = None,
    speaker_entity: str = "chris",
    scope: str = "global",
    db_path: Path | None = None,
) -> str | None:
    """Insert or update an atom row. Returns atom id or None if disabled / failed.

    M8.7: `parent_atom_id` wires to the brain_db@6 column added for
    parent-child chunking. Child atoms set this to the id of their parent
    atom (a larger-context chunk); retrieval paths can swap in parent context
    when the child wins the rank.
    """
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
    # Phase N4: seal the dead insert_raw_event path — every atom on the hot
    # path gets a matching raw_events row so provenance + replay works.
    # Best-effort, gated by BRAIN_ATOMS_ENABLED and content_hash UNIQUE so
    # dedupes cleanly. If the caller already wired raw_event_id, respect it.
    if raw_event_id is None:
        try:
            derived_raw_id = f"raw_{derive_content_hash(text)[:16]}"
            inserted = insert_raw_event(
                event_id=derived_raw_id,
                timestamp=valid_from,
                source_type="atoms_hot_path",
                content=text,
                source_ref=chroma_id,
                db_path=db_path,
            )
            if inserted:
                raw_event_id = inserted
            else:
                # HR1 fix (2026-04-14): content_hash UNIQUE collision
                # means an existing raw_events row holds this content
                # but may NOT have the derived id — pre-atoms-layer
                # rows from ingest pipelines have non-deterministic
                # event_ids (browser_xyz, gmail_abc, etc). Look up the
                # actual id via content_hash so the FK resolves.
                # Previously we guessed the derived id and triggered
                # an IntegrityError on the atoms INSERT, dropping the
                # atom silently (only visible via the SLO counter).
                try:
                    with _conn(db_path) as _c_lookup:
                        row = _c_lookup.execute(
                            "SELECT id FROM raw_events WHERE content_hash = ?",
                            (derive_content_hash(text),),
                        ).fetchone()
                        raw_event_id = row["id"] if row else None
                except sqlite3.Error:
                    raw_event_id = None
        except sqlite3.Error:
            raw_event_id = None
    try:
        with _conn(db_path) as conn:
            conn.execute(
                "INSERT INTO atoms "
                "(id, text, kind, confidence, tier, canonical, version_of, "
                " distilled_by, quality_score, raw_event_id, chroma_id, "
                " collection_hint, valid_from, valid_until, provenance_json, "
                " parent_atom_id, provisional, trust_score, topic_key, "
                " speaker_entity, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  text=excluded.text, "
                "  kind=excluded.kind, "
                # CR3 fix (2026-04-14): preserve promoted tier. SM-2
                # promotes episodic → semantic → core via apply_quality.
                # Re-upserting with tier='episodic' (the default) would
                # demote the atom. Only allow upward transitions AND never
                # demote out of 'core'.
                "  tier=CASE "
                "    WHEN atoms.tier='core' THEN atoms.tier "
                "    WHEN atoms.tier='semantic' AND excluded.tier='episodic' THEN atoms.tier "
                "    ELSE excluded.tier END, "
                # CR3 fix: canonical flag can only move 0 → 1, never 1 → 0
                # (canonical promotion is a one-way ratchet).
                "  canonical=MAX(atoms.canonical, excluded.canonical), "
                "  version_of=excluded.version_of, "
                "  distilled_by=excluded.distilled_by, "
                "  quality_score=excluded.quality_score, "
                "  raw_event_id=COALESCE(excluded.raw_event_id, atoms.raw_event_id), "
                "  collection_hint=excluded.collection_hint, "
                # CR4 fix (2026-04-14): preserve first-seen timestamp. The
                # atom's valid_from is its learning anchor — /brain/evolution
                # and time_decay key off it. Re-upsert must not reset.
                "  valid_from=COALESCE(atoms.valid_from, excluded.valid_from), "
                "  valid_until=excluded.valid_until, "
                "  provenance_json=excluded.provenance_json, "
                "  parent_atom_id=COALESCE(excluded.parent_atom_id, atoms.parent_atom_id), "
                # Phase N2: DO NOT touch confidence via upsert_atom. The only
                # path that moves confidence is update_atom_confidence() — which
                # writes an atom_evidence ledger row in the same transaction,
                # keeping every shift auditable and reversible.
                "  confidence=atoms.confidence, "
                # v3 hygiene fields — let upsert update these freely since
                # they reflect the latest classifier decision.
                "  provisional=excluded.provisional, "
                "  trust_score=excluded.trust_score, "
                "  topic_key=COALESCE(excluded.topic_key, atoms.topic_key), "
                "  speaker_entity=excluded.speaker_entity, "
                "  scope=excluded.scope, "
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
                    parent_atom_id,
                    1 if provisional else 0,
                    trust_score,
                    topic_key,
                    speaker_entity,
                    scope,
                    now,
                    now,
                ),
            )
            conn.commit()

            # v3 Layer B — Neo4j write restoration. Every atom on the hot
            # path triggers entity extraction → Neo4j Entity nodes +
            # RELATES_TO edges. Without this hook, the graph was stuck at
            # 224 entities for 620 atoms because extraction only ran from
            # promote_canonical (nightly) and proactive.py (6h).
            #
            # F3 (2026-04-14): use the bounded _submit_bg_extract pool so a
            # backfill burst can't spawn unbounded daemon threads. Drop-on-
            # overflow is safe because the nightly entity reconciliation
            # cron catches anything the hot path missed.
            if text and len(text) > 40:
                _submit_bg_extract(text[:1500], chroma_id)

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


def apply_explicit_replaces(
    new_atom_chroma_id: str,
    replaces_atom_ids: list[str],
    *,
    reason: str = "",
    agent: str = "unknown",
    db_path: Path | None = None,
) -> dict:
    """Explicit AI-driven supersession (2026-04-26).

    The caller (an AI front-end or user-correction handler) tells the brain
    that the new atom EXPLICITLY replaces specific older atoms. This skips
    the cosine-similarity gate in `ingest_mirror._run_semantic_supersession`
    and goes straight to:
      - parent.superseded_by = new_atom_id
      - parent.valid_until   = now()
      - new.supersedes (via mark_superseded provenance edge)
      - audit_log entry: "explicit_update" with agent + reason

    Used for:
      1. AI detected change language ("X was Y, now Z" / "이제 X")
      2. User correction ("아니야, 그게 아니라 ...")
      3. Programmatic update from a deterministic source

    Returns: {"applied": [ids], "skipped": [{"id":..., "reason":...}], "error": str|None}
    """
    if not BRAIN_ATOMS_ENABLED:
        return {"applied": [], "skipped": [], "error": "atoms_disabled"}
    if not replaces_atom_ids:
        return {"applied": [], "skipped": [], "error": None}

    summary: dict = {"applied": [], "skipped": [], "error": None}
    now_iso = _now()
    try:
        with _conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            new_row = conn.execute(
                "SELECT id, chroma_id FROM atoms WHERE chroma_id = ?",
                (new_atom_chroma_id,),
            ).fetchone()
            if not new_row:
                conn.rollback()
                return {"applied": [], "skipped": [], "error": "new_atom_not_found"}
            new_id = new_row["id"]
            for old_id in replaces_atom_ids:
                old = conn.execute(
                    "SELECT id, tier FROM atoms WHERE id = ? OR chroma_id = ?",
                    (old_id, old_id),
                ).fetchone()
                if not old:
                    summary["skipped"].append({"id": old_id, "reason": "not_found"})
                    continue
                if old["tier"] == "obsolete":
                    summary["skipped"].append({"id": old_id, "reason": "already_obsolete"})
                    continue
                conn.execute(
                    "UPDATE atoms SET superseded_by = ?, valid_until = ?, updated_at = ? " "WHERE id = ?",
                    (new_id, now_iso, now_iso, old["id"]),
                )
                conn.execute(
                    "UPDATE atoms SET supersedes = ?, updated_at = ? WHERE id = ?",
                    (old["id"], now_iso, new_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO provenance "
                    "(parent_kind, parent_id, child_kind, child_id, relation, created_at) "
                    "VALUES ('atom', ?, 'atom', ?, 'explicit_supersede', ?)",
                    (old["id"], new_id, now_iso),
                )
                summary["applied"].append(old["id"])
            conn.commit()
    except sqlite3.Error as exc:
        return {"applied": [], "skipped": [], "error": f"sqlite:{exc}"}

    # Best-effort audit log so explicit updates are visible in /brain/audit.
    if summary["applied"]:
        try:
            from audit_log import log_event

            log_event(
                event_type="explicit_update",
                source=f"agent:{agent}",
                payload={
                    "new_atom_id": new_id,
                    "replaced_ids": summary["applied"],
                    "reason": reason or "ai-explicit-update",
                },
            )
        except Exception as exc:
            log.debug("apply_explicit_replaces audit log skipped: %s", exc)
    return summary


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


# ── Phase N2: mutable Bayesian confidence ledger ────────────────────────

_CONFIDENCE_MIN = 0.02
_CONFIDENCE_MAX = 0.98
_ALLOWED_EVIDENCE_EVENTS = frozenset(
    {"corroborate", "contradict", "reinforce", "retrieval_hit", "retrieval_miss", "manual"}
)


def _conf_to_logit(p: float) -> float:
    p = max(_CONFIDENCE_MIN, min(_CONFIDENCE_MAX, float(p)))
    return math.log(p / (1.0 - p))


def _logit_to_conf(logit: float) -> float:
    prob = 1.0 / (1.0 + math.exp(-logit))
    return max(_CONFIDENCE_MIN, min(_CONFIDENCE_MAX, prob))


def update_atom_confidence(
    atom_id: str,
    event_type: str,
    weight: float,
    evidence_ref: str | None = None,
    cluster_size: int = 1,
    *,
    db_path: Path | None = None,
) -> dict | None:
    """Phase N2: localized, reversible confidence update.

    Logit-space shift: `new_logit = old_logit + weight / max(1, cluster_size)`.
    Kuhn semantic uncertainty — dividing by the cluster size normalizes the
    evidence so a single observation among k near-duplicates counts as 1/k.
    Clamps to [_CONFIDENCE_MIN, _CONFIDENCE_MAX] so sigmoid never saturates.

    Writes one append-only row to atom_evidence AND updates atoms.confidence
    in the SAME transaction — if either leg fails, both roll back and the
    confidence stays stable. Caller gets back `{atom_id, old_conf, new_conf,
    evidence_id}` on success or None on failure / disabled.
    """
    if not BRAIN_ATOMS_ENABLED:
        return None
    if event_type not in _ALLOWED_EVIDENCE_EVENTS:
        return None
    denom = max(1, int(cluster_size or 1))
    effective_weight = float(weight) / denom
    try:
        with _conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT confidence FROM atoms WHERE id = ?", (atom_id,)).fetchone()
            if not row:
                conn.rollback()
                return None
            old_conf = float(row["confidence"] or 0.5)
            new_logit = _conf_to_logit(old_conf) + effective_weight
            new_conf = _logit_to_conf(new_logit)
            cur = conn.execute(
                "INSERT INTO atom_evidence "
                "(atom_id, event_type, weight, evidence_ref, cluster_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (atom_id, event_type, float(weight), evidence_ref, denom, _now()),
            )
            evidence_id = cur.lastrowid
            conn.execute(
                "UPDATE atoms SET confidence = ?, updated_at = ? WHERE id = ?",
                (new_conf, _now(), atom_id),
            )
            conn.commit()
            return {
                "atom_id": atom_id,
                "old_conf": round(old_conf, 4),
                "new_conf": round(new_conf, 4),
                "evidence_id": evidence_id,
            }
    except sqlite3.Error:
        return None


def update_provisional_flag(
    atom_chroma_id: str,
    provisional: bool,
    *,
    db_path: Path | None = None,
) -> bool:
    """Phase G2: flip the provisional flag on an existing atom by chroma_id.

    Used by POST /memory when a write triggers an unresolved contradiction:
    the new atom is marked provisional so search_unified.search_all excludes
    it from retrieval (default include_provisional=False) until the conflict
    is settled via POST /memory/contradictions/{id}/resolve. The resolve
    handler clears the flag for keep_new / both_true / dismiss actions; for
    keep_old / merge the row is orphaned at the vector layer (the atoms.db
    row stays harmless because no vector matches it).

    Matches by chroma_id directly (mirrors `get_atom_by_chroma_id`,
    `mark_superseded`, `reinforce`). An earlier draft hashed the input
    via derive_atom_id, but the resolve path receives `new_id` from the
    contradictions metadata which can carry a different string shape than
    the value upsert_atom was originally called with — direct match
    avoids that silent-mismatch class.

    Returns True when the row was updated, False on missing / disabled.
    """
    if not BRAIN_ATOMS_ENABLED:
        return False
    if not atom_chroma_id:
        return False
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "UPDATE atoms SET provisional = ?, updated_at = ? WHERE chroma_id = ?",
                (1 if provisional else 0, _now(), atom_chroma_id),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.Error:
        return False


def get_confidence_history(atom_id: str, *, limit: int = 50, db_path: Path | None = None) -> list[dict]:
    """Return the atom_evidence ledger for an atom, most recent first.

    Caller can audit every logit delta that moved an atom's confidence.
    Empty list on disabled / unknown atom — never None so callers can
    iterate unconditionally.
    """
    if not BRAIN_ATOMS_ENABLED:
        return []
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                "SELECT id, event_type, weight, evidence_ref, cluster_size, created_at "
                "FROM atom_evidence WHERE atom_id = ? ORDER BY id DESC LIMIT ?",
                (atom_id, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def rollback_confidence(
    atom_id: str, back_to_event_id: int = 0, *, db_path: Path | None = None
) -> dict | None:
    """Replay the atom_evidence ledger in reverse up to (not including) the
    given event id. back_to_event_id=0 means "undo every event" — the atom
    returns to its write-time default confidence 0.5.

    Reversibility is the ROME principle: every confidence shift must be
    undoable. We compute the target logit by integrating all surviving
    events' (weight/cluster_size) from the initial 0.5 base, then write
    that back + log a "manual" rollback audit row.
    """
    if not BRAIN_ATOMS_ENABLED:
        return None
    try:
        with _conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            atom = conn.execute("SELECT confidence FROM atoms WHERE id = ?", (atom_id,)).fetchone()
            if not atom:
                conn.rollback()
                return None
            base_logit = _conf_to_logit(0.5)
            surviving = conn.execute(
                "SELECT weight, cluster_size FROM atom_evidence "
                "WHERE atom_id = ? AND id < ? ORDER BY id ASC",
                (atom_id, int(back_to_event_id) or 0),
            ).fetchall()
            replay_logit = base_logit + sum((r["weight"] / max(1, r["cluster_size"])) for r in surviving)
            new_conf = _logit_to_conf(replay_logit)
            conn.execute(
                "UPDATE atoms SET confidence = ?, updated_at = ? WHERE id = ?",
                (new_conf, _now(), atom_id),
            )
            conn.execute(
                "INSERT INTO atom_evidence "
                "(atom_id, event_type, weight, evidence_ref, cluster_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    atom_id,
                    "manual",
                    0.0,
                    f"rollback_to:{back_to_event_id}",
                    1,
                    _now(),
                ),
            )
            conn.commit()
            return {"atom_id": atom_id, "new_conf": round(new_conf, 4), "replayed": len(surviving)}
    except sqlite3.Error:
        return None


# Kuhn cluster-size cache: (chroma_id) → (size, inserted_at). 5-min TTL.
# Size-capped to prevent unbounded growth over months of writes (a fresh
# chroma_id per POST /memory with no eviction would leak indefinitely).
_CLUSTER_SIZE_CACHE: dict[str, tuple[int, float]] = {}
_CLUSTER_SIZE_TTL = 300.0
_CLUSTER_SIZE_MAX = 2048


def _cluster_size_cache_put(key: str, value: tuple[int, float]) -> None:
    if len(_CLUSTER_SIZE_CACHE) >= _CLUSTER_SIZE_MAX:
        # Evict oldest 10% by insertion timestamp.
        victims = sorted(_CLUSTER_SIZE_CACHE.items(), key=lambda kv: kv[1][1])
        for k, _ in victims[: _CLUSTER_SIZE_MAX // 10]:
            _CLUSTER_SIZE_CACHE.pop(k, None)
    _CLUSTER_SIZE_CACHE[key] = value


def cluster_size_for(
    chroma_id: str,
    embedding: list[float] | None = None,
    *,
    threshold: float = 0.92,
    db_path: Path | None = None,
) -> int:
    """Count near-duplicate atoms (Kuhn semantic uncertainty normalizer).

    Counts ``atoms`` rows with matching chroma_id entries that land within
    ``threshold`` cosine similarity in semantic_memory. Returns 1 on empty
    corpus or any error — the update path multiplies by 1/size so a
    conservative 1 floor means "no normalization".

    LRU-cached by chroma_id for 5 minutes to keep POST /memory latency
    within SLO — the probe would otherwise add ~15-30ms per write.
    """
    if not BRAIN_ATOMS_ENABLED or not chroma_id:
        return 1
    now_ts = time.time()
    cached = _CLUSTER_SIZE_CACHE.get(chroma_id)
    if cached and (now_ts - cached[1]) < _CLUSTER_SIZE_TTL:
        return cached[0]

    if embedding is None:
        _cluster_size_cache_put(chroma_id, (1, now_ts))
        return 1

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from vector_store import get_vector_store

        hits = get_vector_store().query(
            "semantic_memory",
            vector=embedding,
            k=10,
            with_payload=False,
        )
        # VectorHit.score is normalized similarity (higher=better). The
        # caller's `threshold` is a similarity LOWER BOUND (default 0.92 =
        # "near-duplicate"). Previous code inverted this into a distance
        # cap and counted every result — the semantic uncertainty
        # normalizer effectively never discounted anything.
        size = sum(1 for h in hits if h.score >= threshold)
        size = max(1, size)
        _cluster_size_cache_put(chroma_id, (size, now_ts))
        return size
    except Exception:
        _cluster_size_cache_put(chroma_id, (1, now_ts))
        return 1


def count_atoms(*, db_path: Path | None = None) -> dict[str, int | dict]:
    """Aggregate counts for /brain/atoms/stats and /brain/health.

    Extended 2026-05-15 (P4-11) with by_kind, confidence_buckets, and a
    low_quality_recall_atoms counter pulled from atom_recall_quality so the
    parity endpoint is useful for dashboards. All new fields are additive;
    existing keys keep their shape so legacy consumers remain stable.
    """
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
            by_kind = {
                str(r[0]): int(r[1])
                for r in conn.execute("SELECT kind, COUNT(*) FROM atoms GROUP BY kind").fetchall()
            }
            confidence_buckets = {
                "high_0.7_plus": conn.execute(
                    "SELECT COUNT(*) FROM atoms WHERE confidence >= 0.7"
                ).fetchone()[0],
                "mid_0.4_to_0.7": conn.execute(
                    "SELECT COUNT(*) FROM atoms WHERE confidence >= 0.4 AND confidence < 0.7"
                ).fetchone()[0],
                "low_below_0.4": conn.execute("SELECT COUNT(*) FROM atoms WHERE confidence < 0.4").fetchone()[
                    0
                ],
            }
            try:
                low_quality_recall = conn.execute(
                    "SELECT COUNT(*) FROM atom_recall_quality "
                    "WHERE accuracy IS NOT NULL AND accuracy < 0.3 "
                    "  AND (n_good + n_wrong + n_restated) >= 3"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                low_quality_recall = None
            return {
                "enabled": 1,
                "atoms_total": atoms_total,
                "canonical": canonical,
                "episodic": episodic,
                "semantic": semantic,
                "core": core,
                "obsolete": obsolete,
                "raw_events": raw_events,
                "by_kind": by_kind,
                "confidence_buckets": confidence_buckets,
                "low_quality_recall_atoms": low_quality_recall,
            }
    except sqlite3.Error:
        return {"enabled": 1, "error": "sqlite_error"}


def upsert_entity(
    name: str,
    entity_type: str = "concept",
    *,
    db_path: Path | None = None,
) -> str | None:
    """Phase N4: idempotent insert into brain.db `entities` mirror.

    The entities table exists since brain_db@1. Neo4j is primary for the
    graph, but atom_entity has FKs on this SQL mirror — so we have to keep
    it populated on the hot path to land atom↔entity edges.

    Returns entity_id on success (existing or newly-created), None otherwise.
    Name normalization: trimmed lower-case, deduped on (name, entity_type).
    """
    if not BRAIN_ATOMS_ENABLED:
        return None
    nm = (name or "").strip().lower()
    if not nm or len(nm) > 100:
        return None
    etype = (entity_type or "concept").lower()
    try:
        with _conn(db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM entities WHERE name = ? AND entity_type = ?",
                (nm, etype),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE entities SET last_seen_at = ?, mention_count = mention_count + 1 " "WHERE id = ?",
                    (_now(), existing["id"]),
                )
                conn.commit()
                return existing["id"]
            new_id = f"ent_{hashlib.sha256((nm + ':' + etype).encode()).hexdigest()[:12]}"
            conn.execute(
                "INSERT INTO entities (id, name, entity_type, first_seen_at, last_seen_at, mention_count) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (new_id, nm, etype, _now(), _now()),
            )
            conn.commit()
            return new_id
    except sqlite3.Error:
        return None


def link_atom_entity(
    atom_id: str,
    entity_id: str,
    role: str = "subject",
    *,
    db_path: Path | None = None,
) -> bool:
    """Phase N4: INSERT OR IGNORE an atom↔entity edge.

    The atom_entity table has existed since brain_db@1 (DDL line 117) but
    the sleep_consolidate pipeline + entity_graph are the only writers.
    Seals Gap C5 — the join table finally fills.

    Returns True on insert (or no-op conflict), False on disabled / error.
    """
    if not BRAIN_ATOMS_ENABLED:
        return False
    if not atom_id or not entity_id:
        return False
    try:
        with _conn(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO atom_entity (atom_id, entity_id, role) " "VALUES (?, ?, ?)",
                (atom_id, entity_id, role),
            )
            conn.commit()
            return True
    except sqlite3.Error:
        return False


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
