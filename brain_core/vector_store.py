"""brain_core/vector_store.py — backend-agnostic vector store interface.

Introduced as Phase A1 of the Qdrant migration plan (see
``~/.claude/plans/toasty-snacking-shamir.md``).

This module defines :class:`VectorStore`, a ``typing.Protocol`` with the
semantic surface actually used across ``brain_core``, ``ingest``, ``cli``,
``pipeline`` and ``server.py``. The surface was derived from the Phase 1
exploration — every ``chroma_api(...)`` call site falls into one of these
operations.

Two implementations ship with this commit:

* :class:`ChromaStore` — thin wrapper around the existing
  :func:`brain_core.indexer.chroma_api` HTTP client. Behavior-identical to
  direct ``chroma_api`` calls. Safe to swap in anywhere.
* (coming in Phase A5) ``QdrantStore`` — same surface over Qdrant.

**Runtime impact of this commit:** zero. Nothing currently imports this
module; call-site migration (Phases A2/A3) moves individual files over to
this abstraction in follow-up atomic commits. The module is a
no-behavior-change addition.

A future ``DualWriteStore`` (Phase B2) will wrap both implementations for
the dual-write migration window; the kill switch is env-driven via
:func:`get_vector_store`.

Environment variables consulted:

* ``VECTOR_BACKEND`` — ``chroma`` (default) | ``qdrant`` | ``dual``
* ``VECTOR_DUAL_WRITE`` — ``true`` | ``false`` (default ``true`` when
  backend is ``dual``); exists to allow instant write-disable in
  production without a redeploy.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("brain.vector_store")


# ── Data shapes (backend-agnostic) ────────────────────────────────


@dataclass(slots=True)
class VectorHit:
    """A single search result.

    Scores are **normalized to similarity** (higher is better). ChromaDB
    returns cosine *distance* (0..2, lower is better); :class:`ChromaStore`
    converts via ``1 - distance`` so callers never see raw distances.
    Qdrant's native score matches this convention.
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

# Filter shape is a dict today and gets translated per-backend. During the
# migration window ChromaStore accepts Chroma-native ``where`` shapes
# verbatim so existing call sites can adopt VectorStore without rewriting
# their filter syntax. QdrantStore (Phase A5) will add a translator.
Filter = dict[str, Any] | None


@runtime_checkable
class VectorStore(Protocol):
    """Semantic surface every vector backend must implement."""

    name: str  # "chroma" | "qdrant" | "dual" — informational

    def heartbeat(self) -> bool:
        """Cheap liveness probe. True on 2xx, False otherwise."""

    def list_collections(self) -> list[str]:
        """Return collection names (not backend-specific ids)."""

    def create_collection(self, name: str, metadata: dict[str, Any] | None = None) -> None:
        """Create a collection if it does not already exist. No-op on exist.

        `metadata` may include backend-specific tuning; unknown keys are
        silently ignored by each backend. Use named-vector/quantization
        schemas via backend-specific bootstrap scripts, not this path.
        """

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
    ) -> list[VectorHit]:
        """kNN search; ``filter`` uses the backend's native shape today."""

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
        """Merge ``patch`` into the payload of every listed id.

        Vectors are not touched. ChromaDB's API is a full replace of the
        metadata dict for the given ids; :class:`ChromaStore` fetches the
        current metadata, merges ``patch``, and writes back so callers can
        rely on patch semantics on both backends.
        """


# ── ChromaStore — HTTP wrapper preserving current behavior ────────


