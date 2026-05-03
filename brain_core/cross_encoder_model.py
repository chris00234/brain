"""brain_core/cross_encoder_model.py — adaptive bilingual CrossEncoder dispatcher.

Uses TWO cross-encoders side-by-side:
  - BAAI/bge-reranker-base (278M, ~120ms/batch on MPS) — default for English
  - BAAI/bge-reranker-v2-m3 (568M, ~300ms/batch on MPS) — bilingual (ko/en)

Dispatch rule: if the query contains any Korean characters, use v2-m3. Otherwise
use base. This keeps average latency near the base model's cost while giving
Korean queries the accuracy of the multilingual model.

Both models are lazily loaded so cold-start stays light. Override via env:
  BRAIN_CROSS_ENCODER_MODEL=BAAI/bge-reranker-v2-m3   (forces single model)
  BRAIN_CROSS_ENCODER_ADAPTIVE=false                   (disables dispatcher)
  BRAIN_CROSS_ENCODER_LOCAL_FILES_ONLY=false           (allow first download)
"""

from __future__ import annotations

import gc
import hashlib
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict

log = logging.getLogger("brain.cross_encoder_model")

# Direct imports of this module can happen outside server.py, so keep the
# low-noise runtime defaults here too before sentence_transformers/joblib load.
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "8")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# This module is imported both as `brain_core.cross_encoder_model` and, from
# older brain_core scripts that put brain_core/ on sys.path, as
# `cross_encoder_model`. Keep both names bound to the same module object so the
# model singleton and score cache cannot split into two independent copies.
_THIS_MODULE = sys.modules[__name__]
if __name__ == "brain_core.cross_encoder_model":
    sys.modules.setdefault("cross_encoder_model", _THIS_MODULE)
elif __name__ == "cross_encoder_model":
    sys.modules.setdefault("brain_core.cross_encoder_model", _THIS_MODULE)

_BASE_NAME = os.getenv("BRAIN_CROSS_ENCODER_BASE", "BAAI/bge-reranker-base")
_BI_NAME = os.getenv("BRAIN_CROSS_ENCODER_BILINGUAL", "BAAI/bge-reranker-v2-m3")
_FORCE_MODEL = os.getenv("BRAIN_CROSS_ENCODER_MODEL", "").strip()
# Default on — brain operates bilingual (Korean + English). With this off
# every Korean query goes through the English-only base model which has
# measurably worse Korean relevance. Kill switch via BRAIN_CROSS_ENCODER_ADAPTIVE=false.
_ADAPTIVE = os.getenv("BRAIN_CROSS_ENCODER_ADAPTIVE", "true").lower() in ("true", "1", "yes")
_DEVICE_OVERRIDE = os.getenv("BRAIN_CROSS_ENCODER_DEVICE")  # "mps" | "cpu" | "cuda"
_LOCAL_FILES_ONLY = os.getenv("BRAIN_CROSS_ENCODER_LOCAL_FILES_ONLY", "true").lower() in ("true", "1", "yes")
_MPS_EMPTY_CACHE = os.getenv("BRAIN_CE_MPS_EMPTY_CACHE", "false").lower() in ("true", "1", "yes")

_models: dict[str, object] = {}
_model_last_used: dict[str, float] = {}
_load_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()
_IDLE_TTL_SEC = int(os.getenv("BRAIN_CE_MODEL_IDLE_TTL_SEC", "900"))

_KOREAN_RE = re.compile(r"[\uac00-\ud7a3]")  # Hangul syllables

# Score cache — keyed on (model_name, query_hash, doc_hash) → raw logit.
# Scores are deterministic for a given (model, query, doc) triple, so
# memoizing is a pure latency win with zero quality impact. The stable eval
# rescores ~2760 (query, doc) pairs per run with heavy reuse across
# near-duplicate queries, so this halves cold-eval CE time on the first hit
# and drops it to near-zero on repeat runs.
_CACHE_SIZE = int(os.getenv("BRAIN_CE_CACHE_SIZE", "10000"))
_score_cache: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_cache_lock = threading.Lock()
_cache_hits = 0
_cache_misses = 0


