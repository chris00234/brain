"""brain_core/qdrant_store.py — Qdrant backend for the VectorStore protocol.

Phase A5 of the Qdrant migration. Mirrors the ChromaStore surface in
:mod:`brain_core.vector_store` so swapping is a one-env-var flip
(``VECTOR_BACKEND=qdrant``).

Deployment assumption: Qdrant runs at :envvar:`QDRANT_URL` (default
``http://127.0.0.1:6333``). ``get_vector_store`` factory in
``vector_store.py`` instantiates this class lazily so nothing imports
``qdrant_client`` unless the backend is actually selected.

Filter translation: the protocol accepts Chroma-native ``where`` dicts
(``{"status": "active"}``, ``{"agent": {"$eq": "chris"}}``,
``{"$and": [...]}``, ``{"chunk_id": {"$in": [...]}}``). Every call site
that ever touched a filter is written in this shape, so QdrantStore
translates at its boundary instead of asking every caller to switch
syntax. See :func:`_translate_filter`.

Scoring convention: Qdrant cosine distance returns similarity
(``score``) in ``[-1, 1]`` where higher is better. ChromaStore flipped
distance → similarity via ``1 - distance`` at its boundary. The
protocol contract is "higher similarity = better match", so QdrantStore
just passes the native score through.

ID format: Qdrant points use either unsigned int or UUID string as id.
ChromaDB used arbitrary strings (e.g. ``semantic_memory:abc123``,
``code_5e302ab…``). We preserve the original string id in
``payload["original_id"]`` and use a deterministic UUIDv5 as the Qdrant
id so lookups by string remain O(1).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from vector_store import Filter, VectorHit, VectorPoint

log = logging.getLogger("brain.qdrant_store")

# Deterministic namespace for UUIDv5 hashing so the same ChromaDB string id
# always produces the same Qdrant id. Chosen once at migration start; do
# not change or every id stops resolving.
_ID_NAMESPACE = uuid.UUID("2f4c3d10-9e4a-4c3f-9a8a-2e0a1b2c3d4e")


# Legacy collection aliases — the 13→7 topology collapse folded several
# ChromaDB collections under one Qdrant target. Callers that still reference
# the old names (many of them — brain_core/proactive.py, learn.py,
# memory_lifecycle.py, server.py all have bare strings like
# "semantic_contradictions") get transparently redirected: the real
# collection is the target, with an extra filter predicate AND-merged onto
# whatever the caller supplied. Keeps the Qdrant cutover zero-caller-churn.
_COLLECTION_ALIASES: dict[str, tuple[str, dict]] = {
    "semantic_contradictions": ("semantic_memory", {"kind": "contradiction"}),
    "canonical_raptor": ("canonical", {"raptor_level": {"$gt": 0}}),
    "experience_compressed": ("experience", {"compressed": True}),
    "context": ("knowledge", {"origin": "context"}),
    "patterns": ("knowledge", {"origin": "patterns"}),
}


def _resolve_alias(collection: str, where: Any) -> tuple[str, Any]:
    """Translate a legacy collection name to (real_collection, merged_filter).
    If the name isn't aliased, return unchanged."""
    if collection not in _COLLECTION_ALIASES:
        return collection, where
    real, extra = _COLLECTION_ALIASES[collection]
    if not where:
        merged = dict(extra)
    elif "$and" in where:
        merged = {"$and": [*where["$and"], extra]}
    else:
        merged = {"$and": [where, extra]}
    return real, merged


def _string_to_uuid(string_id: str) -> str:
    """Map an arbitrary string id to a stable UUIDv5."""
    return str(uuid.uuid5(_ID_NAMESPACE, string_id))


