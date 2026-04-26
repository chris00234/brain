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
import threading
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
    """Per-collection HNSW + quantization runtime params.

    - hnsw_ef: pulled from hnsw_tuner.SETTINGS (per-collection p95 / recall
      tradeoff). Previously dead code; wired here so tuner recommendations
      flow into live queries.
    - quantization.rescore=True: bootstrap stores vectors in int8 with
      full fp32 on NVMe. Setting rescore=True asks Qdrant to fetch the
      fp32 vectors for the top candidates and re-score them — small
      latency cost (~5-15ms), meaningful quality win at high k.
    """
    from qdrant_client.models import QuantizationSearchParams, SearchParams

    ef = _hnsw_settings().get(collection)
    quant = QuantizationSearchParams(ignore=False, rescore=True, oversampling=2.0)
    return SearchParams(hnsw_ef=int(ef) if ef else None, quantization=quant)


_HNSW_SETTINGS_CACHE: dict[str, int] | None = None
_HNSW_SETTINGS_LOCK = threading.Lock()


def _hnsw_settings() -> dict[str, int]:
    """Lazy-load + cache hnsw_tuner.SETTINGS once per process.

    Previously re-imported on every query and leaked `sys.path` entries
    without bound on long-running processes. The import failure path
    returns an empty dict so callers silently skip ef_search override.

    Lock-guarded first-init so two concurrent recall threads on a fresh
    process don't both parse the tuning log.
    """
    global _HNSW_SETTINGS_CACHE
    if _HNSW_SETTINGS_CACHE is not None:
        return _HNSW_SETTINGS_CACHE
    with _HNSW_SETTINGS_LOCK:
        # Re-check under lock; a concurrent init may have already landed.
        if _HNSW_SETTINGS_CACHE is not None:
            return _HNSW_SETTINGS_CACHE
        import json
        import sys
        from pathlib import Path

        pipeline_dir = str(Path(__file__).resolve().parent / "pipeline")
        if pipeline_dir not in sys.path:
            sys.path.append(pipeline_dir)

        merged: dict[str, int] = {}
        try:
            from hnsw_tuner import SETTINGS  # type: ignore[import-not-found]

            merged.update(SETTINGS)
        except Exception as exc:
            log.debug("hnsw_tuner SETTINGS import failed: %s", exc)

        # Close the adaptive feedback loop: hnsw_tuner.adaptive_tune appends
        # recommendation rows to logs/hnsw_tuning.jsonl but nothing ever read
        # them back. Merge the latest recommendation per collection on top of
        # the static defaults so live queries pick up the tuner's decisions.
        try:
            tuning_log = Path("/Users/chrischo/server/brain/logs/hnsw_tuning.jsonl")
            if tuning_log.exists():
                latest: dict[str, int] = {}
                with tuning_log.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception as exc:
                            log.debug("hnsw_tuning.jsonl parse error: %s", exc)
                            continue
                        col = rec.get("collection")
                        rec_ef = rec.get("recommended_ef") or rec.get("new_ef")
                        if col and isinstance(rec_ef, int) and rec_ef > 0:
                            latest[col] = rec_ef  # last write wins (file is append-only)
                merged.update(latest)
        except Exception as exc:
            log.debug("hnsw_tuning.jsonl read failed: %s", exc)

        _HNSW_SETTINGS_CACHE = merged
        return _HNSW_SETTINGS_CACHE


