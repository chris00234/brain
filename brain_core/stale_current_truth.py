"""Detect stale current-state claims in canonical memory.

This complements ``canonical_staleness``. The older staleness job verifies
code-reality claims in distilled notes; this module verifies current-truth
supersession claims such as "ChromaDB is the current vector store" after the
Qdrant cutover.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from config import BRAIN_DB, BRAIN_DIR, KNOWLEDGE_DIR
except ImportError:  # pragma: no cover - local CLI fallback
    BRAIN_DIR = Path("/Users/chrischo/server/brain")
    BRAIN_DB = BRAIN_DIR / "logs" / "brain.db"
    KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")


DEFAULT_DECOMMISSIONED_TERMS_PATH = BRAIN_DIR / "config" / "decommissioned_terms.json"


@dataclass(frozen=True)
class DecommissionedTerm:
    term: str
    replaced_by: str
    decommissioned_at: str
    current_doc: str
    aliases: tuple[str, ...]


FALLBACK_DECOMMISSIONED_TERMS: tuple[DecommissionedTerm, ...] = (
    DecommissionedTerm(
        term="ChromaDB",
        replaced_by="Qdrant",
        decommissioned_at="2026-04-21",
        current_doc="canonical/infra/rag-qdrant.md",
        aliases=("ChromaDB", "chromadb", "chroma_api", "Chroma API"),
    ),
)

CURRENT_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:is|as|acts as|serves as)\s+(?:the\s+)?(?:current\s+|active\s+)?(?:vector|retrieval|RAG)", re.I
    ),
    re.compile(r"\b(?:current|active|live)\s+(?:Brain\s+)?(?:vector|retrieval|RAG)", re.I),
    re.compile(
        r"\b(?:built on|implemented with|backed by|uses|using|paired with|alongside)\s+ChromaDB\b", re.I
    ),
    re.compile(r"\bChromaDB-backed\b", re.I),
    re.compile(r"\bretrieval backbone\b", re.I),
    re.compile(r"\bvector database(?: service)?\b", re.I),
)

HISTORICAL_CONTEXT_RE = re.compile(
    r"\b("
    r"historical|history|stale|superseded|supersedes|supersession|decommissioned?|deprecated|legacy|"
    r"decommission|replaced|replacement|migrated|migration|cutover|formerly|previously|earlier|"
    r"away\s+from|no\s+longer|era\s+(?:is\s+)?(?:effectively\s+)?over|"
    r"before|until|from\s+20\d\d-\d\d-\d\d\s+(?:to|until)|→|->"
    r")\b",
    re.I,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")  # noqa: RUF001 — CJK fullwidth punctuation is intentional for KR/JA/ZH sentence splits


def _connect_brain_db(db_path: Path) -> sqlite3.Connection:
    """Open brain.db with a write-friendly busy timeout and connect retry.

    The staleness job runs alongside ingest / scheduler writers. A plain
    sqlite3.connect() can fail immediately with ``database is locked`` during
    a harmless WAL writer overlap. Two layers of resilience:

      1. ``timeout=60.0`` + ``PRAGMA busy_timeout=60000`` — the connection
         itself polls for up to 60s. Raised from 30s 2026-05-19 because
         canonical_staleness_check kept tripping the scheduler failure SLO
         under 04:30-cluster contention with ingest writers.
      2. Connect-level retry — if SQLite raises ``database is locked`` while
         opening the file (different code path than statement-level
         busy_timeout) retry the connect itself with exponential backoff.
    """
    import time as _time

    last_exc: sqlite3.OperationalError | None = None
    delay = 1.0
    for _ in range(4):
        try:
            conn = sqlite3.connect(str(db_path), timeout=60.0)
            conn.execute("PRAGMA busy_timeout=60000")
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            _time.sleep(delay)
            delay = min(delay * 2.0, 8.0)
    assert last_exc is not None
    raise last_exc


@lru_cache(maxsize=8)
def load_decommissioned_terms(
    config_path: Path = DEFAULT_DECOMMISSIONED_TERMS_PATH,
) -> tuple[DecommissionedTerm, ...]:
    """Load decommissioned current-truth terms from JSON config.

    Falls back to the built-in ChromaDB → Qdrant map so the guard stays active
    even if a packaging/runtime path misses the config file.
    """
    if not config_path.exists():
        return FALLBACK_DECOMMISSIONED_TERMS
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return FALLBACK_DECOMMISSIONED_TERMS
    terms: list[DecommissionedTerm] = []
    if not isinstance(raw, list):
        return FALLBACK_DECOMMISSIONED_TERMS
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        replaced_by = str(item.get("replaced_by") or "").strip()
        decommissioned_at = str(item.get("decommissioned_at") or "").strip()
        current_doc = str(item.get("current_doc") or "").strip()
        aliases = tuple(str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip())
        if term and replaced_by and decommissioned_at and current_doc and aliases:
            terms.append(
                DecommissionedTerm(
                    term=term,
                    replaced_by=replaced_by,
                    decommissioned_at=decommissioned_at,
                    current_doc=current_doc,
                    aliases=aliases,
                )
            )
    return tuple(terms) or FALLBACK_DECOMMISSIONED_TERMS


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---json"):
        return {}, text
    try:
        metadata_text, body = text[len("---json") :].split("\n---\n", 1)
        metadata = json.loads(metadata_text)
    except (ValueError, json.JSONDecodeError):
        return {}, text
    return metadata if isinstance(metadata, dict) else {}, body


def _is_archived(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts & {"archived", "archive", "backups", "backup"})


def _is_current_file(metadata: dict[str, Any]) -> bool:
    status = str(metadata.get("status") or "").strip().lower()
    if status in {"superseded", "deprecated", "archived", "obsolete", "inactive"}:
        return False
    return not metadata.get("superseded_by")


def _has_alias(line: str, term: DecommissionedTerm) -> bool:
    return any(alias in line for alias in term.aliases)


def _term_current_claim_patterns(term: DecommissionedTerm) -> tuple[re.Pattern[str], ...]:
    # Keep legacy ChromaDB-specific phrases while allowing future terms to work
    # through generic "X is vector/current/retrieval" patterns.
    aliases = "|".join(re.escape(alias) for alias in term.aliases)
    return (
        *CURRENT_CLAIM_PATTERNS,
        re.compile(
            rf"\b(?:built on|implemented with|backed by|uses|using|paired with|alongside)\s+(?:{aliases})\b",
            re.I,
        ),
        re.compile(rf"\b(?:{aliases})-backed\b", re.I),
        re.compile(
            rf"\b(?:{aliases})\b.{{0,120}}\b(?:"
            r"backs?|stores?|serves?|runs?|hosts?|has|indexes?|reindexes?|"
            r"collections?|chunks?|vector search|retrieval|RAG|service|port|startup|hot|heavy|hit|native"
            r"|risk|stall|loop"
            r")\b",
            re.I,
        ),
        re.compile(
            rf"\b(?:"
            r"backs?|stores?|serves?|runs?|hosts?|uses?|using|indexes?|reindexes?|"
            r"reindex|paired with|alongside|heavy on|hit"
            rf")\b.{{0,120}}\b(?:{aliases})\b",
            re.I,
        ),
        re.compile(
            rf"\b(?:RAG|semantic|retrieval|memory|data-layer|capture set)\b.{{0,120}}\b(?:{aliases})\b", re.I
        ),
        re.compile(
            rf"\b(?:{aliases})\b.{{0,120}}\b(?:Ollama|canonical|graph storage|integrity|capture set)\b", re.I
        ),
    )


def _is_current_claim(window: str, term: DecommissionedTerm) -> bool:
    if not _has_alias(window, term):
        return False
    aliases = "|".join(re.escape(alias) for alias in term.aliases)
    if re.search(rf"\breplacing\s+(?:{aliases})\b", window, re.I):
        return False
    if re.search(rf"\bfrom\s+(?:{aliases})\b.{{0,80}}\bto\s+{re.escape(term.replaced_by)}\b", window, re.I):
        return False
    if HISTORICAL_CONTEXT_RE.search(window):
        return False
    return any(pattern.search(window) for pattern in _term_current_claim_patterns(term))


def find_current_truth_blockers_in_text(
    text: str,
    *,
    source: str = "",
    terms: tuple[DecommissionedTerm, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return stale current-truth blockers in arbitrary text.

    This is intentionally deterministic and phrase-based. It is used both by
    file audits and by retrieval-time filtering so stale facts are suppressed
    even if they came from non-canonical collections.
    """
    active_terms = terms or load_decommissioned_terms()
    lines = (text or "").splitlines()
    candidates: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        for term in active_terms:
            if not _has_alias(line, term):
                continue
            sentence_windows = [
                sentence.strip()
                for sentence in _SENTENCE_SPLIT_RE.split(line)
                if sentence.strip() and _has_alias(sentence, term)
            ]
            if not sentence_windows:
                start = max(0, idx - 1)
                end = min(len(lines), idx + 2)
                sentence_windows = [" ".join(lines[start:end])]
            if not any(_is_current_claim(window, term) for window in sentence_windows):
                continue
            candidates.append(
                {
                    "source": source,
                    "line": idx + 1,
                    "term": term.term,
                    "replaced_by": term.replaced_by,
                    "decommissioned_at": term.decommissioned_at,
                    "current_doc": term.current_doc,
                    "text": line.strip(),
                    "reason": "decommissioned term appears in an active current-state claim",
                }
            )
    return candidates