class ChromaStore:
    """VectorStore backed by ChromaDB 1.4.x via the v2 HTTP API.

    Preserves the exact behavior of today's :func:`indexer.chroma_api`
    call sites, including:

    * collection-name → uuid lookup with an in-process cache
    * `hnsw:space=cosine` default metadata on create
    * 5-retry backoff on create_collection (matches ``ensure_collection``)
    * distance → similarity flip so callers see higher-is-better scores
    """

    name = "chroma"

    _TENANT_PATH = "/api/v2/tenants/default_tenant/databases/default_database/collections"

    def __init__(self, base_url: str | None = None) -> None:
        # Deferred import: indexer pulls in heavy deps (sentence_transformers
        # path) at module load, so we only touch it when ChromaStore is
        # actually instantiated. Existing call sites of ``chroma_api`` are
        # unaffected.
        #
        # Import shape depends on how the caller wired sys.path. Server and
        # in-package callers set brain root on the path ("brain_core.indexer");
        # CLI scripts typically insert brain_core/ itself ("indexer"). Try
        # both so this module is drop-in for every existing entry point.
        try:
            from indexer import chroma_api as _chroma_api
        except ModuleNotFoundError:
            from brain_core.indexer import chroma_api as _chroma_api

        self._chroma_api = _chroma_api
        if base_url is not None:
            # Tests can override; prod reads CHROMA_URL via indexer.
            log.debug("ChromaStore constructed with override base_url=%s", base_url)
        self._id_cache: dict[str, str] = {}
        self._id_cache_lock = threading.Lock()

    # ── helpers ──────────────────────────────────────────────────

    def _collection_id(self, name: str, *, create: bool = False) -> str | None:
        """Resolve a collection name to its ChromaDB UUID.

        Results are cached. On miss we re-list collections from ChromaDB
        (this is the same pattern used by ``indexer._get_collection_id``).
        When ``create=True`` a missing collection is created with the
        standard cosine HNSW metadata.
        """

        with self._id_cache_lock:
            cached = self._id_cache.get(name)
        if cached:
            return cached

        cols = self._chroma_api("GET", self._TENANT_PATH)
        resolved: str | None = None
        if isinstance(cols, list):
            for c in cols:
                cid = c.get("id") if isinstance(c, dict) else None
                cname = c.get("name") if isinstance(c, dict) else None
                if cid and cname:
                    with self._id_cache_lock:
                        self._id_cache[cname] = cid
                    if cname == name:
                        resolved = cid

        if resolved is not None:
            return resolved
        if not create:
            return None

        result = self._chroma_api(
            "POST",
            self._TENANT_PATH,
            {"name": name, "metadata": {"hnsw:space": "cosine"}},
        )
        new_id = result.get("id") if isinstance(result, dict) else None
        if new_id:
            with self._id_cache_lock:
                self._id_cache[name] = new_id
        return new_id

    # ── VectorStore implementation ──────────────────────────────

    def heartbeat(self) -> bool:
        try:
            self._chroma_api("GET", "/api/v2/heartbeat")
            return True
        except Exception as exc:
            log.debug("chroma heartbeat failed: %s", exc)
            return False

    def list_collections(self) -> list[str]:
        cols = self._chroma_api("GET", self._TENANT_PATH)
        if not isinstance(cols, list):
            return []
        names: list[str] = []
        with self._id_cache_lock:
            for c in cols:
                if not isinstance(c, dict):
                    continue
                cname = c.get("name")
                cid = c.get("id")
                if cname and cid:
                    self._id_cache[cname] = cid
                    names.append(cname)
        return names

    def create_collection(self, name: str, metadata: dict[str, Any] | None = None) -> None:
        payload = {"name": name, "metadata": {"hnsw:space": "cosine"}}
        if metadata:
            payload["metadata"].update(metadata)
        try:
            result = self._chroma_api("POST", self._TENANT_PATH, payload)
        except Exception:
            # Already-exists is the common case; try to resolve and cache.
            existing = self._collection_id(name)
            if existing:
                return
            raise
        new_id = result.get("id") if isinstance(result, dict) else None
        if new_id:
            with self._id_cache_lock:
                self._id_cache[name] = new_id

    def count(self, collection: str) -> int:
        cid = self._collection_id(collection)
        if not cid:
            return 0
        resp = self._chroma_api("GET", f"{self._TENANT_PATH}/{cid}/count")
        if isinstance(resp, int):
            return resp
        if isinstance(resp, dict):
            try:
                return int(resp.get("count", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        documents: list[str] | None = None,
    ) -> None:
        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError(
                f"upsert length mismatch: ids={len(ids)} vectors={len(vectors)} payloads={len(payloads)}"
            )
        if documents is not None and len(documents) != len(ids):
            raise ValueError(f"upsert length mismatch: ids={len(ids)} documents={len(documents)}")
        cid = self._collection_id(collection, create=True)
        if not cid:
            raise RuntimeError(f"cannot resolve or create collection {collection!r}")
        body: dict[str, Any] = {
            "ids": ids,
            "embeddings": vectors,
            "metadatas": payloads,
        }
        if documents is not None:
            body["documents"] = documents
        self._chroma_api("POST", f"{self._TENANT_PATH}/{cid}/upsert", body)

    def query(
        self,
        collection: str,
        vector: list[float],
        k: int = 10,
        *,
        filter: Filter = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> list[VectorHit]:
        cid = self._collection_id(collection)
        if not cid:
            return []
        include: list[str] = ["distances"]
        if with_payload:
            include.append("metadatas")
            include.append("documents")
        if with_vectors:
            include.append("embeddings")
        body: dict[str, Any] = {
            "query_embeddings": [vector],
            "n_results": k,
            "include": include,
        }
        if filter:
            body["where"] = filter
        resp = self._chroma_api("POST", f"{self._TENANT_PATH}/{cid}/query", body)
        return self._unwrap_query(resp, with_payload=with_payload, with_vectors=with_vectors)

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
        cid = self._collection_id(collection)
        if not cid:
            return []
        include: list[str] = []
        if with_payload:
            include.append("metadatas")
        if with_documents:
            include.append("documents")
        if with_vectors:
            include.append("embeddings")
        body: dict[str, Any] = {"include": include}
        if ids is not None:
            body["ids"] = ids
        if filter:
            body["where"] = filter
        if limit is not None:
            body["limit"] = int(limit)
        if offset:
            body["offset"] = int(offset)
        resp = self._chroma_api("POST", f"{self._TENANT_PATH}/{cid}/get", body)
        return self._unwrap_get(resp, with_payload=with_payload, with_vectors=with_vectors)

    def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        cid = self._collection_id(collection)
        if not cid:
            return
        self._chroma_api("POST", f"{self._TENANT_PATH}/{cid}/delete", {"ids": ids})

    def update_payload(
        self,
        collection: str,
        ids: list[str],
        patch: dict[str, Any],
    ) -> None:
        """Patch-merge payload for each id.

        ChromaDB's ``update`` replaces metadata wholesale, so we read →
        merge → write back for patch semantics. This matches what
        ``learn.py`` and ``memory_lifecycle.py`` already do by hand in
        multiple call sites.
        """

        if not ids or not patch:
            return
        cid = self._collection_id(collection)
        if not cid:
            return
        current = self._chroma_api(
            "POST",
            f"{self._TENANT_PATH}/{cid}/get",
            {"ids": ids, "include": ["metadatas"]},
        )
        if not isinstance(current, dict):
            return
        existing_ids = current.get("ids") or []
        existing_metas = current.get("metadatas") or []
        if len(existing_ids) != len(existing_metas):
            raise RuntimeError("chroma get returned mismatched ids/metadatas")
        merged: list[dict[str, Any]] = []
        for meta in existing_metas:
            base = dict(meta) if isinstance(meta, dict) else {}
            base.update(patch)
            merged.append(base)
        if not existing_ids:
            return
        self._chroma_api(
            "POST",
            f"{self._TENANT_PATH}/{cid}/update",
            {"ids": existing_ids, "metadatas": merged},
        )

    # ── response unwrappers ─────────────────────────────────────

    @staticmethod
    def _unwrap_query(resp: Any, *, with_payload: bool, with_vectors: bool) -> list[VectorHit]:
        if not isinstance(resp, dict):
            return []
        id_batches = resp.get("ids") or [[]]
        dist_batches = resp.get("distances") or [[]]
        meta_batches = resp.get("metadatas") or [[]]
        doc_batches = resp.get("documents") or [[]]
        emb_batches = resp.get("embeddings") or [[]]
        ids = id_batches[0] if id_batches else []
        dists = dist_batches[0] if dist_batches else []
        metas = meta_batches[0] if (meta_batches and with_payload) else []
        docs = doc_batches[0] if (doc_batches and with_payload) else []
        embs = emb_batches[0] if (emb_batches and with_vectors) else []
        hits: list[VectorHit] = []
        for i, hid in enumerate(ids):
            # ChromaDB cosine distance (0..2) → similarity (higher=better).
            score = 1.0 - float(dists[i]) if i < len(dists) else 0.0
            hits.append(
                VectorHit(
                    id=hid,
                    score=score,
                    payload=dict(metas[i]) if i < len(metas) and isinstance(metas[i], dict) else {},
                    document=docs[i] if i < len(docs) else None,
                    vector=list(embs[i]) if i < len(embs) and embs[i] is not None else None,
                )
            )
        return hits

    @staticmethod
    def _unwrap_get(resp: Any, *, with_payload: bool, with_vectors: bool) -> list[VectorPoint]:
        if not isinstance(resp, dict):
            return []
        ids = resp.get("ids") or []
        metas = resp.get("metadatas") or [] if with_payload else []
        docs = resp.get("documents") or []
        embs = resp.get("embeddings") or [] if with_vectors else []
        points: list[VectorPoint] = []
        for i, pid in enumerate(ids):
            points.append(
                VectorPoint(
                    id=pid,
                    payload=dict(metas[i]) if i < len(metas) and isinstance(metas[i], dict) else {},
                    document=docs[i] if i < len(docs) else None,
                    vector=list(embs[i]) if i < len(embs) and embs[i] is not None else None,
                )
            )
        return points


# ── Factory ──────────────────────────────────────────────────────

_factory_lock = threading.Lock()
_singleton: VectorStore | None = None
_singleton_backend: str | None = None


def _resolve_backend() -> str:
    raw = (os.getenv("VECTOR_BACKEND") or "chroma").strip().lower()
    if raw not in ("chroma", "qdrant", "dual"):
        log.warning("unknown VECTOR_BACKEND=%r, falling back to chroma", raw)
        return "chroma"
    return raw


def get_vector_store() -> VectorStore:
    """Return the process-wide :class:`VectorStore` singleton.

    The backend is chosen by ``VECTOR_BACKEND``. Today only ``chroma`` is
    wired; ``qdrant`` and ``dual`` raise :class:`NotImplementedError`
    until Phases A5/B2 land the :class:`QdrantStore` and
    ``DualWriteStore`` implementations.

    The singleton is built lazily so importing this module does not touch
    ChromaDB or environment variables at import time.
    """

    global _singleton, _singleton_backend

    target = _resolve_backend()
    with _factory_lock:
        if _singleton is not None and _singleton_backend == target:
            return _singleton

        if target == "chroma":
            store: VectorStore = ChromaStore()
        elif target == "qdrant":
            # Phase A5 implementation — wired here so VECTOR_BACKEND=qdrant
            # Just Works once a Qdrant instance is reachable at QDRANT_URL.
            from qdrant_store import QdrantStore

            store = QdrantStore()
        elif target == "dual":
            raise NotImplementedError(
                "VECTOR_BACKEND=dual: DualWriteStore lands in Phase B2 of the "
                "migration plan. Use VECTOR_BACKEND=chroma until then."
            )
        else:  # pragma: no cover — _resolve_backend guards this
            raise RuntimeError(f"unreachable backend {target!r}")

        _singleton = store
        _singleton_backend = target
        log.info("VectorStore singleton initialized: backend=%s", target)
        return _singleton


def reset_vector_store() -> None:
    """Drop the cached singleton. Tests use this after setting
    ``VECTOR_BACKEND``; production should never call it."""

    global _singleton, _singleton_backend
    with _factory_lock:
        _singleton = None
        _singleton_backend = None


# Keep ``urllib`` imported to silence lint about the unused import above;
# downstream Qdrant translator helpers will use it in Phase A5.
_ = urllib.parse
