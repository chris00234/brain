"""brain_core/vision_llm.py — multimodal captioning for brain image ingest.

Default backend is subscription CLI vision via `codex exec --image`, not a
direct paid API path. Used by ingest/images.py::_vision_dispatch to generate
rich captions for images that OCR alone can't describe.

Backend policy:
  - codex_cli (default): uses Chris's existing GPT/Codex subscription path.
  - gemini: explicit opt-in via BRAIN_VISION_BACKEND=gemini for fallback only.
  - off: OCR-only image ingest.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC
from pathlib import Path

log = logging.getLogger("brain.vision_llm")
CODEX_CWD = Path("/tmp")  # noqa: S108 — no repo context; pure captioning scratch cwd

DEFAULT_MODEL = "gemini-2.5-flash"  # 2.0-flash was deprecated/quota-exceeded on Chris's key
DEFAULT_BACKEND = os.environ.get("BRAIN_VISION_BACKEND", "codex_cli").strip().lower()
CODEX_BIN = os.environ.get("BRAIN_VISION_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("BRAIN_VISION_CODEX_MODEL", "").strip()
DAILY_CAP = int(os.environ.get("BRAIN_VISION_DAILY_CAP", "50"))
CALL_TIMEOUT_S = 30
CLI_TIMEOUT_S = int(os.environ.get("BRAIN_VISION_CLI_TIMEOUT_S", "90"))
CLI_CONCURRENCY = max(1, int(os.environ.get("BRAIN_VISION_CLI_CONCURRENCY", "1")))
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 3600.0
_CACHE_MAX = 64
_cli_semaphore = threading.BoundedSemaphore(CLI_CONCURRENCY)


def _load_api_key() -> str:
    """Load GEMINI_API_KEY from process env or Hermes/legacy env files."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    for env_file in (Path.home() / ".hermes" / ".env", Path.home() / ".openclaw" / ".env"):
        if not env_file.exists():
            continue
        try:
            lines = env_file.read_text().splitlines()
        except Exception:
            continue
        for line in lines:
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
    return ""


def _backend() -> str:
    return DEFAULT_BACKEND if DEFAULT_BACKEND in {"codex_cli", "gemini", "off"} else "off"


def backend_name() -> str:
    return _backend()


def is_configured() -> bool:
    backend = _backend()
    if backend == "off":
        return False
    if backend == "gemini":
        return bool(_load_api_key())
    return shutil.which(CODEX_BIN) is not None


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
                except json.JSONDecodeError as _exc:
                    log.debug("silenced exception in vision_llm.py: %s", _exc)
                    continue
                if rec.get("date") == today:
                    n += 1
        return n
    except OSError:
        return 0


def _record_call(
    backend: str,
    model: str,
    prompt_len: int,
    output_len: int,
    duration_ms: int,
    *,
    ok: bool = True,
    error: str = "",
) -> None:
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
            "backend": backend,
            "model": model,
            "prompt_chars": prompt_len,
            "output_chars": output_len,
            "duration_ms": duration_ms,
            "ok": ok,
        }
        if error:
            rec["error"] = error[:200]
        with log_file.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as _exc:
        log.debug("silenced exception in vision_llm.py: %s", _exc)


def _breaker_kind(backend: str) -> str:
    return f"vision.{backend}"


def _breaker_allows_request(kind: str) -> bool:
    """Apply the shared persistent breaker to the direct Gemini vision exception."""
    try:
        from breakers import peek_breaker, try_claim_probe

        snapshot = peek_breaker(kind)
        if snapshot.blocks_new_callers:
            log.warning(
                "vision_llm breaker blocks request: state=%s cooldown=%.0fs reason=%s",
                snapshot.state,
                snapshot.remaining_cooldown_s,
                snapshot.reason,
            )
            return False
        if snapshot.is_half_open and not try_claim_probe(kind):
            log.warning("vision_llm breaker probe already in flight")
            return False
        return True
    except Exception as _exc:
        log.debug("vision_llm breaker check skipped: %s", _exc)
        return True


def _record_breaker_result(kind: str, ok: bool, error: str = "") -> None:
    try:
        from breakers import record_result

        record_result(kind, ok=ok, error=error)
    except Exception as _exc:
        log.debug("vision_llm breaker record skipped: %s", _exc)


def _record_usage(
    backend: str,
    model: str,
    duration_ms: int,
    ok: bool,
    prompt_len: int,
    output_len: int,
) -> None:
    """Mirror direct Gemini calls into llm_usage.db for dispatch SLO/accounting."""
    try:
        from openclaw_dispatch import _record_usage as record_usage

        record_usage(
            f"vision.{backend}",
            duration_ms,
            ok,
            prompt_tokens=0,
            response_tokens=0,
            provider="codex-subscription-cli" if backend == "codex_cli" else "google-gemini",
            model=model,
            cost_usd=0.0,
        )
    except Exception as _exc:
        log.debug("vision_llm usage record skipped: %s", _exc)