def _translate_filter(where: Filter) -> Any | None:
    """Translate a Chroma-native ``where`` dict to a Qdrant ``Filter``.

    Supports the operators actually used in the codebase (audited from
    Phase 1): ``$eq``, ``$ne``, ``$in``, ``$nin``, ``$and``, ``$or``,
    ``$gt``, ``$gte``, ``$lt``, ``$lte``. Plain ``{"key": "value"}``
    is treated as ``{"key": {"$eq": "value"}}``.
    """

    if not where:
        return None
    # Local import so importing this module is cheap when the factory
    # picks ChromaStore instead.
    from qdrant_client.models import (
        FieldCondition,
        MatchAny,
        MatchExcept,
        MatchValue,
        Range,
    )
    from qdrant_client.models import (
        Filter as QFilter,
    )

    must: list[FieldCondition | QFilter] = []
    must_not: list[FieldCondition | QFilter] = []
    should: list[FieldCondition | QFilter] = []

    for key, value in where.items():
        if key == "$and":
            # value is list of sub-filters — AND == must of translated children.
            for sub in value:
                sub_f = _translate_filter(sub)
                if sub_f is not None:
                    must.append(sub_f)
            continue
        if key == "$or":
            for sub in value:
                sub_f = _translate_filter(sub)
                if sub_f is not None:
                    should.append(sub_f)
            continue

        if isinstance(value, dict):
            for op, arg in value.items():
                if op == "$eq":
                    must.append(FieldCondition(key=key, match=MatchValue(value=arg)))
                elif op == "$ne":
                    must_not.append(FieldCondition(key=key, match=MatchValue(value=arg)))
                elif op == "$in":
                    must.append(FieldCondition(key=key, match=MatchAny(any=list(arg))))
                elif op == "$nin":
                    must.append(FieldCondition(key=key, match=MatchExcept(**{"except": list(arg)})))
                elif op in ("$gt", "$gte", "$lt", "$lte"):
                    range_kwargs = {
                        "$gt": "gt",
                        "$gte": "gte",
                        "$lt": "lt",
                        "$lte": "lte",
                    }[op]
                    must.append(FieldCondition(key=key, range=Range(**{range_kwargs: arg})))
                else:
                    log.warning("qdrant filter: unknown operator %s on key %s", op, key)
        else:
            # Plain equality shortcut.
            must.append(FieldCondition(key=key, match=MatchValue(value=value)))

    if not (must or must_not or should):
        return None
    return QFilter(must=must or None, must_not=must_not or None, should=should or None)


def _resolve_qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://127.0.0.1:6333")


def _search_params_for(collection: str) -> Any:
    """Per-collection HNSW runtime params.

    hnsw_tuner keeps recommended ef_search per collection (tuned for
    p95 vs recall tradeoffs). Reading it here so the adaptive tuner's
    recommendations actually flow into live queries without every
    caller having to pass SearchParams explicitly.
    """
    try:
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
        from hnsw_tuner import SETTINGS  # type: ignore[import]
    except Exception:
        return None
    ef = SETTINGS.get(collection)
    if not ef:
        return None
    from qdrant_client.models import SearchParams

    return SearchParams(hnsw_ef=int(ef))


