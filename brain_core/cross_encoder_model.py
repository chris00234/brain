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
"""
from __future__ import annotations

import logging
import os
import re
import threading

log = logging.getLogger("brain.cross_encoder_model")

_BASE_NAME = os.getenv("BRAIN_CROSS_ENCODER_BASE",  "BAAI/bge-reranker-base")
_BI_NAME   = os.getenv("BRAIN_CROSS_ENCODER_BILINGUAL", "BAAI/bge-reranker-v2-m3")
_FORCE_MODEL = os.getenv("BRAIN_CROSS_ENCODER_MODEL", "").strip()
_ADAPTIVE = os.getenv("BRAIN_CROSS_ENCODER_ADAPTIVE", "false").lower() in ("true", "1", "yes")
_DEVICE_OVERRIDE = os.getenv("BRAIN_CROSS_ENCODER_DEVICE")  # "mps" | "cpu" | "cuda"

_models: dict[str, object] = {}
_load_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()

_KOREAN_RE = re.compile(r"[\uac00-\ud7a3]")  # Hangul syllables


def _device() -> str:
    try:
        import torch
        if _DEVICE_OVERRIDE:
            return _DEVICE_OVERRIDE
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load_model(name: str):
    """Lazy singleton per model name. Safe under thread contention."""
    if name in _models:
        return _models[name]
    with _global_lock:
        if name not in _load_locks:
            _load_locks[name] = threading.Lock()
    with _load_locks[name]:
        if name in _models:
            return _models[name]
        from sentence_transformers import CrossEncoder
        device = _device()
        log.info("loading cross-encoder %s on %s", name, device)
        _models[name] = CrossEncoder(name, device=device, max_length=512)
        log.info("cross-encoder %s loaded", name)
    return _models[name]


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
    """
    if not docs:
        return []
    try:
        name = _select_name(query)
        model = _load_model(name)
        pairs = [(query, (d or "")[:1500]) for d in docs]  # cap doc at 1500 chars
        scores = model.predict(pairs, show_progress_bar=False, convert_to_numpy=True)
        return [float(s) for s in scores]
    except Exception as e:
        log.warning("cross-encoder scoring failed: %s", e)
        return [0.0] * len(docs)


def warmup() -> bool:
    """Force-load both models at startup so first-request latency is clean.

    Returns True on full success, False if either model failed (the dispatcher
    still falls back to whichever model is available at query time).
    """
    ok = True
    try:
        _load_model(_BASE_NAME)
        score_pairs("warmup probe english", ["warmup document for the english cross encoder"])
    except Exception as e:
        log.warning("base cross-encoder warmup failed: %s", e)
        ok = False
    if _ADAPTIVE and not _FORCE_MODEL:
        try:
            _load_model(_BI_NAME)
            score_pairs("워밍업 테스트", ["이 문서는 한국어 교차 인코더 워밍업용입니다"])
        except Exception as e:
            log.warning("bilingual cross-encoder warmup failed: %s", e)
            ok = False
    return ok
