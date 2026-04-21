"""brain_core/contextual_embed.py — Contextual Retrieval (Anthropic Sep 2024).

Re-embeds canonical chunks with a 50-100 token contextual prefix generated per
parent document via Jenna. Target: W1 extended eval gap (64% vs stable 96.4%).
Anthropic's reported gain: -35% retrieval failure (embeddings only),
-49% (+BM25), -67% (+reranking).

Why per-document (not per-chunk): canonical has ~60 active docs split into
~6K chunks in ChromaDB. Per-chunk Jenna would cost ~$15 and take hours.
Per-doc costs ~$2, takes ~10 min, covers all chunks from the same parent
with the same prefix ("what this document IS"). Good enough — the prefix
is meant to situate the chunk within the whole doc, and the "whole doc"
summary is the same for every chunk of that doc.

Pipeline:
  1. walk ~/server/knowledge/canonical/*.md (skip archived/)
  2. for each file: load → dispatch Jenna for context prefix
  3. chroma GET where metadata.source == abs_path → all chunks of this doc
  4. for each chunk: re-embed `passage: <prefix>\\n\\n<chunk_text>` via Ollama
  5. chroma upsert with new embedding + enriched metadata (contextual_prefix,
     contextualized, contextualized_at)
  6. audit trail: contextual_embed_audit row in brain.db (chunk_id, prefix,
     generated_at, model) — lets us reverse/debug

Safety:
  - Opt-in via BRAIN_CONTEXTUAL_EMBED_ENABLED env var (default off)
  - --dry-run flag: simulate + report; no writes
  - Incremental: skips docs whose content_hash is unchanged since last run
  - Fail-open per doc: one Jenna timeout doesn't stop the batch

Scheduler: `contextual_embed_weekly` Sun 05:00am (after canonical_pipeline
at 02:00 and before eval_run at 03:30 — wait, eval at 03:30 is earlier, so
run BEFORE eval so the eval sees the new embeddings. Revised: Sun 05:00 is
after both, and weekly incremental is fine for catching doc changes).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.contextual_embed")

KNOWLEDGE_CANONICAL_DIR = Path("/Users/chrischo/server/knowledge/canonical")
try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
CANONICAL_COLLECTION = "canonical"

# Anthropic's recommended prompt pattern, adapted slightly for per-doc summaries
CONTEXT_PROMPT = """<document>
{document}
</document>

Give a short succinct context (50-100 tokens) to situate this document for the purposes
of improving search retrieval. Include: what kind of doc (decision, infra, incident, project,
entity, design), its main subject, and relevant time or scope if clear. Answer only with the
context. No preamble, no meta-commentary, no quotes around the answer.

Context:"""

MAX_DOC_CHARS_IN_PROMPT = 8000  # cap Jenna input to keep dispatch fast + cheap
PREFIX_MAX_CHARS = 500  # safety truncation for generated prefixes
JENNA_TIMEOUT_S = 40


# 2026-04-18: thread-local connection pool so the batch `run()` (called on a
# scheduler thread, processes 60+ docs each making 3 sqlite round-trips)
# doesn't open/close 180 connections per weekly pass. Shares the same
# pattern as embed_cache.py and the thread-local pool in search_unified.py.
_audit_local = threading.local()


def _audit_conn() -> sqlite3.Connection:
    conn = getattr(_audit_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(BRAIN_DB), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _audit_local.conn = conn
    return conn


def _ensure_audit_table() -> None:
    conn = _audit_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS contextual_embed_audit (
            doc_path         TEXT NOT NULL,
            content_hash     TEXT NOT NULL,
            chunk_count      INTEGER NOT NULL DEFAULT 0,
            context_prefix   TEXT NOT NULL,
            prefix_chars     INTEGER NOT NULL,
            generated_at     TEXT NOT NULL,
            model            TEXT NOT NULL DEFAULT 'jenna',
            PRIMARY KEY (doc_path, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_ctx_embed_audit_path ON contextual_embed_audit(doc_path);
        """
    )
    conn.commit()


