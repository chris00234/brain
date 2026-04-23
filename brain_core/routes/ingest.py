"""/brain/ingest/image — live image captioning (v3 vision)."""

from __future__ import annotations

import base64 as _b64
import hashlib as _hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from api_deps import _safe_http_detail, verify_bearer
from config import BRAIN_DIR
from fastapi import APIRouter, Depends, HTTPException, Request
from indexer import get_embedding
from pydantic import BaseModel, Field
from rate_limit import limiter
from vector_store import get_vector_store

router = APIRouter(dependencies=[Depends(verify_bearer)])


class ImageIngestRequest(BaseModel):
    """Live image ingest payload. Either `path` (local file, preferred) or
    `base64_data` + optional `mime_type`. Also supports `prompt` override
    to steer the caption."""

    path: str | None = Field(default=None, max_length=512)
    base64_data: str | None = Field(default=None, max_length=30_000_000)  # ~22MB base64 = 16MB raw
    mime_type: str = Field(default="image/png", max_length=32)
    prompt: str | None = Field(default=None, max_length=500)
    agent: str = Field(default="claude", max_length=32)


_IMAGE_ALLOWED_ROOTS = (
    Path("/Users/chrischo/Pictures").resolve(),
    Path("/Users/chrischo/Downloads").resolve(),
    Path("/Users/chrischo/Desktop").resolve(),
    (BRAIN_DIR / "inbox").resolve(),
    Path("/tmp").resolve(),  # noqa: S108 — explicit allowlist of paths usable for image ingest
    Path("/private/tmp").resolve(),
)
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp"}


@router.post("/brain/ingest/image", tags=["memory"])
@limiter.limit("20/minute")
def ingest_image_route(request: Request, req: ImageIngestRequest) -> dict:
    """Live image ingest. Caller submits a file path OR base64 bytes; brain
    sends the image to Gemini 2.5 Flash for captioning, then indexes the
    caption + path in the knowledge Chroma collection for text-query retrieval.
    """
    try:
        import vision_llm
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="vision_llm unavailable") from exc

    if not vision_llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="vision_llm not configured (missing GEMINI_API_KEY)",
        )

    image_bytes: bytes | None = None
    image_path_str: str | None = None
    if req.path:
        try:
            p = Path(req.path).expanduser().resolve(strict=False)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid path") from exc
        if p.suffix.lower() not in _IMAGE_EXTS:
            raise HTTPException(status_code=400, detail="unsupported extension")
        if not any(
            str(p).startswith(str(root) + os.sep) or str(p) == str(root) for root in _IMAGE_ALLOWED_ROOTS
        ):
            raise HTTPException(status_code=400, detail="path outside allowlisted roots")
        if p.is_symlink():
            raise HTTPException(status_code=400, detail="symlinks not allowed")
        if not p.exists():
            raise HTTPException(status_code=400, detail="path not found")
        if not p.is_file():
            raise HTTPException(status_code=400, detail="not a file")
        try:
            image_bytes = p.read_bytes()
            image_path_str = str(p)
        except OSError as exc:
            raise HTTPException(status_code=400, detail="read failed") from exc
    elif req.base64_data:
        try:
            image_bytes = _b64.b64decode(req.base64_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_http_detail("base64 decode", e)) from e
        image_path_str = None
    else:
        raise HTTPException(status_code=400, detail="must provide either 'path' or 'base64_data'")

    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty image")

    try:
        caption = vision_llm.describe_image(
            Path(image_path_str) if image_path_str else image_bytes,
            prompt=req.prompt,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vision_llm", e)) from e

    if not caption:
        raise HTTPException(
            status_code=502,
            detail="vision_llm returned empty caption (quota / model error)",
        )

    image_hash = _hashlib.sha256(image_bytes).hexdigest()
    doc_id = f"image/{image_hash[:16]}"
    doc_text = f"[Image caption]\n{caption}"
    if image_path_str:
        doc_text += f"\n\nPath: {image_path_str}"

    try:
        store = get_vector_store()
        store.create_collection("knowledge")
        embedding = get_embedding(doc_text[:4000])
        if not embedding:
            raise HTTPException(status_code=502, detail="embedding failed")
        store.upsert(
            "knowledge",
            ids=[doc_id],
            vectors=[embedding],
            documents=[doc_text],
            payloads=[
                {
                    "type": "image_caption",
                    "image_hash": image_hash,
                    "path": image_path_str or "",
                    "mime_type": req.mime_type,
                    "agent": req.agent,
                    "captioned_by": "gemini-2.5-flash",
                    "captioned_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("chroma upsert", e)) from e

    return {
        "status": "ingested",
        "id": doc_id,
        "image_hash": image_hash,
        "caption": caption,
        "indexed_in": "knowledge",
    }
