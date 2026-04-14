"""brain_core/migrations_brain_db.py — Phase 3 atoms layer migrations.

Registers `brain_db` component versions 0→1→2→3 with schema_versions.py.
Migrations are IDEMPOTENT (CREATE TABLE IF NOT EXISTS, INSERT OR IGNORE) and
safe to run on every startup. The atoms_store WRITE path is separately gated
by BRAIN_ATOMS_ENABLED so we can land the schema before enabling write hooks.

Migration chain:
  0 → 1  Create brain.db schema (raw_events, atoms, entities, atom_entity, provenance, action_audit)
  1 → 2  Backfill raw_events from ~/server/knowledge/raw/inbox/*.json (idempotent on content_hash UNIQUE)
  2 → 3  Backfill atoms from semantic_memory Chroma collection + canonical/**.md
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.migrations_brain_db")

try:
    from atoms_store import _DDL as BRAIN_DB_DDL
    from atoms_store import _now, derive_atom_id, derive_content_hash
    from config import BRAIN_DB, CANONICAL_DIR, INBOX_DIR
    from schema_versions import CURRENT_VERSIONS, migration
except ImportError as e:
    log.error(f"migrations_brain_db import failed: {e}")
    raise

# Always register at the latest version — migrations are idempotent and safe
# to run on every startup. Without this, a brain restart after a manual migrate
# would hit downgrade-refused on subsequent restarts (db v3 vs code v0).
CURRENT_VERSIONS["brain_db"] = 5


def _safe_int(v: object, default: int = 0) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


def _connect_brain_db() -> sqlite3.Connection:
    BRAIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@migration("brain_db", 0, 1)
def _create_brain_db_schema() -> dict:
    """Apply the brain.db DDL. Idempotent (uses CREATE TABLE IF NOT EXISTS)."""
    conn = _connect_brain_db()
    try:
        conn.executescript(BRAIN_DB_DDL)
        conn.commit()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {"created": str(BRAIN_DB), "tables": [r[0] for r in rows]}
    finally:
        conn.close()


@migration("brain_db", 1, 2)
def _backfill_raw_events() -> dict:
    """Walk ~/server/knowledge/raw/inbox/*.json and load into raw_events.

    Idempotent via UNIQUE content_hash. JSON files are NEVER deleted — json_path
    retains the absolute path so callers can re-load the original record.
    """
    if not INBOX_DIR.exists():
        return {"inserted": 0, "skipped": 0, "malformed": 0, "reason": "inbox missing"}

    conn = _connect_brain_db()
    inserted = skipped = malformed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for json_path in sorted(INBOX_DIR.glob("*.json")):
            try:
                rec = json.loads(json_path.read_text())
            except (OSError, json.JSONDecodeError):
                malformed += 1
                continue
            content = rec.get("content") or ""
            if not content:
                malformed += 1
                continue
            content_hash = derive_content_hash(content)
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO raw_events "
                    "(id, content_hash, timestamp, source_type, source_ref, actor, "
                    " visibility, scrub_status, content, attachments_json, entities_json, "
                    " json_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.get("id") or json_path.stem,
                        content_hash,
                        rec.get("timestamp") or _now(),
                        rec.get("source_type") or "unknown",
                        rec.get("source_ref") or "",
                        rec.get("actor") or "unknown",
                        rec.get("visibility") or "private",
                        rec.get("scrub_status") or "scrubbed",
                        content,
                        json.dumps(rec.get("attachments") or []),
                        json.dumps(rec.get("entities") or []),
                        str(json_path),
                        _now(),
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception:
                malformed += 1
        conn.commit()
    finally:
        conn.close()

    return {"inserted": inserted, "skipped": skipped, "malformed": malformed}


def _http_get(url: str) -> dict | None:
    """Fetch JSON from a local URL. Used to paginate semantic_memory via Chroma HTTP."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read())
    except Exception:
        return None


def _kind_from_category(cat: str) -> str:
    return {
        "preference": "preference",
        "fact": "fact",
        "decision": "decision",
        "entity": "entity",
        "correction": "correction",
    }.get((cat or "").lower(), "fact")


def _kind_from_domain(domain: str) -> str:
    return {
        "decisions": "decision",
        "preferences": "preference",
        "infra": "fact",
        "projects": "fact",
        "incidents": "fact",
    }.get((domain or "").lower(), "fact")


def _parse_canonical_frontmatter(md_path: Path) -> dict | None:
    """Parse frontmatter of a canonical markdown file. Mirror of provenance._parse_frontmatter
    but without the import dance — keeps this migration self-contained."""
    try:
        text = md_path.read_text()
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) < 3:
        return None
    first = lines[0].strip()
    if not (first.startswith("---") or first.startswith("{")):
        return None
    if first.startswith("---"):
        end_index = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break
        if end_index is None:
            return None
        raw = "\n".join(lines[1:end_index])
        body = "\n".join(lines[end_index + 1 :])
    else:
        try:
            body_start = text.index("---")
            raw = text[:body_start]
            body = text[body_start + 3 :]
        except ValueError:
            return None
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        return None
    meta["_body_preview"] = body.strip()[:500]
    return meta


@migration("brain_db", 2, 3)
def _backfill_atoms() -> dict:
    """Backfill atoms from existing knowledge:
    Pass 1 → semantic_memory Chroma collection (~252 entries) as kind='*' with canonical=0
    Pass 2 → canonical/**.md (~266 entries) with canonical=1, tier='core'
    """
    chroma_url = os.getenv("CHROMA_URL", "http://127.0.0.1:8000")
    conn = _connect_brain_db()
    sm_inserted = canonical_inserted = errors = 0

    try:
        conn.execute("BEGIN IMMEDIATE")

        # ─── Pass 1: semantic_memory ─────────────────────────
        # Use Chroma HTTP API v2: GET /api/v2/tenants/default_tenant/databases/default_database/collections/{name}/get
        # We page through all entries and mirror them.
        coll_url = f"{chroma_url}/api/v2/tenants/default_tenant/databases/default_database/collections"
        cols = _http_get(coll_url) or []
        sem_id = None
        for c in cols if isinstance(cols, list) else []:
            if c.get("name") == "semantic_memory":
                sem_id = c.get("id")
                break

        if sem_id:
            # Use POST /collections/{id}/get with empty body to fetch all
            req_body = json.dumps({"limit": 1000, "include": ["metadatas", "documents"]}).encode()
            req = urllib.request.Request(  # noqa: S310
                f"{coll_url}/{sem_id}/get",
                data=req_body,
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                    page = json.loads(resp.read())
            except Exception as e:
                page = {"error": str(e)}

            ids = page.get("ids") or []
            docs = page.get("documents") or []
            metas = page.get("metadatas") or []
            for chroma_id, doc, meta in zip(ids, docs, metas, strict=False):
                if not doc or not chroma_id:
                    continue
                meta = meta or {}
                kind = _kind_from_category(meta.get("category"))
                tier = meta.get("memory_class") or "episodic"
                if tier not in ("episodic", "semantic", "core", "obsolete"):
                    tier = "episodic"
                atom_id = derive_atom_id(chroma_id)
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO atoms "
                        "(id, text, kind, confidence, tier, canonical, distilled_by, "
                        " chroma_id, collection_hint, valid_from, valid_until, "
                        " provenance_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 0, 'backfill', ?, 'semantic_memory', "
                        " ?, ?, ?, ?, ?)",
                        (
                            atom_id,
                            doc[:2000],
                            kind,
                            float(meta.get("confidence", 0.5)),
                            tier,
                            chroma_id,
                            meta.get("valid_from") or meta.get("created_at") or _now(),
                            meta.get("valid_until"),
                            json.dumps({"source_meta": {k: meta[k] for k in meta if k != "embedding"}}),
                            _now(),
                            _now(),
                        ),
                    )
                    if cur.rowcount > 0:
                        sm_inserted += 1
                except Exception:
                    errors += 1

        # ─── Pass 2: canonical markdown ──────────────────────
        if CANONICAL_DIR.exists():
            for md_path in CANONICAL_DIR.rglob("*.md"):
                meta = _parse_canonical_frontmatter(md_path)
                if not meta or not meta.get("id"):
                    continue
                cid = f"canonical:{meta['id']}"
                atom_id = derive_atom_id(cid)
                domain = meta.get("domain") or md_path.parent.name
                title = meta.get("title") or meta["id"]
                body = meta.get("_body_preview", "")
                text = (title + "\n" + body)[:2000]
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO atoms "
                        "(id, text, kind, confidence, tier, canonical, version_of, "
                        " distilled_by, chroma_id, collection_hint, valid_from, valid_until, "
                        " provenance_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'core', 1, ?, 'canonical', ?, 'canonical', "
                        " ?, ?, ?, ?, ?)",
                        (
                            atom_id,
                            text,
                            _kind_from_domain(domain),
                            float(meta.get("confidence", 0.8)),
                            meta["id"],
                            cid,
                            meta.get("valid_from") or _now(),
                            meta.get("valid_to"),
                            json.dumps({"path": str(md_path)}),
                            _now(),
                            _now(),
                        ),
                    )
                    if cur.rowcount > 0:
                        canonical_inserted += 1
                    # Insert provenance edges from frontmatter `relations`
                    for rel in meta.get("relations") or []:
                        if isinstance(rel, dict) and rel.get("target"):
                            conn.execute(
                                "INSERT OR IGNORE INTO provenance "
                                "(parent_kind, parent_id, child_kind, child_id, relation, confidence, created_at) "
                                "VALUES ('canonical', ?, 'canonical', ?, ?, ?, ?)",
                                (
                                    meta["id"],
                                    rel["target"],
                                    rel.get("type", "related"),
                                    rel.get("confidence"),
                                    _now(),
                                ),
                            )
                except Exception:
                    errors += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "semantic_memory_inserted": sm_inserted,
        "canonical_inserted": canonical_inserted,
        "errors": errors,
    }


# ──────────────────────────────────────────────────────────────────────
# brain_db@4 — Phase M6: SearXNG learning loop tables
# ──────────────────────────────────────────────────────────────────────


_M6_DDL = """
CREATE TABLE IF NOT EXISTS web_search_attempts (
  id     TEXT PRIMARY KEY,
  query  TEXT NOT NULL,
  ts     TEXT NOT NULL,
  agent  TEXT NOT NULL DEFAULT 'unknown',
  intent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_web_search_attempts_ts ON web_search_attempts(ts);

CREATE TABLE IF NOT EXISTS web_search_results (
  attempt_id TEXT NOT NULL REFERENCES web_search_attempts(id) ON DELETE CASCADE,
  rank       INTEGER NOT NULL,
  url        TEXT NOT NULL,
  domain     TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  snippet    TEXT NOT NULL DEFAULT '',
  chosen     INTEGER NOT NULL DEFAULT 0,
  outcome    TEXT,
  PRIMARY KEY (attempt_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_web_search_results_domain ON web_search_results(domain);
CREATE INDEX IF NOT EXISTS idx_web_search_results_outcome ON web_search_results(outcome) WHERE outcome IS NOT NULL;

CREATE TABLE IF NOT EXISTS web_source_trust (
  domain       TEXT PRIMARY KEY,
  n_used       INTEGER NOT NULL DEFAULT 0,
  n_correct    INTEGER NOT NULL DEFAULT 0,
  score        REAL NOT NULL DEFAULT 0.5,
  last_updated TEXT NOT NULL
);
"""


@migration("brain_db", 3, 4)
def _create_web_search_tables() -> dict:
    """Add web_search_attempts/results/source_trust tables for Phase M6
    SearXNG learning loop. Idempotent (CREATE TABLE IF NOT EXISTS).
    """
    conn = _connect_brain_db()
    try:
        conn.executescript(_M6_DDL)
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'web_%' ORDER BY name"
        ).fetchall()
        return {"created": [r[0] for r in rows]}
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# brain_db@5 — Phase M7 WS8: per-actor adoption tracking on action_audit
# ──────────────────────────────────────────────────────────────────────


@migration("brain_db", 4, 5)
def _add_action_audit_actor() -> dict:
    """Add actor + tool columns to action_audit so per-agent adoption can be
    measured. Idempotent — checks pragma table_info first. Backfills existing
    rows with actor='unknown', tool=route.
    """
    conn = _connect_brain_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(action_audit)").fetchall()}
        if "actor" not in cols:
            conn.execute("ALTER TABLE action_audit ADD COLUMN actor TEXT NOT NULL DEFAULT 'unknown'")
        if "tool" not in cols:
            conn.execute("ALTER TABLE action_audit ADD COLUMN tool TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE action_audit SET tool = route WHERE tool = ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_audit_actor_ts " "ON action_audit(actor, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_audit_tool_ts " "ON action_audit(tool, created_at)"
        )
        conn.commit()
        final_cols = {r[1] for r in conn.execute("PRAGMA table_info(action_audit)").fetchall()}
        return {"columns": sorted(final_cols)}
    finally:
        conn.close()