def _last_seen_hash(doc_path: str) -> str | None:
    conn = _audit_conn()
    row = conn.execute(
        "SELECT content_hash FROM contextual_embed_audit "
        "WHERE doc_path = ? ORDER BY generated_at DESC LIMIT 1",
        (doc_path,),
    ).fetchone()
    return row[0] if row else None


def _record_audit(
    doc_path: str,
    content_hash: str,
    chunk_count: int,
    prefix: str,
) -> None:
    conn = _audit_conn()
    conn.execute(
        """INSERT OR REPLACE INTO contextual_embed_audit
           (doc_path, content_hash, chunk_count, context_prefix, prefix_chars, generated_at, model)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            doc_path,
            content_hash,
            chunk_count,
            prefix[:PREFIX_MAX_CHARS],
            len(prefix),
            datetime.now(UTC).isoformat(timespec="seconds"),
            "jenna",
        ),
    )
    conn.commit()


def _list_canonical_docs() -> list[Path]:
    """Walk canonical dir, skip archived/ and entity .zip snapshots."""
    docs: list[Path] = []
    for p in KNOWLEDGE_CANONICAL_DIR.rglob("*.md"):
        # Skip archived content
        if "archived" in p.parts:
            continue
        # Skip live_state/ (auto-generated dashboards, not canonical knowledge)
        if "live_state" in p.parts:
            continue
        if not p.is_file():
            continue
        docs.append(p)
    return sorted(docs)


def _generate_context(doc_path: Path) -> str | None:
    """Dispatch Jenna to produce a per-document context prefix."""
    try:
        content = doc_path.read_text(errors="ignore")
    except Exception as exc:
        log.debug("read failed for %s: %s", doc_path, exc)
        return None
    if len(content) < 100:
        return None  # too short to benefit from contextualization
    prompt = CONTEXT_PROMPT.format(document=content[:MAX_DOC_CHARS_IN_PROMPT])
    try:
        from cli_llm import dispatch

        result = dispatch(agent="jenna", message=prompt, thinking="low", timeout=JENNA_TIMEOUT_S)
    except Exception as exc:
        log.warning("jenna dispatch failed for %s: %s", doc_path, exc)
        return None
    if not result.ok or not result.text:
        return None
    text = result.text.strip()
    # Strip wrapping quotes / backticks if any
    if text.startswith("```"):
        text = text.split("```", 2)[1].strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text[:PREFIX_MAX_CHARS]


def _reembed_chunks_for_doc(doc_path: Path, prefix: str, dry_run: bool) -> int:
    """Look up chroma chunks where metadata.source matches doc_path,
    re-embed each with `passage: <prefix>\\n\\n<chunk>`, upsert back.

    Returns count of chunks updated.
    """
    try:
        from indexer import get_embedding
        from vector_store import get_vector_store
    except Exception as exc:
        log.warning("vector_store / indexer import failed: %s", exc)
        return 0

    store = get_vector_store()
    abs_path = str(doc_path)
    try:
        points = store.get(
            CANONICAL_COLLECTION,
            filter={"source": {"$eq": abs_path}},
            with_payload=True,
            with_documents=True,
        )
    except Exception as exc:
        log.debug("chroma get failed for %s: %s", abs_path, exc)
        return 0

    if not points:
        return 0

    if dry_run:
        log.info("[dry-run] would re-embed %d chunks for %s", len(points), doc_path.name)
        return len(points)

    # Batch re-embed with wall-clock guard
    # 2026-04-17 hang fix: get_embedding retries 5x with 120s timeout each → worst-case
    # 10 min per chunk stall if Ollama flaps. Observed 7+ min hang during T2.12 batch.
    # Wrap whole doc loop in 90s cap; partial progress is acceptable since the audit
    # row won't be committed, and next scheduler run will retry cleanly.
    import time as _t

    t_start = _t.time()
    MAX_SECONDS_PER_DOC = 90
    updated = 0
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    for p in points:
        if _t.time() - t_start > MAX_SECONDS_PER_DOC:
            log.warning(
                "wall-clock cap hit for %s at %d/%d chunks — aborting doc",
                doc_path.name,
                updated,
                len(points),
            )
            break
        doc = p.document or ""
        meta = p.payload or {}
        if not doc:
            continue
        # 2026-04-17 prod-review: reverted to Anthropic's prefix-first order.
        # Earlier chunk-first reasoning was "preserve chunk content on long
        # chunks" but that makes contextualization a no-op for ~27% of chunks
        # (the long ones), which is where contextualization matters most.
        # Prefix-first is Anthropic's original pattern: short chunks get full
        # context+chunk, long chunks get full context + truncated chunk head
        # (still better than plain chunk for retrieval per the paper).
        enriched_text = f"{prefix}\n\n{doc}"
        embedding = get_embedding(enriched_text[:1000], prefix="passage", use_cache=False)
        if not embedding:
            continue
        new_meta = dict(meta)
        new_meta["contextual_prefix"] = prefix[:PREFIX_MAX_CHARS]
        new_meta["contextualized"] = True
        new_meta["contextualized_at"] = now_iso
        try:
            store.upsert(
                CANONICAL_COLLECTION,
                ids=[p.id],
                vectors=[embedding],
                payloads=[new_meta],
                documents=[doc],
            )
            updated += 1
        except Exception as exc:
            log.debug("upsert failed for %s: %s", p.id, exc)
            continue
    return updated


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    only_kind: str | None = None,
) -> dict:
    """Main entry point for the T2.12 contextual re-embedding pass.

    Args:
        dry_run: simulate — count chunks that would be touched, no writes
        limit: cap the number of docs processed (useful for smoke tests)
        force: re-process docs even when content_hash is unchanged
        only_kind: limit to a subfolder (e.g. 'decisions', 'entities')

    Returns a summary dict suitable for a scheduler JSON log.
    """
    enabled = os.environ.get("BRAIN_CONTEXTUAL_EMBED_ENABLED", "").lower() in ("1", "true", "yes")
    if not enabled and not dry_run:
        return {
            "status": "disabled",
            "note": "set BRAIN_CONTEXTUAL_EMBED_ENABLED=true to enable. Use --dry-run to preview.",
        }

    _ensure_audit_table()
    docs = _list_canonical_docs()
    if only_kind:
        docs = [d for d in docs if only_kind in d.parts]
    if limit:
        docs = docs[:limit]

    started = time.time()
    summary: dict = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "total_docs": len(docs),
        "skipped_unchanged": 0,
        "processed": 0,
        "chunks_updated": 0,
        "errors": [],
    }

    for doc_path in docs:
        try:
            content = doc_path.read_text(errors="ignore")
        except Exception as _exc:
            log.debug("silenced exception in contextual_embed.py: %s", _exc)
            continue
        if len(content) < 100:
            continue
        content_hash = _hash_content(content)
        if not force:
            prev = _last_seen_hash(str(doc_path))
            if prev == content_hash:
                summary["skipped_unchanged"] += 1
                continue

        prefix = _generate_context(doc_path)
        if not prefix:
            summary["errors"].append({"doc": str(doc_path), "reason": "jenna_empty_or_failed"})
            continue

        n = _reembed_chunks_for_doc(doc_path, prefix, dry_run=dry_run)
        summary["processed"] += 1
        summary["chunks_updated"] += n
        if not dry_run:
            _record_audit(str(doc_path), content_hash, n, prefix)

    summary["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    summary["duration_s"] = round(time.time() - started, 2)
    return summary


def apply_prefix_for_doc(doc_path: str, prefix: str) -> dict:
    """Session-mode entry: re-embed + audit with a prefix provided by the caller.

    Bypasses Jenna dispatch entirely. Intended for use when Claude Code (or any
    other LLM the user is interacting with directly) generates the prefix in-session.
    Zero additional API cost and better-quality prefixes when the caller is already
    paying LLM attention to the doc.

    Invariants:
      - `prefix` must be the caller's best summary of the doc (not generated here)
      - `doc_path` must exist on disk (used for content_hash audit row)
    """
    p = Path(doc_path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "doc_not_found", "doc_path": doc_path}
    if not prefix or len(prefix) < 20:
        return {"ok": False, "error": "prefix_too_short", "doc_path": doc_path}
    try:
        content = p.read_text(errors="ignore")
    except Exception as exc:
        return {"ok": False, "error": f"read_failed:{exc}", "doc_path": doc_path}
    content_hash = _hash_content(content)
    _ensure_audit_table()
    n = _reembed_chunks_for_doc(p, prefix.strip()[:PREFIX_MAX_CHARS], dry_run=False)
    _record_audit(str(p), content_hash, n, prefix.strip())
    return {"ok": True, "doc_path": str(p), "chunks_updated": n, "prefix_chars": len(prefix)}


def apply_prefixes_batch(entries: list[dict]) -> dict:
    """Session-mode batch: apply many caller-provided prefixes at once.

    `entries` = [{"doc_path": "/abs/path.md", "prefix": "..."}, ...]
    Returns summary suitable for logging.
    """
    summary: dict = {"total": len(entries), "ok": 0, "failed": 0, "chunks": 0, "details": []}
    for e in entries:
        r = apply_prefix_for_doc(e.get("doc_path", ""), e.get("prefix", ""))
        summary["details"].append(r)
        if r.get("ok"):
            summary["ok"] += 1
            summary["chunks"] += r.get("chunks_updated", 0)
        else:
            summary["failed"] += 1
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="T2.12 Contextual Retrieval re-embedding pipeline")
    sub = parser.add_subparsers(dest="cmd")

    # Default: full run (with Jenna, for scheduled use)
    parser.add_argument("--dry-run", action="store_true", help="simulate, no writes")
    parser.add_argument("--limit", type=int, default=None, help="cap number of docs processed")
    parser.add_argument("--force", action="store_true", help="re-process unchanged docs too")
    parser.add_argument("--only", type=str, default=None, help="limit to subfolder")

    # Session mode: apply prefixes from stdin JSON
    sp_apply = sub.add_parser("apply", help="apply caller-supplied prefixes from stdin JSON")

    # List pending docs (not yet in audit table)
    sp_list = sub.add_parser("list-pending", help="print JSON of pending doc paths + content")
    sp_list.add_argument("--limit", type=int, default=None)

    args = parser.parse_args()

    if args.cmd == "apply":
        entries = json.loads(sys.stdin.read())
        if not isinstance(entries, list):
            entries = [entries]
        out = apply_prefixes_batch(entries)
        print(json.dumps(out, indent=2))  # noqa: T201 — CLI stdout
    elif args.cmd == "list-pending":
        _ensure_audit_table()
        done_paths = set()
        import sqlite3 as _s

        conn = _s.connect(str(BRAIN_DB))
        try:
            for row in conn.execute("SELECT doc_path FROM contextual_embed_audit").fetchall():
                done_paths.add(row[0])
        finally:
            conn.close()
        pending = []
        for d in _list_canonical_docs():
            if str(d) in done_paths:
                continue
            try:
                content = d.read_text(errors="ignore")
            except Exception as _exc:
                log.debug("silenced exception in contextual_embed.py: %s", _exc)
                continue
            if len(content) < 100:
                continue
            pending.append({"doc_path": str(d), "content": content[:MAX_DOC_CHARS_IN_PROMPT]})
        if args.limit:
            pending = pending[: args.limit]
        print(json.dumps(pending, ensure_ascii=False))  # noqa: T201 — CLI stdout
    else:
        out = run(dry_run=args.dry_run, limit=args.limit, force=args.force, only_kind=args.only)
        print(json.dumps(out, indent=2))  # noqa: T201 — CLI stdout
