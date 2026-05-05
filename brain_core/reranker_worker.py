"""Isolated cross-encoder reranker service.

Runs Torch/MPS cross-encoder scoring out-of-process from the main brain server
so model allocator growth is bounded by launchd restarts instead of accumulating
inside the API process.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("BRAIN_CE_MPS_EMPTY_CACHE", "true")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "8")

if __package__ in {None, ""}:  # pragma: no cover - direct script launch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

try:
    import psutil
except Exception:  # pragma: no cover - psutil is present in prod, optional in tests
    psutil = None  # type: ignore[assignment]

try:
    from brain_core.config import load_bearer_secret
except ImportError:  # pragma: no cover - top-level script import
    from config import load_bearer_secret

app = FastAPI(title="Brain Cross-Encoder Reranker", version="1.0")
log = logging.getLogger("brain.reranker_worker")

_STARTED_AT = time.monotonic()
_REQUEST_COUNT = 0
_RECYCLE_SCHEDULED = False
_MAX_RSS_MB = int(os.getenv("BRAIN_RERANKER_MAX_RSS_MB", "3072"))
_MAX_REQUESTS = int(os.getenv("BRAIN_RERANKER_MAX_REQUESTS", "2000"))
_MAX_LIFETIME_SEC = int(os.getenv("BRAIN_RERANKER_MAX_LIFETIME_SEC", "21600"))


class ScoreRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    docs: list[str] = Field(..., min_length=1, max_length=64)


class ScoreResponse(BaseModel):
    scores: list[float]


def _rss_mb() -> float | None:
    if psutil is None:
        return None
    return round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)


def _authorize(authorization: str | None = Header(default=None)) -> None:
    try:
        secret = load_bearer_secret()
    except FileNotFoundError:
        secret = ""
    if not secret:
        return
    expected = f"Bearer {secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _score_pairs(query: str, docs: list[str]) -> list[float]:
    # Import here so module import/health checks do not load Torch/model weights.
    try:
        from brain_core.cross_encoder_model import score_pairs
    except ImportError:  # pragma: no cover - top-level script import
        from cross_encoder_model import score_pairs
    return score_pairs(query, docs)


def _recycle_reason() -> str | None:
    rss = _rss_mb()
    if _MAX_RSS_MB > 0 and rss is not None and rss > _MAX_RSS_MB:
        return f"rss_mb>{_MAX_RSS_MB}"
    if _MAX_REQUESTS > 0 and _REQUEST_COUNT >= _MAX_REQUESTS:
        return f"requests>={_MAX_REQUESTS}"
    age = time.monotonic() - _STARTED_AT
    if _MAX_LIFETIME_SEC > 0 and age >= _MAX_LIFETIME_SEC:
        return f"lifetime_sec>={_MAX_LIFETIME_SEC}"
    return None


def _schedule_recycle(reason: str) -> None:
    global _RECYCLE_SCHEDULED
    if _RECYCLE_SCHEDULED:
        return
    _RECYCLE_SCHEDULED = True
    log.warning("brain reranker recycling after response: %s", reason)
    threading.Timer(0.25, lambda: os._exit(75)).start()


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "pid": os.getpid(),
        "rss_mb": _rss_mb(),
        "requests": _REQUEST_COUNT,
        "max_rss_mb": _MAX_RSS_MB,
        "max_requests": _MAX_REQUESTS,
        "max_lifetime_sec": _MAX_LIFETIME_SEC,
        "recycle_scheduled": _RECYCLE_SCHEDULED,
    }


@app.post("/score", response_model=ScoreResponse)
def score(request: ScoreRequest, _: None = Depends(_authorize)) -> ScoreResponse:
    global _REQUEST_COUNT
    _REQUEST_COUNT += 1
    scores = _score_pairs(request.query, request.docs)
    reason = _recycle_reason()
    if reason:
        _schedule_recycle(reason)
    return ScoreResponse(scores=scores)


def main() -> None:
    import uvicorn

    port = int(os.getenv("BRAIN_RERANKER_PORT", "8792"))
    uvicorn.run("brain_core.reranker_worker:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover - launchd entry point
    main()