def _scan_file(
    path: Path, *, root: Path, terms: tuple[DecommissionedTerm, ...]
) -> tuple[list[dict[str, Any]], int]:
    metadata, body = _parse_frontmatter(path)
    if not _is_current_file(metadata):
        return [], 0
    lines = body.splitlines()
    candidates: list[dict[str, Any]] = []
    historical_mentions = 0
    for idx, line in enumerate(lines):
        for term in terms:
            if not _has_alias(line, term):
                continue
            start = max(0, idx - 1)
            end = min(len(lines), idx + 2)
            window = " ".join(lines[start:end])
            if _is_current_claim(window, term):
                candidates.append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": idx + 1,
                        "term": term.term,
                        "replaced_by": term.replaced_by,
                        "decommissioned_at": term.decommissioned_at,
                        "current_doc": term.current_doc,
                        "text": line.strip(),
                        "reason": "decommissioned term appears in an active current-state claim",
                    }
                )
            elif HISTORICAL_CONTEXT_RE.search(window):
                historical_mentions += 1
    return candidates, historical_mentions


def build_canonical_report(
    knowledge_root: Path = KNOWLEDGE_DIR,
    *,
    config_path: Path = DEFAULT_DECOMMISSIONED_TERMS_PATH,
) -> dict[str, Any]:
    terms = load_decommissioned_terms(config_path)
    canonical_root = knowledge_root / "canonical"
    candidates: list[dict[str, Any]] = []
    files_scanned = 0
    skipped_archived = 0
    historical_mentions = 0
    if canonical_root.exists():
        for path in sorted(canonical_root.rglob("*.md")):
            if _is_archived(path):
                skipped_archived += 1
                continue
            files_scanned += 1
            file_candidates, file_historical_mentions = _scan_file(path, root=knowledge_root, terms=terms)
            candidates.extend(file_candidates)
            historical_mentions += file_historical_mentions
    return {
        "passed": not candidates,
        "knowledge_root": str(knowledge_root),
        "config_path": str(config_path),
        "files_scanned": files_scanned,
        "skipped_archived": skipped_archived,
        "historical_mentions_allowed": historical_mentions,
        "decommissioned_terms": [
            {
                "term": term.term,
                "replaced_by": term.replaced_by,
                "decommissioned_at": term.decommissioned_at,
                "current_doc": term.current_doc,
            }
            for term in terms
        ],
        "blocker_count": len(candidates),
        "blockers": candidates,
    }