class QdrantStore:
    """VectorStore backed by Qdrant ≥ 1.14 via the Python client."""

    name = "qdrant"

    # Default vector config: 1024-dim cosine (multilingual-e5-large-instruct).
    # The cli/qdrant_bootstrap.py script creates collections with this plus
    # named-vector + sparse config per the plan; this default is only used
    # when a caller creates a collection through ``create_collection`` without
    # explicit schema, i.e., the legacy one-arg path.
    DEFAULT_VECTOR_SIZE = 1024

    def __init__(self, url: str | None = None) -> None:
        from qdrant_client import QdrantClient

        self._url = url or _resolve_qdrant_url()
        self._client = QdrantClient(url=self._url, timeout=30)
        # Many callers pass legacy string ids; we cache the mapping for
        # the duration of a process rather than re-hash every call.
        self._id_cache: dict[str, str] = {}

    # ── helpers ──────────────────────────────────────────────────

    def _qid(self, string_id: str) -> str:
        cached = self._id_cache.get(string_id)
        if cached:
            return cached
        qid = _string_to_uuid(string_id)
        self._id_cache[string_id] = qid
        return qid

    def _hit_payload(self, payload: dict | None) -> tuple[dict, str, str | None]:
        """Split a Qdrant payload into (user_payload, original_id, document).

        ``original_id`` and ``document`` are stored as reserved keys by
        :meth:`upsert`. Callers want payload without those.
        """
        if not payload:
            return {}, "", None
        p = dict(payload)
        original_id = p.pop("_original_id", "")
        document = p.pop("_document", None)
        return p, original_id, document

    # ── VectorStore implementation ──────────────────────────────

    def heartbeat(self) -> bool:
        try:
            # `get_collections` is the cheapest healthcheck; Qdrant has no
            # dedicated heartbeat endpoint on the REST API.
            self._client.get_collections()
            return True
        except Exception as exc:
            log.debug("qdrant heartbeat failed: %s", exc)
            return False

    def list_collections(self) -> list[str]:
        try:
            resp = self._client.get_collections()
        except Exception as exc:
            log.warning("qdrant list_collections failed: %s", exc)
            return []
        return [c.name for c in (resp.collections or [])]

    def create_collection(self, name: str, metadata: dict[str, Any] | None = None) -> None:
        from qdrant_client.models import Distance, VectorParams

        del metadata  # Qdrant has no collection-level metadata dict; schema
        # lives in the named-vector config. Bootstrap script handles full
        # schema; this path keeps existing code compiling.
        # Legacy alias: caller asking for "semantic_contradictions" doesn't
        # need a new collection — it's folded into semantic_memory already.
        real, _ = _resolve_alias(name, None)
        try:
            existing = {c.name for c in (self._client.get_collections().collections or [])}
            if real in existing:
                return
            self._client.create_collection(
                collection_name=real,
                vectors_config=VectorParams(
                    size=self.DEFAULT_VECTOR_SIZE,
                    distance=Distance.COSINE,
                ),
            )
        except Exception as exc:
            log.warning("qdrant create_collection(%s) failed: %s", name, exc)

    def count(self, collection: str) -> int:
        real, alias_filter = _resolve_alias(collection, None)
        try:
            if alias_filter:
                resp = self._client.count(
                    collection_name=real,
                    count_filter=_translate_filter(alias_filter),
                    exact=True,
                )
            else:
                resp = self._client.count(collection_name=real, exact=True)
            return int(resp.count or 0)
        except Exception:
            return 0

    def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        documents: list[str] | None = None,
    ) -> None:
        from qdrant_client.models import PointStruct

        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError(
                f"upsert length mismatch: ids={len(ids)} vectors={len(vectors)} payloads={len(payloads)}"
            )
        if documents is not None and len(documents) != len(ids):
            raise ValueError(f"upsert length mismatch: ids={len(ids)} documents={len(documents)}")

        # Legacy alias: inject the discriminator payload so the caller doesn't
        # have to know about the 13→7 collapse. e.g. an upsert to the legacy
        # "semantic_contradictions" name lands in semantic_memory with
        # kind=contradiction stamped on the payload.
        real, alias_filter = _resolve_alias(collection, None)
        alias_patch: dict[str, Any] = {}
        if alias_filter:
            # alias_filter is a dict like {"kind": "contradiction"} or
            # {"raptor_level": {"$gt": 0}}. The upsert path wants literal
            # values to stamp; extract bare equality terms only.
            for k, v in alias_filter.items():
                if isinstance(v, dict):
                    continue  # Skip operator dicts; caller must set these explicitly
                alias_patch[k] = v

        points: list[PointStruct] = []
        for i, (sid, vec, payload) in enumerate(zip(ids, vectors, payloads, strict=True)):
            merged = dict(payload or {})
            merged.update(alias_patch)
            # Reserved keys so get()/query() can return original string id and document.
            merged["_original_id"] = sid
            if documents is not None:
                merged["_document"] = documents[i]
            points.append(PointStruct(id=self._qid(sid), vector={"dense": vec}, payload=merged))
        self._client.upsert(collection_name=real, points=points, wait=False)

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
        # qdrant-client 1.17+ uses `query_points` instead of deprecated
        # `search`. Bootstrap guarantees every collection has a primary
        # `dense` named vector; `canonical` additionally has `contextual`
        # populated on contextualized rows, so we issue a multi-vector
        # prefetch + RRF fusion for it. Non-canonical collections stay on
        # single-vector dense search.
        real, merged_filter = _resolve_alias(collection, filter)
        search_params = _search_params_for(real)
        qfilter = _translate_filter(merged_filter)

        if real == "canonical":
            try:
                from qdrant_client.models import FusionQuery, Prefetch

                resp = self._client.query_points(
                    collection_name=real,
                    prefetch=[
                        Prefetch(
                            query=vector,
                            using="dense",
                            limit=max(k * 3, 30),
                            filter=qfilter,
                            params=search_params,
                        ),
                        Prefetch(
                            query=vector,
                            using="contextual",
                            limit=max(k * 3, 30),
                            filter=qfilter,
                            params=search_params,
                        ),
                    ],
                    query=FusionQuery(fusion="rrf"),
                    limit=k,
                    with_payload=with_payload,
                    with_vectors=with_vectors,
                )
                hits = resp.points
            except Exception as exc:
                log.warning("qdrant hybrid-query(%s) failed, falling back to dense: %s", collection, exc)
                hits = []
        else:
            hits = []

        if not hits:
            try:
                resp = self._client.query_points(
                    collection_name=real,
                    query=vector,
                    using="dense",
                    limit=k,
                    query_filter=qfilter,
                    search_params=search_params,
                    with_payload=with_payload,
                    with_vectors=with_vectors,
                )
                hits = resp.points
            except Exception as exc:
                log.warning("qdrant query(%s) failed: %s", collection, exc)
                return []

        results: list[VectorHit] = []
        for h in hits:
            user_payload, original_id, document = self._hit_payload(h.payload if with_payload else None)
            # Named-vector collections return vector as {name: [...]}; pull
            # the `dense` slot.
            vec: list[float] | None = None
            if with_vectors and h.vector is not None:
                vec = list(h.vector.get("dense") or []) if isinstance(h.vector, dict) else list(h.vector)
            results.append(
                VectorHit(
                    id=original_id or str(h.id),
                    score=float(h.score),
                    payload=user_payload,
                    document=document,
                    vector=vec,
                )
            )
        return results

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
        real, merged_filter = _resolve_alias(collection, filter)
        if ids is not None:
            try:
                records = self._client.retrieve(
                    collection_name=real,
                    ids=[self._qid(sid) for sid in ids],
                    with_payload=with_payload or with_documents,
                    with_vectors=with_vectors,
                )
            except Exception as exc:
                log.warning("qdrant get-by-ids(%s) failed: %s", collection, exc)
                return []
            return [
                self._record_to_point(r, with_documents=with_documents, with_vectors=with_vectors)
                for r in records
            ]

        # Filter + paginate path uses scroll.
        try:
            points, _next = self._client.scroll(
                collection_name=real,
                scroll_filter=_translate_filter(merged_filter),
                limit=limit or 500,
                offset=offset or None,
                with_payload=with_payload or with_documents,
                with_vectors=with_vectors,
            )
        except Exception as exc:
            log.warning("qdrant scroll(%s) failed: %s", collection, exc)
            return []
        return [
            self._record_to_point(r, with_documents=with_documents, with_vectors=with_vectors) for r in points
        ]

    def _record_to_point(self, record: Any, *, with_documents: bool, with_vectors: bool) -> VectorPoint:
        user_payload, original_id, document = self._hit_payload(record.payload)
        raw_vec = getattr(record, "vector", None)
        vec: list[float] | None = None
        if with_vectors and raw_vec is not None:
            vec = list(raw_vec.get("dense") or []) if isinstance(raw_vec, dict) else list(raw_vec)
        return VectorPoint(
            id=original_id or str(record.id),
            payload=user_payload,
            document=document if with_documents else None,
            vector=vec,
        )

    def delete(self, collection: str, ids: list[str]) -> None:
        from qdrant_client.models import PointIdsList

        if not ids:
            return
        real, _ = _resolve_alias(collection, None)
        try:
            self._client.delete(
                collection_name=real,
                points_selector=PointIdsList(points=[self._qid(sid) for sid in ids]),
                wait=False,
            )
        except Exception as exc:
            log.warning("qdrant delete(%s) failed: %s", collection, exc)

    def update_payload(
        self,
        collection: str,
        ids: list[str],
        patch: dict[str, Any],
    ) -> None:
        if not ids or not patch:
            return
        real, _ = _resolve_alias(collection, None)
        try:
            self._client.set_payload(
                collection_name=real,
                payload=patch,
                points=[self._qid(sid) for sid in ids],
                wait=False,
            )
        except Exception as exc:
            log.warning("qdrant set_payload(%s) failed: %s", collection, exc)