def _doc_hash(text: str) -> str:
    return hashlib.sha1((text or "")[:1500].encode("utf-8", "replace")).hexdigest()[:16]  # noqa: S324


def _query_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]  # noqa: S324


def cache_stats() -> dict:
    """Return CE score cache statistics."""
    with _cache_lock:
        size = len(_score_cache)
        hits = _cache_hits
        misses = _cache_misses
    total = hits + misses
    return {
        "size": size,
        "max_size": _CACHE_SIZE,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / total, 4) if total else 0.0,
    }


def _device() -> str:
    try:
        import torch

        if _DEVICE_OVERRIDE:
            return _DEVICE_OVERRIDE
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception as exc:
        log.debug("device detection failed: %s", exc)
    return "cpu"


def _disable_tqdm_mp_lock() -> None:
    """Avoid a tqdm multiprocessing write lock during model-load progress bars.

    sentence-transformers/transformers use tqdm while loading model weights.
    tqdm's default multiprocessing RLock creates a named `/loky-*` semaphore;
    under the launchd-hosted FastAPI process it survives until Python shutdown
    and pollutes server.err.log with resource_tracker warnings. A thread-only
    tqdm lock is enough here because model warmup does not fork workers.
    """
    try:
        from tqdm.std import TqdmDefaultWriteLock

        TqdmDefaultWriteLock.mp_lock = None
    except Exception as exc:
        log.debug("could not disable tqdm multiprocessing lock: %s", exc)


def _load_model(name: str) -> object:
    """Lazy singleton per model name. Safe under thread contention."""
    if name in _models:
        return _models[name]
    with _global_lock:
        if name not in _load_locks:
            _load_locks[name] = threading.Lock()
    with _load_locks[name]:
        if name in _models:
            return _models[name]
        if _LOCAL_FILES_ONLY:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            from huggingface_hub import snapshot_download

            model_ref = snapshot_download(name, local_files_only=True)
        else:
            model_ref = name
        _disable_tqdm_mp_lock()
        from sentence_transformers import CrossEncoder

        device = _device()
        log.info("loading cross-encoder %s on %s", name, device)
        # max_length=384: docs are truncated to 1500 chars (~375 BGE tokens)
        # upstream in score_pairs, so 512-token model windows waste ~25% of
        # every forward pass on padding. 384 covers the 1500-char ceiling
        # with 9 tokens of headroom. Saves ~15-30ms per CE batch on MPS.
        _models[name] = CrossEncoder(
            model_ref, device=device, max_length=384, local_files_only=_LOCAL_FILES_ONLY
        )
        _model_last_used[name] = time.monotonic()
        log.info("cross-encoder %s loaded (max_length=384)", name)
    return _models[name]


def _evict_idle_models() -> list[str]:
    """Unload non-base CE models after an idle window to preserve RAM."""

    if _IDLE_TTL_SEC <= 0:
        return []
    now = time.monotonic()
    evicted: list[str] = []
    with _global_lock:
        for name in list(_models):
            if name == _BASE_NAME or (_FORCE_MODEL and name == _FORCE_MODEL):
                continue
            last_used = _model_last_used.get(name, now)
            if now - last_used < _IDLE_TTL_SEC:
                continue
            _models.pop(name, None)
            _model_last_used.pop(name, None)
            evicted.append(name)
    if evicted:
        gc.collect()
        _clear_accelerator_cache()
        log.info("evicted idle cross-encoder models: %s", ", ".join(evicted))
    return evicted


def _clear_accelerator_cache() -> None:
    """Release allocator scratch buffers after cross-encoder GPU work.

    PyTorch MPS keeps temporary Metal buffers in its caching allocator. In the
    long-running launchd FastAPI process, repeated CrossEncoder.predict calls
    can make RSS climb for hours even when Python object caches are bounded.
    Clearing after prediction is intentionally gated by
    BRAIN_CE_MPS_EMPTY_CACHE so tests/CPU paths keep the old behavior while
    production can trade a small latency cost for stable memory.
    """

    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        elif _MPS_EMPTY_CACHE and hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
    except Exception as exc:
        log.debug("cross-encoder cache cleanup failed: %s", exc)