def _point_text(point: Any) -> str:
    doc = getattr(point, "document", None)
    if doc:
        return str(doc)
    payload = getattr(point, "payload", None) or {}
    return str(payload.get("content") or payload.get("text") or "")


def build_vector_report(
    *,
    collections: tuple[str, ...] = (
        "semantic_memory",
        "canonical",
        "experience",
        "knowledge",
        "personal",
        "obsidian",
    ),
    config_path: Path = DEFAULT_DECOMMISSIONED_TERMS_PATH,
    limit_per_collection: int = 200_000,
    apply: bool = False,
) -> dict[str, Any]:
    """Scan Qdrant collections for stale current-truth claims.

    When ``apply`` is true, stale points are not deleted. They are marked with
    ``superseded_by`` / ``valid_until`` / ``memory_class=obsolete`` so normal
    recall hygiene can suppress them while preserving audit history.
    """
    terms = load_decommissioned_terms(config_path)
    try:
        from vector_store import get_vector_store
    except ImportError as exc:
        return {"available": False, "reason": str(exc), "passed": False}

    store = get_vector_store()
    available = set(store.list_collections())
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    blockers: list[dict[str, Any]] = []
    scanned: dict[str, int] = {}
    marked: dict[str, int] = {}
    skipped: list[str] = []
    for collection in collections:
        if collection not in available:
            skipped.append(collection)
            continue
        points = store.get(
            collection,
            limit=limit_per_collection,
            with_payload=True,
            with_documents=True,
            with_vectors=False,
        )
        scanned[collection] = len(points)
        ids_to_mark: list[str] = []
        for point in points:
            payload = getattr(point, "payload", None) or {}
            if payload.get("superseded_by") or payload.get("memory_class") == "obsolete":
                continue
            source = str(payload.get("source") or getattr(point, "id", ""))
            point_blockers = find_current_truth_blockers_in_text(
                _point_text(point), source=source, terms=terms
            )
            if not point_blockers:
                continue
            for blocker in point_blockers:
                blockers.append(
                    {
                        **blocker,
                        "collection": collection,
                        "id": getattr(point, "id", ""),
                    }
                )
            ids_to_mark.append(getattr(point, "id", ""))
        if apply and ids_to_mark:
            store.update_payload(
                collection,
                ids=ids_to_mark,
                patch={
                    "superseded_by": f"stale_current_truth:{now}",
                    "valid_until": now,
                    "memory_class": "obsolete",
                    "stale_current_truth": True,
                    "stale_current_truth_reason": "decommissioned term used as active current-state claim",
                },
            )
            marked[collection] = len(ids_to_mark)
        else:
            marked[collection] = 0
    return {
        "available": True,
        "passed": not blockers,
        "apply": apply,
        "config_path": str(config_path),
        "collections": list(collections),
        "collections_scanned": scanned,
        "collections_skipped": skipped,
        "marked": marked,
        "decommissioned_terms": [
            {
                "term": term.term,
                "replaced_by": term.replaced_by,
                "decommissioned_at": term.decommissioned_at,
                "current_doc": term.current_doc,
            }
            for term in terms
        ],
        "blocker_count": len(blockers),
        "blockers": blockers[:200],
    }