class QdrantStore:
    """VectorStore backed by Qdrant ≥ 1.14 via the Python client."""

    name = "qdrant"

    # Default vector config: 1024-dim cosine (multilingual-e5-large-instruct).
    # The cli/qdrant_bootstrap.py script creates collections with this plus
    # named-vector + sparse config per the plan; this default is only used
    # when a caller creates a collection through ``create_collection`` without
    # explicit schema, i.e., the legacy one-arg path.
    DEFAULT_VECTOR_SIZE = 1024

    # Bound the id cache so a long-running server process can't grow it
    # unbounded as new memories stream in. 50000 entries x ~120 bytes
    # approx 6 MB ceiling; UUIDv5 hashing on miss is cheap (~us), so LRU
    # eviction is free to discard cold ids.
    _ID_CACHE_MAX = 50_000

    def __init__(self, url: str | None = None) -> None:
        from collections import OrderedDict

        from qdrant_client import QdrantClient

        self._url = url or _resolve_qdrant_url()
        self._client = QdrantClient(url=self._url, timeout=30, check_compatibility=False)
        self._id_cache: OrderedDict[str, str] = OrderedDict()
        self._id_cache_lock = threading.Lock()
        self._sparse_cache: dict[str, bool] = {}
        self._sparse_cache_lock = threading.Lock()

    # ── helpers ──────────────────────────────────────────────────

    def _qid(self, string_id: str) -> str:
        with self._id_cache_lock:
            cached = self._id_cache.get(string_id)
            if cached is not None:
                self._id_cache.move_to_end(string_id)
                return cached
        qid = _string_to_uuid(string_id)
        with self._id_cache_lock:
            self._id_cache[string_id] = qid
            self._id_cache.move_to_end(string_id)
            if len(self._id_cache) > self._ID_CACHE_MAX:
                self._id_cache.popitem(last=False)
        return qid

    def _build_prefetch(
        self,
        *,
        real: str,
        vector: list[float],
        query_text: str | None,
        k: int,
        qfilter: Any,
        search_params: Any,
    ) -> list:
        """Build the per-named-vector Prefetch list for hybrid fusion.

        - Every collection gets a dense prefetch.
        - Canonical additionally gets a contextual prefetch.
        - If query_text is supplied AND the collection actually has a
          populated sparse slot, also adds a sparse prefetch.

        Returns an empty list if no hybrid prefetch makes sense (single
        dense path handled by caller).
        """
        from qdrant_client.models import Prefetch, SparseVector

        # Asymmetric prefetch depths:
        # - dense is the primary recall source → wide prefetch (k*3, min 30)
        # - contextual / sparse are tie-breakers for exact wording and
        #   prefix-enriched semantics → narrower (k*2, min 20) to avoid
        #   diluting dense ranking before the top-level rescore.
        # Tuned 2026-04-21 against cli/eval_set_stable.json.
        dense_limit = max(k * 3, 30)
        aux_limit = max(k * 2, 20)
        prefetch_list: list[Prefetch] = [
            Prefetch(
                query=vector,
                using="dense",
                limit=dense_limit,
                filter=qfilter,
                params=search_params,
            )
        ]
        if real == "canonical":
            prefetch_list.append(
                Prefetch(
                    query=vector,
                    using="contextual",
                    limit=aux_limit,
                    filter=qfilter,
                    params=search_params,
                )
            )
        if query_text and self._has_sparse(real):
            try:
                from sparse_tokenizer import encode as sparse_encode

                indices, values = sparse_encode(query_text)
                if indices:
                    prefetch_list.append(
                        Prefetch(
                            query=SparseVector(indices=indices, values=values),
                            using="sparse",
                            limit=aux_limit,
                            filter=qfilter,
                        )
                    )
            except Exception as exc:
                log.debug("qdrant sparse encode failed for %s: %s", real, exc)

        # Hybrid only makes sense with 2+ prefetches; 1 prefetch = plain dense.
        return prefetch_list if len(prefetch_list) >= 2 else []

    def _has_sparse(self, collection: str) -> bool:
        """Cheap, cached check: does this collection have a `sparse` named slot?"""
        with self._sparse_cache_lock:
            hit = self._sparse_cache.get(collection)
            if hit is not None:
                return hit
        # Perform the Qdrant RPC outside the lock to avoid serializing
        # unrelated collection probes under concurrent hybrid_search fan-out.
        try:
            info = self._client.get_collection(collection_name=collection)
            sparse_config = getattr(getattr(info, "config", None), "params", None)
            sparse_vectors = getattr(sparse_config, "sparse_vectors", None) or {}
            has = "sparse" in sparse_vectors
        except Exception:
            # Transient RPC failures (Qdrant restart, blip) must not cache a
            # permanent False — that would silently downgrade the collection
            # to dense-only for the rest of the process lifetime.
            return False
        with self._sparse_cache_lock:
            self._sparse_cache[collection] = has
        return has

    def _has_dense_vector(self, collection: str) -> bool:
        try:
            info = self._client.get_collection(collection_name=collection)
            params = getattr(getattr(info, "config", None), "params", None)
            vectors = getattr(params, "vectors", None)
            if isinstance(vectors, dict):
                return "dense" in vectors
            return False
        except Exception:
            return False

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
                if real == "healthcheck_probe" and not self._has_dense_vector(real):
                    self._client.delete_collection(collection_name=real)
                else:
                    return
            self._client.create_collection(
                collection_name=real,
                vectors_config={
                    "dense": VectorParams(
                        size=self.DEFAULT_VECTOR_SIZE,
                        distance=Distance.COSINE,
                    )
                },
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

        has_sparse = self._has_sparse(real)
        sparse_encode = None
        sparse_tokenizer_version: str | None = None
        if has_sparse and documents is not None:
            try:
                from sparse_tokenizer import SPARSE_TOKENIZER_VERSION
                from sparse_tokenizer import encode as _se

                sparse_encode = _se
                sparse_tokenizer_version = SPARSE_TOKENIZER_VERSION
            except Exception:
                sparse_encode = None

        points: list[PointStruct] = []
        for i, (sid, vec, payload) in enumerate(zip(ids, vectors, payloads, strict=True)):
            merged = dict(payload or {})
            merged.update(alias_patch)
            # Reserved keys so get()/query() can return original string id and document.
            merged["_original_id"] = sid
            point_vectors: dict[str, Any] = {"dense": vec}
            if documents is not None:
                merged["_document"] = documents[i]
                if sparse_encode is not None:
                    indices, values = sparse_encode(documents[i] or "")
                    if indices:
                        from qdrant_client.models import SparseVector

                        point_vectors["sparse"] = SparseVector(indices=indices, values=values)
                        # Stamp the tokenizer version on the point so a
                        # reindex job can detect sparse-schema drift and
                        # regen just the stale rows, not the whole corpus.
                        merged["sparse_tokenizer_version"] = sparse_tokenizer_version
            points.append(PointStruct(id=self._qid(sid), vector=point_vectors, payload=merged))
        # wait=True: hot-path brain_store writes must be durable before the
        # POST /memory handler returns. A Qdrant restart within the ack
        # window would otherwise silently drop the write.
        self._client.upsert(collection_name=real, points=points, wait=True)

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
        include_document: bool = True,
    ) -> list[VectorHit]:
        # qdrant-client 1.17+ uses `query_points`. Hybrid search combines
        # the primary `dense` vector with the collection's extra named
        # vectors: `contextual` for canonical (prefix-enriched dense) and
        # `sparse` for all 7 (BM25 via Qdrant IDF modifier). `query_text`
        # is required to compute the sparse vector; if absent, sparse is
        # skipped and hybrid collapses to dense (+ contextual for canonical).
        real, merged_filter = _resolve_alias(collection, filter)
        search_params = _search_params_for(real)
        qfilter = _translate_filter(merged_filter)

        prefetch_list = self._build_prefetch(
            real=real,
            vector=vector,
            query_text=query_text,
            k=k,
            qfilter=qfilter,
            search_params=search_params,
        )

        # Payload selector: when the caller doesn't need the chunk text
        # (fan-out ranking stages), ask Qdrant to exclude `_document` from
        # the returned payload. Cuts response size by ~500B-2KB per point,
        # meaningful on the ~14 parallel /recall/v2 fan-out queries.
        payload_arg: Any = with_payload
        if with_payload and not include_document:
            from qdrant_client.models import PayloadSelectorExclude

            payload_arg = PayloadSelectorExclude(exclude=["_document"])

        hits: list = []
        if prefetch_list:
            # Qdrant 1.17 hybrid pattern: prefetch broadly with hybrid
            # fusion, then rescore the merged top candidates with pure
            # dense cosine. RRF alone gave strong content recall but
            # demoted dense-top-ranked source hits (eval source@5 dropped
            # 5.1pts on 2026-04-21 regression). Rescoring with dense at
            # the top level restores source ordering while keeping sparse
            # and contextual as recall-widening signals.
            try:
                from qdrant_client.models import FusionQuery, Prefetch

                fusion_prefetch = Prefetch(
                    prefetch=prefetch_list,
                    query=FusionQuery(fusion="rrf"),
                    limit=max(k * 3, 30),
                )
                resp = self._client.query_points(
                    collection_name=real,
                    prefetch=fusion_prefetch,
                    query=vector,
                    using="dense",
                    limit=k,
                    query_filter=qfilter,
                    search_params=search_params,
                    with_payload=payload_arg,
                    with_vectors=with_vectors,
                )
                hits = resp.points
            except Exception as exc:
                log.warning("qdrant hybrid-query(%s) failed, falling back to dense: %s", collection, exc)
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
                    with_payload=payload_arg,
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
        # Payload projection: callers that don't need the chunk text ask
        # Qdrant to omit `_document` from the returned payload. Saves
        # bandwidth proportional to doc size times row count.
        want_payload = bool(with_payload or with_documents)
        if want_payload and with_payload and not with_documents:
            from qdrant_client.models import PayloadSelectorExclude

            payload_arg: Any = PayloadSelectorExclude(exclude=["_document"])
        else:
            payload_arg = want_payload

        if ids is not None:
            try:
                records = self._client.retrieve(
                    collection_name=real,
                    ids=[self._qid(sid) for sid in ids],
                    with_payload=payload_arg,
                    with_vectors=with_vectors,
                )
            except Exception as exc:
                log.warning("qdrant get-by-ids(%s) failed: %s", collection, exc)
                return []
            return [
                self._record_to_point(r, with_documents=with_documents, with_vectors=with_vectors)
                for r in records
            ]

        # Filter + paginate path uses Qdrant's cursor-based scroll.
        #
        # Qdrant's `offset` on scroll is a POINT ID (UUID/int/str), NOT a
        # numeric row offset. The earlier implementation passed `offset=500,
        # 1000, ...` which Qdrant tried to interpret as a point id, matched
        # nothing, and kept returning the first page — turning callers'
        # "paginate forward" loops into infinite loops that blew RAM to
        # several GB (surfaced via pipeline/episode_binder.py hanging at
        # ~7GB RSS during the 2026-04-21 Qdrant sparse rebuild).
        #
        # Correct translation: walk Qdrant's opaque `next_offset` cursor
        # from the start, skip the first `offset` rows, then return the
        # next `limit`. O(offset) per call — fine for small offsets; callers
        # doing full iteration should use offset=0 + limit=large.
        target_skip = max(0, int(offset or 0))
        target_take = int(limit or 500)
        page_size = min(target_take + target_skip, 500)
        if page_size <= 0:
            return []
        collected: list[VectorPoint] = []
        cursor: Any = None
        skipped = 0
        MAX_PAGES = 2000  # safety bound: 2000 * 500 = 1M rows max walked per call
        for _ in range(MAX_PAGES):
            try:
                points, cursor = self._client.scroll(
                    collection_name=real,
                    scroll_filter=_translate_filter(merged_filter),
                    limit=page_size,
                    offset=cursor,
                    with_payload=payload_arg,
                    with_vectors=with_vectors,
                )
            except Exception as exc:
                log.warning("qdrant scroll(%s) failed: %s", collection, exc)
                return collected
            if not points:
                break
            for r in points:
                if skipped < target_skip:
                    skipped += 1
                    continue
                collected.append(
                    self._record_to_point(r, with_documents=with_documents, with_vectors=with_vectors)
                )
                if len(collected) >= target_take:
                    return collected
            if cursor is None:
                break
        return collected

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
            # wait=True so an upsert-then-delete sequence can't end up with
            # the delete persisting while the preceding write is still
            # un-ack'd (and therefore lost on a Qdrant restart).
            self._client.delete(
                collection_name=real,
                points_selector=PointIdsList(points=[self._qid(sid) for sid in ids]),
                wait=True,
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
                wait=True,
            )
        except Exception as exc:
            log.warning("qdrant set_payload(%s) failed: %s", collection, exc)
