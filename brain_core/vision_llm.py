"""brain_core/vision_llm.py — multimodal captioning for brain image ingest.

Calls Google Gemini's REST API directly via urllib (no SDK dependency — brain
venv stays slim). Used by ingest/images.py::_vision_dispatch to generate rich
captions for images that OCR alone can't describe.

Why Gemini:
  - GEMINI_API_KEY is already in Chris's ~/.openclaw/.env
  - gemini-2.0-flash is free on Chris's tier + has strong vision
  - REST API is simple; no SDK bloat in brain venv
  - Output is plain text — drops directly into Chroma as document text
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
from datetime import UTC
from pathlib import Path

log = logging.getLogger("brain.vision_llm")

DEFAULT_MODEL = "gemini-2.5-flash"  # 2.0-flash was deprecated/quota-exceeded on Chris's key
DAILY_CAP = int(os.environ.get("BRAIN_VISION_DAILY_CAP", "50"))
CALL_TIMEOUT_S = 30
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 3600.0
_CACHE_MAX = 64


def _load_api_key() -> str:
    """Load GEMINI_API_KEY from process env or ~/.openclaw/.env."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    env_file = Path.home() / ".openclaw" / ".env"
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k in ("GEMINI_API_KEY", "GOOGLE_API_KEY") and v:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def is_configured() -> bool:
    return bool(_load_api_key())


def _detect_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    ext = path.suffix.lower()
    return {
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".avif": "image/avif",
    }.get(ext, "image/png")


def _count_today_calls() -> int:
    try:
        from config import BRAIN_LOGS_DIR
    except ImportError:
        BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    log_file = BRAIN_LOGS_DIR / "vision_llm_calls.jsonl"
    if not log_file.exists():
        return 0
    try:
        from datetime import datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        n = 0
        with log_file.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date") == today:
                    n += 1
        return n
    except OSError:
        return 0


def _record_call(model: str, prompt_len: int, output_len: int, duration_ms: int) -> None:
    try:
        from datetime import datetime

        try:
            from config import BRAIN_LOGS_DIR
        except ImportError:
            BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
        log_file = BRAIN_LOGS_DIR / "vision_llm_calls.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "model": model,
            "prompt_chars": prompt_len,
            "output_chars": output_len,
            "duration_ms": duration_ms,
        }
        with log_file.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def describe_image(
    source: Path | bytes,
    *,
    prompt: str | None = None,
    model: str | None = None,
    max_tokens: int = 400,
) -> str:
    """Generate a text description of an image via Gemini multimodal.

    Returns the caption text, or empty string on failure.
    """
    api_key = _load_api_key()
    if not api_key:
        log.debug("no GEMINI_API_KEY; vision disabled")
        return ""

    if isinstance(source, Path):
        if not source.exists():
            return ""
        try:
            image_bytes = source.read_bytes()
        except OSError:
            return ""
        mime = _detect_mime(source)
        cache_key = f"{source.resolve()}|{source.stat().st_mtime}"
    else:
        image_bytes = source
        mime = "image/png"
        cache_key = None

    if len(image_bytes) > MAX_IMAGE_BYTES:
        log.warning("image too large (%d bytes)", len(image_bytes))
        return ""

    if cache_key:
        entry = _CACHE.get(cache_key)
        if entry and (time.time() - entry[0]) < _CACHE_TTL:
            return entry[1]

    if _count_today_calls() >= DAILY_CAP:
        log.warning("vision_llm daily cap (%d) reached", DAILY_CAP)
        return ""

    effective_model = model or os.environ.get("BRAIN_VISION_MODEL", DEFAULT_MODEL)
    effective_prompt = prompt or (
        "Describe this image in 2-4 sentences. Focus on what's visible, any "
        "text content, visible people or objects, and the likely context. "
        "Be concrete and factual. No speculation about intent or meaning."
    )

    b64_data = base64.b64encode(image_bytes).decode("ascii")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{effective_model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": effective_prompt},
                    {"inline_data": {"mime_type": mime, "data": b64_data}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
        },
    }

    t0 = time.time()
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
            body = resp.read().decode()
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            pass
        log.warning("vision_llm HTTP %d: %s", e.code, err_body)
        return ""
    except Exception as e:
        log.warning("vision_llm request failed: %s", e)
        return ""

    caption = ""
    try:
        candidates = data.get("candidates", []) or []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts", []) or []
            for p in parts:
                if "text" in p:
                    caption = str(p["text"]).strip()
                    break
    except (KeyError, IndexError, TypeError):
        pass

    duration_ms = int((time.time() - t0) * 1000)
    _record_call(effective_model, len(effective_prompt), len(caption), duration_ms)

    if cache_key and caption:
        _CACHE[cache_key] = (time.time(), caption)
        if len(_CACHE) > _CACHE_MAX:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            del _CACHE[oldest]

    return caption


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Caption an image via Gemini vision.")
    parser.add_argument("image")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    if not is_configured():
        print("ERROR: GEMINI_API_KEY not found in env or ~/.openclaw/.env")
        raise SystemExit(1)
    result = describe_image(Path(args.image), prompt=args.prompt, model=args.model)
    print(result or "(empty)")