def _active_atom_rows(db_path: Path) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    conn = _connect_brain_db(db_path)
    try:
        return list(
            conn.execute(
                """
                SELECT id, chroma_id, text, kind, tier, confidence, trust_score, superseded_by, valid_until
                FROM atoms
                WHERE COALESCE(tier, '') != 'obsolete'
                  AND COALESCE(superseded_by, '') = ''
                  AND (valid_until IS NULL OR valid_until = '')
                """
            )
        )
    finally:
        conn.close()


def _superseded_atoms_missing_valid_until(db_path: Path) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    conn = _connect_brain_db(db_path)
    try:
        return list(
            conn.execute(
                """
                SELECT id, chroma_id, text, tier, kind, superseded_by, updated_at
                FROM atoms
                WHERE COALESCE(superseded_by, '') != ''
                  AND (valid_until IS NULL OR valid_until = '')
                """
            )
        )
    finally:
        conn.close()


def _collection_from_chroma_id(chroma_id: str) -> str:
    collection, _, _ = chroma_id.partition(":")
    return collection or "semantic_memory"


def _low_confidence_decommissioned_mentions(
    text: str,
    *,
    source: str,
    terms: tuple[DecommissionedTerm, ...],
) -> list[dict[str, Any]]:
    """Flag low-trust speculative atoms that mention decommissioned systems.

    This is deliberately narrower than current-claim detection: it is used only
    for conjectural/low-confidence atoms. Historical context is still allowed.
    """
    candidates: list[dict[str, Any]] = []
    for idx, line in enumerate((text or "").splitlines()):
        if HISTORICAL_CONTEXT_RE.search(line):
            continue
        for term in terms:
            if not _has_alias(line, term):
                continue
            candidates.append(
                {
                    "source": source,
                    "line": idx + 1,
                    "term": term.term,
                    "replaced_by": term.replaced_by,
                    "decommissioned_at": term.decommissioned_at,
                    "current_doc": term.current_doc,
                    "text": line.strip(),
                    "reason": "low-confidence/provisional atom mentions a decommissioned term",
                }
            )
    return candidates


