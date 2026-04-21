#!/usr/bin/env python3
"""ingest/pdfs.py — PDF ingestion via Docling for Phase M7-WS2a.

Scans a configured directory of PDF files, parses each via the Docling
DocumentConverter, chunks the resulting markdown, embeds via Ollama, and
upserts the chunks into the `knowledge` ChromaDB collection with
`source=pdf/<sha256>`.

Hash-based dedupe: every PDF is keyed by its content SHA-256. Already-
ingested PDFs are skipped on subsequent runs. State lives in
logs/pdf-ingest-state.json (one per file: hash → ingested_at, chunk_count).

Configurable via env vars:
  BRAIN_PDF_INGEST_DIR        — root scan dir (default ~/Documents/PDFs)
  BRAIN_PDF_INGEST_DISABLED   — kill switch (any value)
  BRAIN_PDF_MAX_PAGES         — per-doc page cap (default 100; 0 = no cap)

Wired into the scheduler as `pdf_ingest` daily at 05:30am off-hours.

CLI:
  ingest/pdfs.py                          # scan the default dir
  ingest/pdfs.py --dir /path/to/pdfs      # one-shot from arbitrary dir
  ingest/pdfs.py --file /path/single.pdf  # single-file mode
  ingest/pdfs.py --reingest               # force re-ingest (ignore dedupe)
  ingest/pdfs.py --dry-run                # parse but don't write to Chroma
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Reuse helpers from brain_core.indexer (single source of truth).
sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from indexer import (
    chunk_text,
    get_embedding,
)
from vector_store import get_vector_store

# M8.2: optional semantic chunking. Module-level kill switch via
# BRAIN_SEMANTIC_CHUNKING env var. Falls back to indexer.chunk_text otherwise.
try:
    from semantic_chunk import chunk_with_fallback
except Exception:
    chunk_with_fallback = None  # type: ignore[assignment]

log = logging.getLogger("brain.ingest.pdfs")

KNOWLEDGE_COLLECTION = "knowledge"
DEFAULT_PDF_DIR = Path.home() / "Documents" / "PDFs"
STATE_FILE = Path("/Users/chrischo/server/brain/logs/pdf-ingest-state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/pdf-ingest-failures.jsonl")
MAX_PAGES_DEFAULT = 100
CHUNK_SIZE = 1000  # characters
EMBED_TRUNCATE = 1000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _log_failure(pdf_path: str, error: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(
                json.dumps(
                    {"timestamp": _now_iso(), "pdf": pdf_path, "error": str(error)[:500]},
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# M8 follow-up: DocumentConverter loads ~1GB of layout / table / OCR models
# on first instantiation. Reusing one instance across all PDFs in a run cuts
# peak memory ~50% (was instantiating per-file in earlier draft) and load
# time ~6-8s per extra PDF. Module-level so subprocess invocations get a
# single load + many uses.
_converter_singleton = None


def _get_converter():  # noqa: ANN202 — Docling type not in our type system
    """Lazy-construct (and reuse) the Docling DocumentConverter for this run."""
    global _converter_singleton
    if _converter_singleton is None:
        from docling.document_converter import DocumentConverter

        _converter_singleton = DocumentConverter()
    return _converter_singleton


def _parse_pdf_to_markdown(pdf_path: Path, max_pages: int = MAX_PAGES_DEFAULT) -> tuple[str, dict]:
    """Run Docling on a single PDF, return (markdown_text, metadata).

    Uses the module-level singleton so peak memory across an N-PDF run is
    O(1) Docling instances instead of O(N).
    """
    t0 = time.time()
    converter = _get_converter()
    result = converter.convert(str(pdf_path))
    doc = result.document

    markdown = doc.export_to_markdown()
    page_count = len(doc.pages) if hasattr(doc, "pages") else 0

    meta = {
        "filename": pdf_path.name,
        "size_bytes": pdf_path.stat().st_size,
        "page_count": page_count,
        "parse_ms": int((time.time() - t0) * 1000),
    }

    if max_pages > 0 and page_count > max_pages:
        # Cap at max_pages worth of markdown — rough proxy via line count
        lines = markdown.splitlines()
        line_cap = max_pages * 50  # ~50 lines/page average
        markdown = "\n".join(lines[:line_cap])
        meta["truncated"] = True

    return markdown, meta


def _chunk_markdown(markdown: str, source: str) -> list[dict]:
    """Chunk a markdown document. Uses semantic chunking when
    BRAIN_SEMANTIC_CHUNKING=1, falls back to indexer.chunk_text otherwise.

    Returns list of `{"content": str, "metadata": dict}` records.
    """
    if chunk_with_fallback is not None:
        raw_chunks = chunk_with_fallback(markdown, max_size=CHUNK_SIZE)
    else:
        raw_chunks = chunk_text(markdown, max_size=CHUNK_SIZE)
    out: list[dict] = []
    for i, raw in enumerate(raw_chunks):
        if isinstance(raw, dict):
            content_str = raw.get("content", "")
            section = raw.get("section", "")
            chunk_id = raw.get("chunk_id", "")
            parent_id = raw.get("parent_id")
            is_parent = raw.get("is_parent", False)
        else:
            content_str = str(raw)
            section = ""
            chunk_id = ""
            parent_id = None
            is_parent = False
        if not content_str.strip():
            continue
        out.append(
            {
                "content": content_str,
                "metadata": {
                    "source": source,
                    "source_type": "pdf",
                    "chunk_index": i,
                    "total_chunks": len(raw_chunks),
                    "section": section,
                    "chunk_id": chunk_id,
                    "parent_id": parent_id,
                    "is_parent": is_parent,
                },
            }
        )
    return out


def _upsert_chunks(chunks: list[dict], pdf_hash: str) -> int:
    """Batch-upsert chunks into knowledge collection. Returns count written."""
    if not chunks:
        return 0

    store = get_vector_store()
    store.create_collection(KNOWLEDGE_COLLECTION)

    ids: list[str] = []
    embeddings: list[list[float]] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    now_iso = _now_iso()

    for chunk in chunks:
        content = chunk["content"]
        if not content.strip():
            continue
        emb = get_embedding(content[:EMBED_TRUNCATE])
        if not emb:
            continue
        chunk_id = f"pdf:{pdf_hash}:{chunk['metadata']['chunk_index']:04d}"
        meta = dict(chunk["metadata"])
        meta["pdf_hash"] = pdf_hash
        meta["created_at"] = now_iso
        meta["embed_model"] = "multilingual-e5-large-instruct"

        ids.append(chunk_id)
        embeddings.append(emb)
        documents.append(content)
        metadatas.append(meta)

    if not ids:
        return 0

    store.upsert(
        KNOWLEDGE_COLLECTION,
        ids=ids,
        vectors=embeddings,
        documents=documents,
        payloads=metadatas,
    )
    return len(ids)


def ingest_pdf(
    pdf_path: Path,
    *,
    state: dict,
    reingest: bool = False,
    dry_run: bool = False,
    max_pages: int = MAX_PAGES_DEFAULT,
) -> dict:
    """Ingest one PDF; updates state in-place. Returns per-file result dict."""
    if not pdf_path.exists():
        return {"path": str(pdf_path), "status": "missing"}
    if pdf_path.suffix.lower() != ".pdf":
        return {"path": str(pdf_path), "status": "not_pdf"}

    pdf_hash = _hash_file(pdf_path)
    if not reingest and pdf_hash in state:
        return {
            "path": str(pdf_path),
            "hash": pdf_hash,
            "status": "skipped_dedupe",
            "previously_ingested_at": state[pdf_hash].get("ingested_at"),
        }

    try:
        markdown, meta = _parse_pdf_to_markdown(pdf_path, max_pages=max_pages)
    except Exception as e:
        log.warning("docling parse failed for %s: %s", pdf_path.name, e)
        _log_failure(str(pdf_path), f"parse: {e}")
        return {"path": str(pdf_path), "status": "parse_failed", "error": str(e)[:200]}

    if not markdown.strip():
        return {"path": str(pdf_path), "hash": pdf_hash, "status": "empty"}

    chunks = _chunk_markdown(markdown, source=f"pdf/{pdf_path.name}")

    if dry_run:
        return {
            "path": str(pdf_path),
            "hash": pdf_hash,
            "status": "dry_run",
            "chunks": len(chunks),
            "parse_ms": meta["parse_ms"],
            "page_count": meta.get("page_count"),
        }

    try:
        written = _upsert_chunks(chunks, pdf_hash)
    except Exception as e:
        log.warning("chroma upsert failed for %s: %s", pdf_path.name, e)
        _log_failure(str(pdf_path), f"upsert: {e}")
        return {"path": str(pdf_path), "status": "upsert_failed", "error": str(e)[:200]}

    state[pdf_hash] = {
        "filename": pdf_path.name,
        "ingested_at": _now_iso(),
        "chunk_count": written,
        "page_count": meta.get("page_count"),
        "parse_ms": meta["parse_ms"],
    }

    return {
        "path": str(pdf_path),
        "hash": pdf_hash,
        "status": "ingested",
        "chunks": written,
        "parse_ms": meta["parse_ms"],
        "page_count": meta.get("page_count"),
    }


def ingest_directory(
    pdf_dir: Path,
    *,
    state: dict,
    reingest: bool = False,
    dry_run: bool = False,
    max_pages: int = MAX_PAGES_DEFAULT,
) -> list[dict]:
    """Walk a directory recursively for *.pdf files and ingest each."""
    if not pdf_dir.exists():
        log.info("pdf dir does not exist, skipping: %s", pdf_dir)
        return []

    pdfs = sorted(pdf_dir.rglob("*.pdf")) + sorted(pdf_dir.rglob("*.PDF"))
    pdfs = list(dict.fromkeys(pdfs))  # dedupe paths
    if not pdfs:
        log.info("no PDFs found in %s", pdf_dir)
        return []

    log.info("ingesting %d PDFs from %s", len(pdfs), pdf_dir)
    results = []
    for pdf in pdfs:
        result = ingest_pdf(pdf, state=state, reingest=reingest, dry_run=dry_run, max_pages=max_pages)
        results.append(result)
    return results


def run() -> dict:
    """Entrypoint for the scheduler. Reads env config, runs default dir."""
    if os.environ.get("BRAIN_PDF_INGEST_DISABLED"):
        return {"status": "disabled", "reason": "BRAIN_PDF_INGEST_DISABLED env"}

    pdf_dir = Path(os.environ.get("BRAIN_PDF_INGEST_DIR") or DEFAULT_PDF_DIR)
    max_pages = int(os.environ.get("BRAIN_PDF_MAX_PAGES") or MAX_PAGES_DEFAULT)

    state = _load_state()
    results = ingest_directory(pdf_dir, state=state, max_pages=max_pages)
    _save_state(state)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "pdf_dir": str(pdf_dir),
        "total_files": len(results),
        "by_status": counts,
        "state_size": len(state),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", type=Path, default=None)
    p.add_argument("--file", type=Path, default=None)
    p.add_argument("--reingest", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    args = p.parse_args()

    state = _load_state()

    if args.file:
        result = ingest_pdf(
            args.file,
            state=state,
            reingest=args.reingest,
            dry_run=args.dry_run,
            max_pages=args.max_pages,
        )
        if not args.dry_run:
            _save_state(state)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") in {"ingested", "skipped_dedupe", "dry_run"} else 1

    pdf_dir = args.dir or Path(os.environ.get("BRAIN_PDF_INGEST_DIR") or DEFAULT_PDF_DIR)
    results = ingest_directory(
        pdf_dir,
        state=state,
        reingest=args.reingest,
        dry_run=args.dry_run,
        max_pages=args.max_pages,
    )
    if not args.dry_run:
        _save_state(state)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print(
        json.dumps(
            {"pdf_dir": str(pdf_dir), "total_files": len(results), "by_status": counts},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