def _select_name(query: str) -> str:
    """Pick which model to use for a query."""
    if _FORCE_MODEL:
        return _FORCE_MODEL
    if not _ADAPTIVE:
        return _BASE_NAME
    if query and _KOREAN_RE.search(query):
        return _BI_NAME
    return _BASE_NAME


def score_pairs(query: str, docs: list[str]) -> list[float]:
    """Score [query, doc] pairs. Dispatches to the right model for the query.

    Returns raw relevance logits (BGE-reranker base ~[-10, 10], v2-m3 similar).
    Sigmoid is applied downstream if needed for [0,1] normalization.

    Results are memoized per (model, query, doc) because cross-encoder scores
    are deterministic — idempotent caching, zero ranking impact.
    """
    global _cache_hits, _cache_misses
    if not docs:
        return []
    try:
        _evict_idle_models()
        name = _select_name(query)
        qh = _query_hash(query)

        # Fast-path lookup: hit ratio check first, only load model on miss.
        scores: list[float | None] = [None] * len(docs)
        miss_indices: list[int] = []
        miss_docs: list[str] = []
        cached_hits = 0
        with _cache_lock:
            for i, d in enumerate(docs):
                key = (name, qh, _doc_hash(d or ""))
                if key in _score_cache:
                    _score_cache.move_to_end(key)
                    scores[i] = _score_cache[key]
                    cached_hits += 1
                else:
                    miss_indices.append(i)
                    miss_docs.append(d or "")
            _cache_hits += cached_hits
            _cache_misses += len(miss_indices)

        if miss_indices:
            model = _load_model(name)
            pairs = [(query, (d or "")[:1500]) for d in miss_docs]
            try:
                raw = model.predict(pairs, show_progress_bar=False, convert_to_numpy=True)
            finally:
                _clear_accelerator_cache()
            _model_last_used[name] = time.monotonic()
            fresh = [float(s) for s in raw]
            with _cache_lock:
                for i, score in zip(miss_indices, fresh, strict=False):
                    scores[i] = score
                    key = (name, qh, _doc_hash(docs[i] or ""))
                    _score_cache[key] = score
                    _score_cache.move_to_end(key)
                while len(_score_cache) > _CACHE_SIZE:
                    _score_cache.popitem(last=False)

        return [float(s or 0.0) for s in scores]
    except Exception as e:
        log.warning("cross-encoder scoring failed: %s", e)
        return [0.0] * len(docs)


def warmup() -> bool:
    """Preload the English base cross-encoder at startup.

    2026-04-22: bilingual v2-m3 model (~568 MB) is NOT preloaded. It lazy-
    loads on the first Korean query via `_load_model` in the score path.
    Trade-off: ~500 MB RAM saved vs +1-2s cold start on the first Korean
    query per process. Korean hit rate is ~20-30% of queries so the savings
    dominate. Override with BRAIN_CE_EAGER_WARMUP=true to restore old
    behaviour.
    """
    ok = True
    try:
        _load_model(_BASE_NAME)
        score_pairs("warmup probe english", ["warmup document for the english cross encoder"])
    except Exception as e:
        log.warning("base cross-encoder warmup failed: %s", e)
        ok = False
    eager = os.getenv("BRAIN_CE_EAGER_WARMUP", "").strip().lower() in ("true", "1", "yes")
    if eager and _ADAPTIVE and not _FORCE_MODEL:
        try:
            _load_model(_BI_NAME)
            score_pairs("워밍업 테스트", ["이 문서는 한국어 교차 인코더 워밍업용입니다"])
        except Exception as e:
            log.warning("bilingual cross-encoder warmup failed: %s", e)
            ok = False
    return ok
