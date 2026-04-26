"""Shared FastAPI dependencies, response shapes, and HTTP helpers.

Extracted from server.py so route modules under brain_core/routes/ can import
them without forcing a circular dep back through __main__ (server.py runs as
__main__ when launched directly).
"""

from __future__ import annotations

import contextlib
import contextvars as _contextvars
import hmac
import json
import os
import time
import uuid as _uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import Header, HTTPException
from pydantic import BaseModel

from config import FAILURE_LOG, SECRET_FILE

log = structlog.get_logger("brain.server")

LISTEN_HOST = os.getenv("BRAIN_SERVER_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("BRAIN_SERVER_PORT", "8791"))

SERVER_START = time.time()


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "brain-server"
    port: int = LISTEN_PORT
    uptime_sec: int


_cached_secret: str | None = None
# 2026-04-18: track mtime so we can detect rotation without reading on every
# request. Previously the secret was loaded once at startup and the server kept
# accepting the OLD secret indefinitely after rotation.
_cached_secret_mtime: float = 0.0


def _load_secret() -> str | None:
    if not SECRET_FILE.exists():
        return None
    return SECRET_FILE.read_text().strip()


def prime_secret_cache() -> None:
    """Called at startup to pre-load the secret into the cache."""
    global _cached_secret
    _cached_secret = _load_secret()


def _current_secret() -> str | None:
    """Return the current bearer secret, auto-reloading if the file changed.

    Cheap mtime check (one stat syscall, ~μs) so /recall/v2 hot path pays
    almost nothing when the file hasn't rotated.
    """
    global _cached_secret, _cached_secret_mtime
    try:
        mtime = SECRET_FILE.stat().st_mtime
    except FileNotFoundError:
        _cached_secret = None
        _cached_secret_mtime = 0.0
        return None
    if mtime != _cached_secret_mtime:
        _cached_secret = _load_secret()
        _cached_secret_mtime = mtime
    return _cached_secret


def _safe_http_detail(kind: str, exc: Exception, *, route: str = "?") -> str:
    """Return a user-safe HTTPException detail string.

    Logs the full exception server-side with an err_id; callers embed the
    err_id in the returned message so Chris can correlate. Internal details
    (SQL schema, file paths, stack frames, secrets embedded in error text)
    never reach the HTTP response.
    """
    err_id = _uuid.uuid4().hex[:12]
    with contextlib.suppress(Exception):
        log.warning(
            "HTTP error kind=%s route=%s err_id=%s exc_type=%s exc=%s",
            kind,
            route,
            err_id,
            type(exc).__name__,
            str(exc)[:500],
        )
    return f"{kind} failed (err_id={err_id})"


def _log_failure(reason: str, route: str = "?") -> None:
    with contextlib.suppress(Exception):
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "route": route,
                        "reason": reason[:500],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


# Request ID correlation — set by the `request_id_and_latency` middleware in
# server.py, read by handlers (e.g. /recall/v2 for debug tracing).
_request_id_ctx: _contextvars.ContextVar[str] = _contextvars.ContextVar("brain_request_id", default="")


def set_request_id(rid: str) -> None:
    _request_id_ctx.set(rid)


def get_request_id() -> str:
    """Return the current request's correlation ID (empty outside a request)."""
    return _request_id_ctx.get()


def verify_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
    """Auth dependency injected into every protected route. /healthz and /docs skip this."""
    secret = _current_secret()
    if not secret:
        _log_failure("server has no secret configured")
        raise HTTPException(status_code=503, detail="server misconfigured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[len("Bearer ") :].strip()
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="invalid bearer token")
