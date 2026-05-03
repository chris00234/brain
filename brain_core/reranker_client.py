"""HTTP client for the isolated cross-encoder reranker worker.

The long-running FastAPI brain server should not own the Torch/MPS
cross-encoder process. This client keeps the main server dependency-light:
worker unavailable means stage-1 results stand, not that the server imports
Torch as a fallback and repeats the RSS leak in-process.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

try:
    from brain_core.config import BRAIN_RERANKER_TIMEOUT_MS, BRAIN_RERANKER_URL, load_bearer_secret
except ImportError:  # pragma: no cover - top-level script import
    from config import BRAIN_RERANKER_TIMEOUT_MS, BRAIN_RERANKER_URL, load_bearer_secret

log = logging.getLogger("brain.reranker_client")


class RerankerUnavailable(RuntimeError):
    """Raised when the isolated reranker cannot produce a valid score batch."""


def _endpoint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RerankerUnavailable(f"refusing non-local reranker URL: {url!r}")
    return url.rstrip("/") + "/score"


def _optional_secret() -> str:
    try:
        return load_bearer_secret()
    except FileNotFoundError:
        return ""


def score_pairs_remote(
    query: str,
    docs: list[str],
    *,
    url: str | None = None,
    timeout_ms: int | None = None,
) -> list[float]:
    """Score query/document pairs through the isolated reranker worker."""

    if not docs:
        return []

    payload = json.dumps({"query": query, "docs": docs}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - _endpoint() restricts to local http(s) worker URLs
        _endpoint(url or BRAIN_RERANKER_URL),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    secret = _optional_secret()
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")

    timeout_s = max(0.1, (timeout_ms if timeout_ms is not None else BRAIN_RERANKER_TIMEOUT_MS) / 1000)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - local worker URL
            body = response.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise RerankerUnavailable(f"reranker worker request failed: {exc}") from exc

    try:
        decoded = json.loads(body.decode("utf-8"))
        scores = decoded["scores"]
        floats = [float(score) for score in scores]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RerankerUnavailable("reranker worker returned invalid score payload") from exc

    if len(floats) != len(docs):
        raise RerankerUnavailable(f"reranker worker score count mismatch: {len(floats)} != {len(docs)}")
    return floats
