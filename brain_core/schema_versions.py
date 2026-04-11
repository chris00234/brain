"""brain_core/schema_versions.py — Schema version tracking and migration runner.

On brain-server startup, checks registered component versions against DB state.
Runs pending migrations. Refuses to start if current code is OLDER than DB
(downgrade protection).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("brain.schema_versions")

VERSIONS_DB = Path("/Users/chrischo/server/brain/logs/schema_versions.db")

# Component registry — code defines what version it expects
CURRENT_VERSIONS = {
    "semantic_memory": 4,     # R4 added: supersedes, valid_from, memory_class, trust_score
    "experience": 2,          # R2 added: embed_model_version
    "canonical": 3,           # R3 added: frontmatter spec
    "neo4j_schema": 2,        # R4 added: Lesson, Skill nodes
    "llm_usage": 2,           # R5 added: skipped_cb column
    "fts_index": 2,           # R4 added: unicode61 tokenizer (from porter)
    "agent_prefs": 1,         # R5 baseline
    "self_heal_state": 1,     # R6 baseline
    "contradiction_votes": 1, # R6 baseline
}


# Migration registry: {(component, from_ver, to_ver): callable}
MIGRATIONS: dict[tuple[str, int, int], Callable[[], dict]] = {}


def migration(component: str, from_version: int, to_version: int):
    """Decorator to register a migration function."""
    def decorator(fn: Callable[[], dict]):
        MIGRATIONS[(component, from_version, to_version)] = fn
        return fn
    return decorator


def _conn():
    VERSIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(VERSIONS_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            rollback_snapshot TEXT
        )
    """)
    conn.commit()
    return conn


def get_version(component: str) -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE component = ?",
            (component,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def set_version(component: str, version: int, snapshot: str | None = None, conn: sqlite3.Connection | None = None):
    """Write version. If conn is provided, caller owns the txn (no close/commit)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO schema_versions (component, version, applied_at, rollback_snapshot) VALUES (?, ?, ?, ?)",
            (component, version, datetime.now(timezone.utc).isoformat(), snapshot),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _run_one_migration(component: str, from_ver: int, to_ver: int) -> tuple[bool, str]:
    """Run a single migration atomically with the version bump.

    Returns (ok, detail). The version bump happens in the same txn as whatever
    the migration records in the versions DB — if the migration raises, the
    version is NOT advanced.
    """
    key = (component, from_ver, to_ver)
    migration_fn = MIGRATIONS.get(key)
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if migration_fn is None:
            # No registered transform — treat as a no-op bump (schema already
            # compatible, e.g. baseline components on fresh deploy).
            set_version(component, to_ver, conn=conn)
            conn.commit()
            return True, "no_migration_registered (treated as no-op)"
        result = migration_fn()
        set_version(component, to_ver, conn=conn)
        conn.commit()
        return True, str(result)
    except Exception as e:
        conn.rollback()
        return False, f"failed: {e}"
    finally:
        conn.close()


def check_and_migrate() -> dict:
    """Check all components. Run pending migrations. Returns status dict.

    Raises RuntimeError on downgrade (caller should fail startup).

    Uses a file-based advisory lock (fcntl.flock on a sidecar) to serialize
    concurrent startups — e.g., launchd restarting the server while a manual
    invocation races check_and_migrate. Without this, two processes can both
    read current=N and both try to apply the same migration, one winning and
    the other logging a spurious "failed: database is locked".
    """
    import fcntl
    lock_path = VERSIONS_DB.parent / "schema_versions.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return _check_and_migrate_locked()
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _check_and_migrate_locked() -> dict:
    status = {"migrated": [], "up_to_date": [], "downgrade_refused": []}

    for component, target_version in CURRENT_VERSIONS.items():
        current = get_version(component)

        if current == target_version:
            status["up_to_date"].append(f"{component}@{target_version}")
            continue

        if current > target_version:
            log.error(
                "Downgrade refused for %s: DB has v%d, code expects v%d",
                component, current, target_version,
            )
            status["downgrade_refused"].append(f"{component}: db={current} code={target_version}")
            continue

        # Upgrade path (includes current=0 → target: run the full chain).
        for ver in range(current, target_version):
            ok, detail = _run_one_migration(component, ver, ver + 1)
            if ok:
                status["migrated"].append(f"{component} v{ver} → v{ver+1}: {detail}")
                log.info("Migrated %s v%d → v%d", component, ver, ver + 1)
            else:
                log.error("Migration %s v%d → v%d failed: %s", component, ver, ver + 1, detail)
                status["migrated"].append(f"{component} v{ver} → v{ver+1}: {detail}")
                break  # stop upgrade chain for this component

    if status["downgrade_refused"]:
        raise RuntimeError(
            "schema downgrade refused: " + "; ".join(status["downgrade_refused"])
        )
    return status


# ── Example migration registrations ─────────────────────────
# (Real migrations live here or in component-specific modules)

@migration("semantic_memory", 3, 4)
def _migrate_semantic_memory_3_to_4() -> dict:
    """Backfill trust_score=0.5 on any memory that lacks it.

    Added in Round 4 Phase 1E. Running this migration on older memories ensures
    they participate in trust_score ranking when the feature flag is enabled.
    """
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from http_pool import http_json
        from search import get_collections

        cols = get_collections()
        sem_col = cols.get("semantic_memory")
        if not sem_col:
            return {"updated": 0, "reason": "collection missing"}

        # Fetch all docs missing trust_score
        resp = http_json(
            "POST",
            f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/get",
            {"limit": 50000, "include": ["metadatas"]},
        )
        ids = resp.get("ids", [])
        metas = resp.get("metadatas", []) or []

        needs_update = []
        for mid, meta in zip(ids, metas):
            if not meta or "trust_score" not in meta:
                needs_update.append(mid)

        if not needs_update:
            return {"updated": 0}

        # Batch update
        BATCH = 200
        updated = 0
        for i in range(0, len(needs_update), BATCH):
            batch = needs_update[i:i+BATCH]
            http_json(
                "POST",
                f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/update",
                # Round 11 fix: use string format to match all other writers
                # (learn.py, memory_lifecycle.py, entity_graph.py). Float
                # storage was breaking ChromaDB $lt/$gt where filters that
                # expect type-consistent values across rows.
                {"ids": batch, "metadatas": [{"trust_score": "0.5"} for _ in batch]},
            )
            updated += len(batch)

        return {"updated": updated}
    except Exception as e:
        return {"error": str(e), "updated": 0}


if __name__ == "__main__":
    import json
    result = check_and_migrate()
    print(json.dumps(result, indent=2))
