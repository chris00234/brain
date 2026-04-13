"""brain_core/http_async.py — Async HTTP client for hot-path handlers.

Uses httpx.AsyncClient with connection pooling. Complementary to http_pool.py
(which is sync-only). Used by async handlers in server.py that need non-blocking
HTTP calls to ChromaDB and Ollama.
"""

from __future__ import annotations

import asyncio
import logging
import httpx

from http_pool import ChromaAPIError

log = logging.getLogger("brain.http_async")

_client: httpx.AsyncClient | None = None
_client_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _get_lock():
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=60,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return _client


async def http_json_async(method: str, url: str, payload=None, timeout: int = 60):
    """Async HTTP JSON request. Raises ChromaAPIError on 4xx/5xx."""
    client = await get_client()
    resp = await client.request(method, url, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        try:
            err_body = resp.json() if resp.content else {}
        except Exception:
            err_body = {}
        err_msg = err_body.get("error") or err_body.get("detail") or err_body.get("message") or f"status {resp.status_code}"
        raise ChromaAPIError(resp.status_code, err_msg, url[:80])
    if not resp.content:
        return {}
    return resp.json()


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
