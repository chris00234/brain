"""Shared HTTP connection pool with keep-alive for ChromaDB and Ollama.

Per-thread connection reuse avoids TCP setup overhead under concurrent load.
Single source of truth — imported by search.py and indexer.py.
"""

import http.client
import json
import logging
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("brain.http_pool")


class ChromaAPIError(Exception):
    """Raised when ChromaDB/Ollama returns a 4xx error response."""
    def __init__(self, status: int, message: str, path: str = ""):
        self.status = status
        self.message = message
        self.path = path
        super().__init__(f"HTTP {status} from {path}: {message}")

_thread_local = threading.local()
_CONN_TTL = 120  # seconds — shorter than Ollama's 5-min idle unload window


def _get_conn(host: str, port: int, timeout: int = 60) -> http.client.HTTPConnection:
    pool = getattr(_thread_local, 'conn_pool', None)
    if pool is None:
        _thread_local.conn_pool = {}
        pool = _thread_local.conn_pool
    key = f"{host}:{port}"
    entry = pool.get(key)
    if entry is not None:
        conn, created_at = entry
        if (time.time() - created_at) > _CONN_TTL:
            conn.close()
            del pool[key]
            entry = None
    if entry is None:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        pool[key] = (conn, time.time())
    else:
        conn = entry[0]
    return conn


def http_json(method: str, url: str, payload=None, timeout: int = 60):
    """HTTP JSON request with keep-alive connection reuse and auto-reconnect."""
    parsed = urlparse(url)
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "Connection": "keep-alive"} if body else {"Connection": "keep-alive"}
    conn = _get_conn(parsed.hostname, parsed.port, timeout=timeout)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (http.client.RemoteDisconnected, ConnectionError, OSError):
        conn.close()
        key = f"{parsed.hostname}:{parsed.port}"
        try:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
            _thread_local.conn_pool[key] = (conn, time.time())
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except Exception:
            # Reconnect failed — evict broken connection from pool
            _thread_local.conn_pool.pop(key, None)
            raise
    if resp.status >= 500:
        log.warning("HTTP %d from %s %s", resp.status, method, path[:80])
    if resp.status >= 400:
        try:
            err_body = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            err_body = {}
        err_msg = err_body.get("error") or err_body.get("detail") or err_body.get("message") or f"status {resp.status}"
        raise ChromaAPIError(resp.status, err_msg, path[:80])
    if not raw:
        return {}
    return json.loads(raw)