def build_atoms_report(
    *,
    db_path: Path = BRAIN_DB,
    config_path: Path = DEFAULT_DECOMMISSIONED_TERMS_PATH,
    apply: bool = False,
    mirror_vector: bool = True,
) -> dict[str, Any]:
    """Scan the atoms truth layer for stale active-current claims.

    Vector payload cleanup alone is not enough because ``brain_doubt`` and
    atoms-backed recall read ``logs/brain.db`` directly. This audit mirrors the
    same replacement semantics into the truth layer: stale current-state atoms
    are marked obsolete/superseded, while historical mentions remain intact.
    """
    terms = load_decommissioned_terms(config_path)
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    blockers: list[dict[str, Any]] = []
    rows = _active_atom_rows(db_path)
    superseded_lifecycle_gaps = _superseded_atoms_missing_valid_until(db_path)
    ids_to_mark: list[str] = []
    chroma_ids_to_mark: list[str] = []
    for row in rows:
        source = str(row["chroma_id"] or row["id"])
        row_blockers = find_current_truth_blockers_in_text(
            str(row["text"] or ""),
            source=source,
            terms=terms,
        )
        if not row_blockers:
            try:
                confidence = float(row["confidence"] if row["confidence"] is not None else 0.5)
            except (TypeError, ValueError):
                confidence = 0.5
            try:
                trust_score = float(row["trust_score"] if row["trust_score"] is not None else 0.5)
            except (TypeError, ValueError):
                trust_score = 0.5
            low_trust = confidence < 0.45 or trust_score < 0.45
            speculative = str(row["kind"] or "").lower() == "conjecture"
            if speculative or low_trust:
                row_blockers = _low_confidence_decommissioned_mentions(
                    str(row["text"] or ""),
                    source=source,
                    terms=terms,
                )
        if not row_blockers:
            continue
        ids_to_mark.append(str(row["id"]))
        chroma_id = str(row["chroma_id"] or "")
        if chroma_id:
            chroma_ids_to_mark.append(chroma_id)
        for blocker in row_blockers:
            blockers.append(
                {
                    **blocker,
                    "atom_id": row["id"],
                    "chroma_id": chroma_id,
                    "kind": row["kind"],
                    "tier": row["tier"],
                    "confidence": row["confidence"],
                    "trust_score": row["trust_score"],
                }
            )
    # wal_checkpoint_intraday (cron :35) holds an EXCLUSIVE lock ~20s on
    # brain.db. Use retrying_transaction so the maintenance pass waits the
    # checkpoint out instead of failing the daily run.
    from db import retrying_transaction

    marked_atoms = 0
    marked_vectors: dict[str, int] = {}
    if apply and ids_to_mark and db_path.exists():
        conn = _connect_brain_db(db_path)
        try:
            with retrying_transaction(conn):
                conn.executemany(
                    """
                    UPDATE atoms
                       SET tier = 'obsolete',
                           superseded_by = ?,
                           valid_until = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    [(f"stale_current_truth:{now}", now, now, atom_id) for atom_id in ids_to_mark],
                )
                marked_atoms = conn.total_changes
        finally:
            conn.close()
    repaired_superseded_valid_until = 0
    if apply and superseded_lifecycle_gaps and db_path.exists():
        conn = _connect_brain_db(db_path)
        try:
            with retrying_transaction(conn):
                conn.executemany(
                    """
                    UPDATE atoms
                       SET valid_until = COALESCE(NULLIF(updated_at, ''), ?),
                           updated_at = ?
                     WHERE id = ?
                       AND COALESCE(superseded_by, '') != ''
                       AND (valid_until IS NULL OR valid_until = '')
                    """,
                    [(now, now, str(row["id"])) for row in superseded_lifecycle_gaps],
                )
                repaired_superseded_valid_until = conn.total_changes
        finally:
            conn.close()
    if apply and mirror_vector and chroma_ids_to_mark:
        try:
            from vector_store import get_vector_store

            store = get_vector_store()
            available_collections = set(store.list_collections())
            by_collection: dict[str, list[str]] = {}
            for chroma_id in chroma_ids_to_mark:
                collection = _collection_from_chroma_id(chroma_id)
                if collection in available_collections:
                    by_collection.setdefault(collection, []).append(chroma_id)
            patch = {
                "superseded_by": f"stale_current_truth:{now}",
                "valid_until": now,
                "memory_class": "obsolete",
                "stale_current_truth": True,
                "stale_current_truth_reason": "decommissioned term used as active current-state atom",
            }
            for collection, ids in by_collection.items():
                store.update_payload(collection, ids=ids, patch=patch)
                marked_vectors[collection] = len(ids)
        except Exception as exc:
            marked_vectors["error"] = str(exc)[:300]  # type: ignore[assignment]
    return {
        "available": db_path.exists(),
        "passed": not blockers,
        "apply": apply,
        "db_path": str(db_path),
        "config_path": str(config_path),
        "atoms_scanned": len(rows),
        "marked_atoms": marked_atoms,
        "marked_vectors": marked_vectors,
        "superseded_valid_until_missing": len(superseded_lifecycle_gaps),
        "repaired_superseded_valid_until": repaired_superseded_valid_until,
        "decommissioned_terms": [
            {
                "term": term.term,
                "replaced_by": term.replaced_by,
                "decommissioned_at": term.decommissioned_at,
                "current_doc": term.current_doc,
            }
            for term in terms
        ],
        "blocker_count": len(blockers),
        "blockers": blockers[:200],
    }


def build_report(
    knowledge_root: Path = KNOWLEDGE_DIR,
    *,
    config_path: Path = DEFAULT_DECOMMISSIONED_TERMS_PATH,
) -> dict[str, Any]:
    """Backward-compatible canonical-file report entry point."""
    return build_canonical_report(knowledge_root, config_path=config_path)
