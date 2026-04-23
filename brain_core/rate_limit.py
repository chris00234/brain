"""Shared slowapi limiter instance.

Bearer-token-keyed so a leaked token can't burn unbounded LLM cost behind the
Cloudflare tunnel (all tunneled requests look like 127.0.0.1 to uvicorn).
"""

from __future__ import annotations

import hashlib
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

_rate_limit_disabled = os.getenv("BRAIN_RATE_LIMIT_DISABLED", "").lower() in ("1", "true", "yes")


def _rate_limit_key(request: Request) -> str:
    """Bearer-token-keyed rate limiting (M7-WS7 C1 fix).

    Threat model: external token leak ⇒ unbounded LLM cost. Brain runs behind
    nginx in OrbStack and is reached via Cloudflare tunnel — every external
    request lands at uvicorn with `request.client.host == "127.0.0.1"` because
    we don't run a `forwarded_allow_ips` proxy header chain. Keying on client
    IP would give EVERY tunnel request a free pass, which is what was
    happening before this fix.

    The right key is the bearer token itself (which is also the principal
    being rate-limited). We only hash the first 16 hex chars to keep the key
    space bounded and avoid leaking the secret into log buckets.

    Anonymous requests (no Authorization header, e.g. /healthz) fall back to
    client IP — fine because /healthz is unauth and not LLM-billable.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return f"bearer:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
    return get_remote_address(request) or "anon"


limiter = Limiter(
    key_func=_rate_limit_key,
    enabled=not _rate_limit_disabled,
    default_limits=["1000/minute"],
    headers_enabled=False,
)
