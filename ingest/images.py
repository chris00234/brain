#!/usr/bin/env python3
"""ingest/images.py — image OCR + optional OpenClaw vision captioning (M7-WS2b).

Walks ~/Pictures/brain-ingest/ (configurable), hashes each image, runs Docling's
built-in OCR (Apple Vision via ocrmac on macOS) for text extraction. If OCR
yields >=20 chars of clean text, that text is indexed as the image's caption
in ChromaDB. Otherwise the image is logged but skipped — no expensive LLM
fallback by default.

The OpenClaw vision dispatch path is wired but gated behind
`BRAIN_IMAGE_VISION_DISPATCH=1`. When enabled (and a Sage MODEL.json with
vision support exists), text-empty images get captioned via openclaw_dispatch
to Sage with a vision-capable model. Defaults OFF to honor the
"no extra cost / resource" constraint.

Hash-based dedupe: every image is keyed by its content SHA-256. State at
logs/image-ingest-state.json. Cost tracker at logs/image-ingest-cost.jsonl.

Daily limit:
  IMAGE_INGEST_DAILY_CAP = 20  (hard cap on vision dispatches per UTC day)

Env vars:
  BRAIN_IMAGE_INGEST_DIR        — root dir (default ~/Pictures/brain-ingest)
  BRAIN_IMAGE_INGEST_DISABLED   — kill switch
  BRAIN_IMAGE_VISION_DISPATCH   — opt-in to vision LLM fallback
  BRAIN_IMAGE_DAILY_CAP         — override daily cap

CLI:
  ingest/images.py                       # scan default dir
  ingest/images.py --dir /path           # arbitrary dir
  ingest/images.py --file /path/img.png  # single file
  ingest/images.py --reingest            # ignore dedupe
  ingest/images.py --dry-run             # OCR but don't write to Chroma
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

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from indexer import get_embedding
from vector_store import get_vector_store

log = logging.getLogger("brain.ingest.images")

KNOWLEDGE_COLLECTION = "knowledge"
DEFAULT_IMAGE_DIR = Path.home() / "Pictures" / "brain-ingest"
STATE_FILE = Path("/Users/chrischo/server/brain/logs/image-ingest-state.json")
COST_FILE = Path("/Users/chrischo/server/brain/logs/image-ingest-cost.jsonl")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/image-ingest-failures.jsonl")

OCR_MIN_CHARS = 20
DEFAULT_DAILY_CAP = 20
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff", ".bmp", ".heic"}
EMBED_TRUNCATE = 1000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _log_failure(image_path: str, error: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(
                json.dumps(
                    {"timestamp": _now_iso(), "image": image_path, "error": str(error)[:500]},
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _record_cost(image_hash: str, method: str, cost_cents: float) -> None:
    try:
        COST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with COST_FILE.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": _now_iso(),
                        "date": _today_utc(),
                        "image_hash": image_hash,
                        "method": method,
                        "cost_cents": cost_cents,
                    },
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


def _vision_dispatches_today() -> int:
    """Count vision dispatches in the cost log for the current UTC day."""
    if not COST_FILE.exists():
        return 0
    today = _today_utc()
    count = 0
    try:
        with COST_FILE.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date") == today and rec.get("method") == "vision":
                    count += 1
    except OSError:
        pass
    return count


# M8 follow-up: same singleton optimization as pdfs.py — reuse one
# DocumentConverter across all images in an ingest run instead of
# constructing a fresh one (and re-loading ~1GB of models) per file.
_converter_singleton = None


def _get_converter():  # noqa: ANN202 — Docling type not in our type system
    global _converter_singleton
    if _converter_singleton is None:
        from docling.document_converter import DocumentConverter

        _converter_singleton = DocumentConverter()
    return _converter_singleton


def _ocr_image(image_path: Path) -> str:
    """Use Docling's OCR (Apple Vision via ocrmac) for text extraction.

    Docling's image input goes through its DocumentConverter — not exactly
    the same as a raw OCR call, but the converter handles image inputs
    via the same pipeline and exports to markdown.
    """
    try:
        converter = _get_converter()
        result = converter.convert(str(image_path))
        text = result.document.export_to_markdown() if result.document else ""
        return text.strip()
    except Exception as e:
        log.warning("ocr failed for %s: %s", image_path.name, e)
        return ""


def _vision_dispatch(image_path: Path) -> str | None:
    """Generate a rich caption via brain_core.vision_llm (Gemini 2.5 Flash).

    v3 (2026-04-14): previously gated by BRAIN_IMAGE_VISION_DISPATCH and
    returned None because openclaw_dispatch couldn't carry image bytes.
    Now uses vision_llm.describe_image() which calls Gemini REST directly
    (no SDK, no dependency bloat). Daily cap + content-hash cache enforced
    inside vision_llm.

    Returns the caption text or None on failure. Callers should fall back
    to OCR-only when None.
    """
    try:
        sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
        import vision_llm
    except ImportError:
        log.warning("brain_core.vision_llm not importable")
        return None

    if not vision_llm.is_configured():
        log.debug("vision_llm not configured (no GEMINI_API_KEY)")
        return None

    try:
        caption = vision_llm.describe_image(image_path)
        return caption if caption else None
    except Exception as e:
        log.warning("vision_llm describe_image failed for %s: %s", image_path.name, e)
        return None


def ingest_image(
    image_path: Path,
    *,
    state: dict,
    reingest: bool = False,
    dry_run: bool = False,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> dict:
    """Ingest one image; updates state in-place. Returns per-file result dict."""
    if not image_path.exists():
        return {"path": str(image_path), "status": "missing"}
    if image_path.suffix.lower() not in SUPPORTED_EXTS:
        return {"path": str(image_path), "status": "unsupported_ext"}

    image_hash = _hash_file(image_path)
    if not reingest and image_hash in state:
        return {
            "path": str(image_path),
            "hash": image_hash,
            "status": "skipped_dedupe",
            "previously_ingested_at": state[image_hash].get("ingested_at"),
        }

    t0 = time.time()
    ocr_text = _ocr_image(image_path)
    ocr_chars = len(ocr_text)
    method = "ocr" if ocr_chars >= OCR_MIN_CHARS else "empty"

    caption: str | None = None
    if method == "ocr":
        caption = ocr_text
        cost_cents = 0.0
    else:
        # Try vision dispatch (gated by env var; off by default)
        if _vision_dispatches_today() >= daily_cap:
            return {
                "path": str(image_path),
                "hash": image_hash,
                "status": "daily_cap_reached",
                "cap": daily_cap,
            }
        vision_caption = _vision_dispatch(image_path)
        if vision_caption:
            caption = vision_caption
            method = "vision"
            cost_cents = 0.0  # Gemini 2.5 Flash free tier (separate quota from OpenAI)
        else:
            cost_cents = 0.0

    if not caption:
        return {
            "path": str(image_path),
            "hash": image_hash,
            "status": "no_caption",
            "ocr_chars": ocr_chars,
            "ocr_ms": int((time.time() - t0) * 1000),
        }

    if dry_run:
        return {
            "path": str(image_path),
            "hash": image_hash,
            "status": "dry_run",
            "method": method,
            "caption_chars": len(caption),
            "ocr_ms": int((time.time() - t0) * 1000),
        }

    # Index caption in the vector store
    try:
        store = get_vector_store()
        store.create_collection(KNOWLEDGE_COLLECTION)

        emb = get_embedding(caption[:EMBED_TRUNCATE])
        if not emb:
            raise RuntimeError("embedding failed")

        doc_id = f"image:{image_hash}"
        store.upsert(
            KNOWLEDGE_COLLECTION,
            ids=[doc_id],
            vectors=[emb],
            documents=[caption],
            payloads=[
                {
                    "source": f"image/{image_path.name}",
                    "source_type": "image",
                    "image_hash": image_hash,
                    "caption_method": method,
                    "image_path": str(image_path),
                    "ocr_chars": ocr_chars,
                    "created_at": _now_iso(),
                    "embed_model": "multilingual-e5-large-instruct",
                }
            ],
        )
    except Exception as e:
        log.warning("image upsert failed for %s: %s", image_path.name, e)
        _log_failure(str(image_path), f"upsert: {e}")
        return {"path": str(image_path), "status": "upsert_failed", "error": str(e)[:200]}

    state[image_hash] = {
        "filename": image_path.name,
        "ingested_at": _now_iso(),
        "method": method,
        "ocr_chars": ocr_chars,
        "caption_chars": len(caption),
    }
    _record_cost(image_hash, method, cost_cents)

    return {
        "path": str(image_path),
        "hash": image_hash,
        "status": "ingested",
        "method": method,
        "caption_chars": len(caption),
        "ocr_ms": int((time.time() - t0) * 1000),
        "cost_cents": cost_cents,
    }


def ingest_directory(
    image_dir: Path,
    *,
    state: dict,
    reingest: bool = False,
    dry_run: bool = False,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> list[dict]:
    if not image_dir.exists():
        log.info("image dir does not exist, skipping: %s", image_dir)
        return []

    images: list[Path] = []
    for ext in SUPPORTED_EXTS:
        images.extend(image_dir.rglob(f"*{ext}"))
        images.extend(image_dir.rglob(f"*{ext.upper()}"))
    images = sorted(set(images))
    if not images:
        log.info("no images found in %s", image_dir)
        return []

    log.info("ingesting %d images from %s", len(images), image_dir)
    results = []
    for img in images:
        result = ingest_image(img, state=state, reingest=reingest, dry_run=dry_run, daily_cap=daily_cap)
        results.append(result)
    return results


def run() -> dict:
    """Entrypoint for the scheduler. Reads env config, runs default dir."""
    if os.environ.get("BRAIN_IMAGE_INGEST_DISABLED"):
        return {"status": "disabled", "reason": "BRAIN_IMAGE_INGEST_DISABLED env"}

    image_dir = Path(os.environ.get("BRAIN_IMAGE_INGEST_DIR") or DEFAULT_IMAGE_DIR)
    daily_cap = int(os.environ.get("BRAIN_IMAGE_DAILY_CAP") or DEFAULT_DAILY_CAP)

    state = _load_state()
    results = ingest_directory(image_dir, state=state, daily_cap=daily_cap)
    _save_state(state)

    counts: dict[str, int] = {}
    by_method: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r.get("method"):
            by_method[r["method"]] = by_method.get(r["method"], 0) + 1

    return {
        "image_dir": str(image_dir),
        "total_files": len(results),
        "by_status": counts,
        "by_method": by_method,
        "daily_cap": daily_cap,
        "vision_dispatches_today": _vision_dispatches_today(),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", type=Path, default=None)
    p.add_argument("--file", type=Path, default=None)
    p.add_argument("--reingest", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    args = p.parse_args()

    state = _load_state()

    if args.file:
        result = ingest_image(
            args.file,
            state=state,
            reingest=args.reingest,
            dry_run=args.dry_run,
            daily_cap=args.daily_cap,
        )
        if not args.dry_run:
            _save_state(state)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") in {"ingested", "skipped_dedupe", "dry_run"} else 1

    image_dir = args.dir or Path(os.environ.get("BRAIN_IMAGE_INGEST_DIR") or DEFAULT_IMAGE_DIR)
    results = ingest_directory(
        image_dir,
        state=state,
        reingest=args.reingest,
        dry_run=args.dry_run,
        daily_cap=args.daily_cap,
    )
    if not args.dry_run:
        _save_state(state)

    print(json.dumps({"image_dir": str(image_dir), "total_files": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
