"""brain_core/vector_store.py — backend-agnostic vector store interface.

Defines :class:`VectorStore`, a ``typing.Protocol`` with the semantic surface
used across ``brain_core``, ``ingest``, ``cli``, ``pipeline`` and ``server.py``.

The live backend is :class:`~qdrant_store.QdrantStore` — ChromaDB was
decommissioned 2026-04-21 after the 33,552-point migration and verification.
The protocol + factory stay so swapping backends in the future is a
one-env-var flip and so tests can stub the surface cheaply.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("brain.vector_store")


# ── Data shapes (backend-agnostic) ────────────────────────────────


@dataclass(slots=True)
class VectorHit:
    """A single search result.

    Scores are **normalized to similarity** (higher is better). Qdrant's
    native cosine score matches this convention directly.
    """

    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)
    document: str | None = None
    vector: list[float] | None = None


@dataclass(slots=True)
class VectorPoint:
    """A stored point fetched by id/filter."""

    id: str
    payload: dict[str, Any] = field(default_factory=dict)
    document: str | None = None
    vector: list[float] | None = None


# ── Protocol ──────────────────────────────────────────────────────

Filter = dict[str, Any] | None


@runtime_checkable
class VectorStore(Protocol):
    """Semantic surface every vector backend must implement."""

    name: str  # "qdrant" — informational

    def heartbeat(self) -> bool:
        """Cheap liveness probe. True on 2xx, False otherwise."""

    def list_collections(self) -> list[str]:
        """Return collection names (not backend-specific ids)."""

    def create_collection(self, name: str, metadata: dict[str, Any] | None = None) -> None:
        """Create a collection if it does not already exist. No-op on exist."""

    def count(self, collection: str) -> int:
        """Row count for a collection. Returns 0 if the collection is absent."""

    def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        documents: list[str] | None = None,
    ) -> None:
        """Insert or overwrite points. Length of every list must match."""

    def query(
        self,
        collection: str,
        vector: list[float],
        k: int = 10,
        *,
        filter: Filter = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        query_text: str | None = None,
    ) -> list[VectorHit]:
        """kNN search; ``filter`` uses the Chroma-native shape translated at the backend boundary.

        ``query_text`` is optional and only used by hybrid-search backends
        (QdrantStore) for BM25 sparse fusion. Dense-only backends ignore it.
        """

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        *,
        filter: Filter = None,
        limit: int | None = None,
        offset: int = 0,
        with_payload: bool = True,
        with_vectors: bool = False,
        with_documents: bool = True,
    ) -> list[VectorPoint]:
        """Fetch points by id and/or filter. Paginates via ``offset``/``limit``."""

    def delete(self, collection: str, ids: list[str]) -> None:
        """Remove points by id. No-op on unknown ids."""

    def update_payload(
        self,
        collection: str,
        ids: list[str],
        patch: dict[str, Any],
    ) -> None:
        """Merge ``patch`` into the payload of every listed id. Vectors untouched."""


# ── Factory ──────────────────────────────────────────────────────

_factory_lock = threading.Lock()
_singleton: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """Return the process-wide :class:`VectorStore` singleton.

    QdrantStore is the only live backend. The env var ``VECTOR_BACKEND``
    is read to keep the shape in place for future swaps, but any value
    other than ``qdrant`` is rejected with a loud error so operators
    don't silently fall back to a store that no longer exists.

    The singleton is built lazily so importing this module does not touch
    Qdrant or environment variables at import time.
    """

    global _singleton

    with _factory_lock:
        if _singleton is not None:
            return _singleton

        target = (os.getenv("VECTOR_BACKEND") or "qdrant").strip().lower()
        if target != "qdrant":
            raise RuntimeError(
                f"VECTOR_BACKEND={target!r} is no longer supported. "
                "ChromaDB was decommissioned 2026-04-21; Qdrant is the only backend."
            )

        from qdrant_store import QdrantStore

        _singleton = QdrantStore()
        log.info("VectorStore singleton initialized: backend=qdrant")
        return _singleton


def reset_vector_store() -> None:
    """Drop the cached singleton. Tests use this; production should never call it."""

    global _singleton
    with _factory_lock:
        _singleton = None