def _describe_with_codex(image_path: Path, prompt: str) -> tuple[str, str]:
    codex_path = shutil.which(CODEX_BIN)
    if not codex_path:
        return "", "codex_not_found"
    if not _cli_semaphore.acquire(blocking=False):
        return "", "codex_busy"

    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out:
        out_path = Path(out.name)
    cmd = [
        codex_path,
        "exec",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--image",
        str(image_path),
        "--output-last-message",
        str(out_path),
    ]
    if CODEX_MODEL:
        cmd.extend(["--model", CODEX_MODEL])
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(CODEX_CWD),
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
        )
        if result.returncode != 0:
            return "", (result.stderr or result.stdout or "codex_failed")[:200]
        caption = out_path.read_text(errors="replace").strip()
        return caption, "" if caption else "empty_caption"
    except subprocess.TimeoutExpired:
        return "", "codex_timeout"
    except Exception as exc:
        return "", type(exc).__name__
    finally:
        _cli_semaphore.release()
        try:
            out_path.unlink(missing_ok=True)
        except OSError as _exc:
            log.debug("vision_llm output cleanup skipped: %s", _exc)


def _describe_with_gemini(
    image_bytes: bytes,
    mime: str,
    prompt: str,
    model: str,
    max_tokens: int,
) -> tuple[str, str, int]:
    api_key = _load_api_key()
    if not api_key:
        return "", "missing_gemini_api_key", 0

    b64_data = base64.b64encode(image_bytes).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/" f"{model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
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
        req = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:  # noqa: S310
            body = resp.read().decode()
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:300]
        except Exception as _exc:
            log.debug("silenced exception in vision_llm.py: %s", _exc)
        return "", f"http_{e.code}: {err_body}", int((time.time() - t0) * 1000)
    except Exception as e:
        return "", type(e).__name__, int((time.time() - t0) * 1000)

    caption = ""
    try:
        candidates = data.get("candidates", []) or []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts", []) or []
            for p in parts:
                if "text" in p:
                    caption = str(p["text"]).strip()
                    break
    except (KeyError, IndexError, TypeError) as _exc:
        log.debug("silenced exception in vision_llm.py: %s", _exc)
    return caption, "" if caption else "empty_caption", int((time.time() - t0) * 1000)


def describe_image(
    source: Path | bytes,
    *,
    prompt: str | None = None,
    model: str | None = None,
    max_tokens: int = 400,
) -> str:
    """Generate a text description of an image via the configured vision backend.

    Returns the caption text, or empty string on failure.
    """
    backend = _backend()
    if backend == "off":
        log.debug("vision backend off; OCR-only")
        return ""

    temp_input: Path | None = None
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
        if backend == "codex_cli":
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(image_bytes)
                temp_input = Path(tmp.name)

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

    breaker_kind = _breaker_kind(backend)
    if not _breaker_allows_request(breaker_kind):
        if temp_input:
            temp_input.unlink(missing_ok=True)
        return ""

    t0 = time.time()
    try:
        if backend == "codex_cli":
            image_path = source if isinstance(source, Path) else temp_input
            if image_path is None:
                return ""
            effective_model = CODEX_MODEL or "codex-default"
            caption, error = _describe_with_codex(image_path, effective_prompt)
            duration_ms = int((time.time() - t0) * 1000)
        else:
            caption, error, duration_ms = _describe_with_gemini(
                image_bytes,
                mime,
                effective_prompt,
                effective_model,
                max_tokens,
            )
    finally:
        if temp_input:
            temp_input.unlink(missing_ok=True)

    ok = bool(caption)
    if not ok and not error:
        error = "empty_caption"
        log.warning("vision_llm %s returned empty caption", backend)
    _record_call(
        backend,
        effective_model,
        len(effective_prompt),
        len(caption),
        duration_ms,
        ok=ok,
        error=error,
    )
    _record_usage(backend, effective_model, duration_ms, ok, len(effective_prompt), len(caption))
    _record_breaker_result(breaker_kind, ok, error)

    if cache_key and caption:
        _CACHE[cache_key] = (time.time(), caption)
        if len(_CACHE) > _CACHE_MAX:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            del _CACHE[oldest]

    return caption


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Caption an image via the configured vision backend.")
    parser.add_argument("image")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    if not is_configured():
        print("ERROR: vision backend not configured")  # noqa: T201
        raise SystemExit(1)
    result = describe_image(Path(args.image), prompt=args.prompt, model=args.model)
    print(result or "(empty)")  # noqa: T201
