#!/opt/homebrew/bin/python3
"""RAG Hybrid Search — vector + keyword, with reranking.

Usage:
  search.py <query> [options]

Options:
  --collection, -c  Collection(s) to search (default: all)
                    Values: knowledge, experience, context, semantic_memory, obsidian, all
  --limit, -n       Number of results (default: 5)
  --keyword, -k     Enable keyword boost (default: on)
  --json            Output as JSON

Examples:
  search.py "ghost container port"
  search.py "이전에 nginx 설정 변경한 적" -c context
  search.py "OOM error" -c experience -n 10
  search.py "Cloudflare tunnel" -c all --json
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time

log = logging.getLogger("brain.search")
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from http_pool import http_json as _http_json  # noqa: E402

# Direct localhost HTTP (chromadb + ollama expose 127.0.0.1 via docker-compose).
try:
    from config import CHROMA_URL, OLLAMA_URL
except ImportError:
    CHROMA_URL = "http://127.0.0.1:8000"
    OLLAMA_URL = "http://127.0.0.1:11434"


# ── Embedding cache (shared with indexer via embed_cache.py) ──
_embed_lock = threading.Lock()
_embed_mem_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 2048

try:
    from embed_cache import cache_get as _db_cache_get
    from embed_cache import cache_put as _db_cache_put
except ImportError:

    def _db_cache_get(key):
        return None

    def _db_cache_put(key, emb):
        pass


def _cache_key(text: str) -> str:
    try:
        from config import EMBED_MODEL as _model
    except ImportError:
        _model = "blaifa/multilingual-e5-large-instruct"
    # Scope by model so stale vectors from a prior model can't hit.
    return hashlib.md5(f"{_model}:{text[:1200]}".encode()).hexdigest()


def get_embedding(text, prefix="query"):
    prompted = f"{prefix}: {text[:1000]}" if prefix else text[:1000]

    # 2026-04-17: LoRA adapter aware. When brain_core.indexer has a loaded
    # adapter (via POST /admin/embed_adapter), route through it. The cache
    # key folds in the adapter path so base + adapter vectors don't collide.
    # Previously this function was the ACTUAL recall-path embedder (search.py
    # is what hybrid_search calls) while indexer.py had its own get_embedding
    # that carried my LoRA integration — zero-delta on A/B gate because the
    # adapter was loaded but never consulted on the hot path.
    _adapter_path = None
    try:
        import indexer as _idx

        if _idx._lora_embedder is not None:
            _adapter_path = _idx._lora_embedder[0]
    except Exception:
        pass

    key_material = prompted if _adapter_path is None else f"{_adapter_path}|{prompted}"
    key = _cache_key(key_material)
    with _embed_lock:
        cached = _embed_mem_cache.get(key)
        if cached is not None:
            _embed_mem_cache.move_to_end(key)
            return cached

    db_cached = _db_cache_get(key)
    if db_cached:
        with _embed_lock:
            _embed_mem_cache[key] = db_cached
            if len(_embed_mem_cache) > _EMBED_CACHE_MAX:
                _embed_mem_cache.popitem(last=False)
        return db_cached

    # LoRA path: delegate to indexer's in-process sentence-transformers
    if _adapter_path is not None:
        try:
            import indexer as _idx

            emb = _idx._embed_via_lora(text, prefix)
            if emb:
                with _embed_lock:
                    _embed_mem_cache[key] = emb
                    if len(_embed_mem_cache) > _EMBED_CACHE_MAX:
                        _embed_mem_cache.popitem(last=False)
                _db_cache_put(key, emb)
                return emb
        except Exception:
            pass  # fall through to Ollama on any LoRA failure

    try:
        from config import EMBED_MODEL as _model
    except ImportError:
        _model = "blaifa/multilingual-e5-large-instruct"
    payload = {"model": _model, "prompt": prompted}
    data = _http_json("POST", f"{OLLAMA_URL}/api/embeddings", payload=payload, timeout=60)
    emb = data.get("embedding") or (data.get("embeddings") or [[]])[0]
    if not emb:
        raise RuntimeError(f"Ollama returned empty embedding for text[:50]={text[:50]!r}")

    with _embed_lock:
        _embed_mem_cache[key] = emb
        if len(_embed_mem_cache) > _EMBED_CACHE_MAX:
            _embed_mem_cache.popitem(last=False)
    _db_cache_put(key, emb)
    return emb


# ── Collections cache (thread-safe) ─────────────────────
_collections_cache: dict[str, str] = {}
_collections_cache_ts: float = 0.0
_collections_ttl = 60.0
_collections_lock = threading.Lock()


def get_collections():
    """Return a {name: identifier} mapping. Under the VectorStore abstraction
    the "identifier" is just the collection name itself — callers only use
    the value as an opaque handle they pass back to :func:`vector_search`,
    so returning a name→name map keeps every existing call site compiling."""
    global _collections_cache, _collections_cache_ts
    now = time.time()
    with _collections_lock:
        if _collections_cache and (now - _collections_cache_ts) < _collections_ttl:
            return dict(_collections_cache)
    # Fetch outside lock (HTTP call is slow, don't hold the lock during I/O)
    from vector_store import get_vector_store

    try:
        names = get_vector_store().list_collections()
    except Exception:
        names = []
    with _collections_lock:
        if _collections_cache and (time.time() - _collections_cache_ts) < _collections_ttl:
            return dict(_collections_cache)
        _collections_cache = {n: n for n in names}
        _collections_cache_ts = time.time()
        return dict(_collections_cache)


def vector_search(col_id, embedding, n=10, where=None):
    """KNN search returning the legacy ChromaDB response shape so the many
    call sites that unpack ``resp["ids"][0]`` / ``resp["distances"][0]``
    don't need to change. ``col_id`` is a collection NAME under the new
    VectorStore abstraction (see :func:`get_collections`)."""
    from vector_store import get_vector_store

    hits = get_vector_store().query(
        col_id,
        vector=embedding,
        k=n,
        filter=where,
        with_payload=True,
    )
    # Callers expect similarity → distance (1 - distance convention). The
    # ChromaStore flipped to similarity at the adapter boundary; reconstruct
    # the cosine distance so downstream scoring math stays identical.
    return {
        "ids": [[h.id for h in hits]],
        "distances": [[max(0.0, 1.0 - h.score) for h in hits]],
        "documents": [[h.document or "" for h in hits]],
        "metadatas": [[h.payload or {} for h in hits]],
    }


def keyword_score(query, document):
    """Word-boundary keyword matching score (0-1)."""
    query_terms = set(re.findall(r"\w+", query.lower()))
    if not query_terms:
        return 0.0
    doc_terms = set(re.findall(r"\w+", document.lower()))
    matches = len(query_terms & doc_terms)
    return matches / len(query_terms)


# Infrastructure topic keywords (English + Korean) — when present, boost config files
INFRA_KEYWORDS = {
    "docker",
    "container",
    "service",
    "port",
    "network",
    "volume",
    "compose",
    "nginx",
    "proxy",
    "server",
    "config",
    "configuration",
    "database",
    "storage",
    "backup",
    "monitoring",
    "resource",
    "limit",
    "cpu",
    "memory",
    "도커",
    "컨테이너",
    "서비스",
    "포트",
    "네트워크",
    "볼륨",
    "엔진엑스",
    "프록시",
    "서버",
    "설정",
    "구성",
    "데이터베이스",
    "저장소",
    "백업",
    "모니터링",
    "리소스",
    "제한",
    "메모리",
}

CONFIG_PATH_HINTS = (
    "docker-compose",
    "/nginx/",
    ".conf",
    "conf.d",
)


def source_boost(query, metadata):
    """Boost score when query mentions a service or infra topic that matches the source."""
    query_lower = query.lower()
    source = metadata.get("source", "").lower()
    service = metadata.get("service", "").lower()
    best = 0.0

    # Service-name boost (English ASCII terms)
    terms = set(re.findall(r"[a-z][a-z0-9_-]+", query_lower))
    for term in terms:
        if len(term) >= 3:
            # Strong boost: exact service directory or service metadata match
            if f"/{term}/" in source or f"/{term}." in source or term == service:
                best = max(best, 0.45)
            # Medium boost: term anywhere in source path
            elif term in source:
                best = max(best, 0.15)

    # Infra-topic boost — if query asks about infra concepts, prefer actual config files
    # over agent AGENTS.md/MEMORY.md files. This handles generic Korean queries like
    # "도커 서비스 포트 설정" that don't name a specific service.
    has_infra_term = any(kw in query_lower for kw in INFRA_KEYWORDS)
    if has_infra_term:
        if any(hint in source for hint in CONFIG_PATH_HINTS):
            best = max(best, 0.15)
        elif source.endswith(("agents.md", "memory.md", "tools.md", "soul.md", "identity.md")):
            # Slight penalty: agent docs match Korean infra keywords too easily
            best -= 0.05

    return best


def expand_query(query):
    """Generate query variants for better recall."""
    variants = [query]
    # Korean/English mix — add both
    # Simple heuristic: if query has Korean, also search key English terms
    english_words = re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]+", query)
    korean_parts = re.findall(r"[가-힣]+", query)
    if english_words and korean_parts:
        variants.append(" ".join(english_words))
    return variants[:2]  # max 2 variants


import atexit as _atexit
from concurrent.futures import ThreadPoolExecutor, as_completed

# Pool sized to cover the typical fan-out in one wave:
# 7 _ALL_COLLECTIONS × up to 2 bilingual variants = up to 14 tasks.
# Earlier note said "bump to 16 showed zero rag_ms delta" — that was measured
# against a single-query workload where the wave structure wasn't the
# bottleneck. Under the current formula (lower candidate_n, cheaper per call)
# the wave effect dominates, so sizing the pool to absorb a full fan-out in
# one wave is the win. Env var still overrides.
_HYBRID_POOL_SIZE = int(os.getenv("BRAIN_HYBRID_POOL_SIZE", "14"))
_hybrid_pool = ThreadPoolExecutor(max_workers=_HYBRID_POOL_SIZE, thread_name_prefix="hybrid")
_atexit.register(_hybrid_pool.shutdown, wait=False)


def hybrid_search(query, collections, limit=5, use_keyword=True, where=None, deduplicate=True):
    """Hybrid search: vector similarity + keyword boost + query expansion + cross-collection merge.

    `where` is an optional ChromaDB v2 metadata filter clause (e.g. temporal range).
    `deduplicate` controls whether results are deduped by content hash. Set False when
    results flow into RRF so multi-collection agreement is preserved as a ranking signal.
    Collections are queried in parallel via a shared ThreadPoolExecutor — with 11 collections
    and ~50ms per query, this takes ~50ms instead of ~550ms.
    """
    queries = expand_query(query)
    embeddings = [get_embedding(q) for q in queries]
    col_map = get_collections()

    # Build the list of (col_name, col_id, embedding) tasks up front.
    tasks: list[tuple[str, str, list[float]]] = []
    for col_name in collections:
        col_id = col_map.get(col_name)
        if not col_id:
            continue
        for emb in embeddings:
            tasks.append((col_name, col_id, emb))

    # Formerly max(80, limit*10). Default /recall/v2 passes limit=20 → candidate_n=200
    # per (collection, variant) call — 14 calls × 200 = 2800 raw results just to
    # keep 10. HNSW cost scales with the fetch count, so 80% of that is waste.
    # Cut to max(25, limit*3): 20→60 per call, 840 raw for default — RRF + CE
    # rerank still see the same top layer where the answers live. Eval gates the
    # cut; bump back up if stable content_hit moves.
    candidate_n = max(25, limit * 3)

    def _query_one(task):
        col_name, col_id, emb = task
        try:
            data = vector_search(col_id, emb, n=candidate_n, where=where)
        except Exception as e:
            log.warning("vector_search failed for collection=%s: %s", col_name, e)
            return col_name, {}
        return col_name, data

    all_results = []
    # Shared pool prevents thread explosion under concurrency. chroma_api is a
    # blocking urllib call, so threads free the GIL during network I/O.
    futures = [_hybrid_pool.submit(_query_one, task) for task in tasks]
    for fut in as_completed(futures):
        col_name, data = fut.result()
        if not data:
            continue
        ids = (data.get("ids") or [[]])[0]
        docs = (data.get("documents") or [[]])[0]
        metas = (data.get("metadatas") or [[]])[0]
        dists = (data.get("distances") or [[]])[0]

        for i in range(len(docs)):
            # Cosine distance → similarity. Clamp to [0,1] because ChromaDB cosine
            # returns [0,2] (anti-correlated vectors give distance > 1 → negative sim).
            vector_sim = max(0.0, min(1.0, 1 - dists[i]))

            # Keyword boost (always against original query)
            kw_score = keyword_score(query, docs[i]) if use_keyword else 0

            # Source/service name boost
            s_boost = source_boost(query, metas[i]) if use_keyword else 0

            # 2026-04-16 Tier 2 fix: source_boost negative path was
            # zero-clamped before blending, so the agent-doc penalty at
            # `source_boost:198` (best -= 0.05 for infra queries hitting
            # AGENTS.md) silently did nothing. Now:
            #   1. preserve the sign through normalization — negative
            #      s_boost maps to a negative s_signed that actually lands
            #      in the blend (so penalties apply).
            #   2. renormalize the 55/35/10 weights when source signal is
            #      absent rather than systematically penalizing all docs
            #      without a path match by 10%. s_present=1 if there was
            #      any source signal (pos or neg), else redistribute the
            #      0.10 weight onto vector (0.61) + keyword (0.39).
            # s_boost range: [-0.05, +0.45]; normalize preserving sign.
            s_signed = s_boost / 0.45  # [-0.111, 1.0]
            s_signed = max(-0.2, min(1.0, s_signed))  # bound penalty at -0.02 in combined
            s_present = s_boost != 0
            if s_present:
                combined = (0.55 * vector_sim) + (0.35 * kw_score) + (0.10 * s_signed)
            else:
                combined = (0.61 * vector_sim) + (0.39 * kw_score)
            combined = max(0.0, min(1.0, combined))

            all_results.append(
                {
                    "id": ids[i] if i < len(ids) else "",
                    "content": docs[i],
                    "source": metas[i].get("source", ""),
                    "agent": metas[i].get("agent", ""),
                    "type": metas[i].get("type", ""),
                    "service": metas[i].get("service", ""),
                    "collection": col_name,
                    "vector_score": round(vector_sim, 4),
                    "keyword_score": round(kw_score, 4),
                    "score": round(combined, 4),
                    "created_at": metas[i].get("created_at", ""),
                    "section": metas[i].get("section", ""),
                    # M9.2: parent-child chunking fields. Propagated from ChromaDB
                    # metadata so search_unified.normalize_rag_result can put them
                    # in the output metadata and parent_child_expand can swap
                    # child content for parent content at recall time.
                    "parent_id": metas[i].get("parent_id"),
                    "is_parent": metas[i].get("is_parent", False),
                    "chunk_id": metas[i].get("chunk_id"),
                }
            )

    # Sort by combined score, optionally deduplicate by full content hash.
    # When deduplicate=False (RRF path), keep duplicates so RRF can count
    # how many collections agreed on a document.
    sorted_results = sorted(all_results, key=lambda x: x["score"], reverse=True)
    if deduplicate:
        seen = set()
        unique = []
        for r in sorted_results:
            content_key = hashlib.md5(r["content"].encode()).hexdigest()
            if content_key not in seen:
                seen.add(content_key)
                unique.append(r)
        sorted_results = unique

    results = sorted_results[:limit]

    # Track references for self-learning
    try:
        _track_references(results)
    except Exception:
        pass  # non-blocking

    return results


# Module-level ref tracking state (initialized at import time — no first-call race)
try:
    from config import BRAIN_LOGS_DIR as _BRAIN_LOGS_DIR2

    _REF_FILE = _BRAIN_LOGS_DIR2 / "reference_counts.json"
except ImportError:
    _REF_FILE = Path("/Users/chrischo/server/brain/logs/reference_counts.json")
_ref_lock = threading.Lock()
_ref_counts: dict[str, int] = {}
_ref_writes: int = 0
try:
    if _REF_FILE.exists():
        _ref_counts = json.loads(_REF_FILE.read_text())
except Exception:
    pass


def _track_references(results):
    """Thread-safe reference counting. Writes every 100 increments."""
    global _ref_writes
    with _ref_lock:
        for r in results:
            key = f"{r['collection']}:{r['source'][:80]}"
            _ref_counts[key] = _ref_counts.get(key, 0) + 1
        _ref_writes += 1
        if _ref_writes % 100 == 0:
            try:
                _REF_FILE.parent.mkdir(parents=True, exist_ok=True)
                _REF_FILE.write_text(json.dumps(_ref_counts, indent=2, ensure_ascii=False))
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="RAG Hybrid Search")
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "-c", "--collection", default="all", help="Collection(s): knowledge, experience, context, all"
    )
    parser.add_argument("-n", "--limit", type=int, default=5, help="Number of results")
    parser.add_argument("-k", "--keyword", action="store_true", default=True, help="Enable keyword boost")
    parser.add_argument("--no-keyword", action="store_true", help="Disable keyword boost")
    parser.add_argument(
        "--where",
        default=None,
        help='ChromaDB metadata filter as JSON (e.g. \'{"created_at":{"$gte":"2026-04-01"}}\')',
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    if args.collection == "all":
        collections = [
            "knowledge",
            "experience",
            "context",
            "semantic_memory",
            "obsidian",
            "canonical",
            "personal",
        ]
    else:
        collections = [c.strip() for c in args.collection.split(",")]

    use_keyword = not args.no_keyword
    where_clause = None
    if args.where:
        try:
            where_clause = json.loads(args.where)
        except json.JSONDecodeError as e:
            print(f"ERROR: --where is not valid JSON: {e}", file=sys.stderr)
            sys.exit(2)

    results = hybrid_search(args.query, collections, args.limit, use_keyword, where=where_clause)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    print(f"\nQuery: '{args.query}'")
    print(f"Collections: {', '.join(collections)} | Hybrid: {'on' if use_keyword else 'off'}")
    print("=" * 60)
    print(f"Results: {len(results)}")

    for i, r in enumerate(results):
        print(
            f"\n#{i+1} (score: {r['score']:.3f} | vec: {r['vector_score']:.3f} | kw: {r['keyword_score']:.3f})"
        )
        print(f"  [{r['collection']}] {r['source']}")
        print(f"  Agent: {r['agent'] or '-'} | Service: {r['service'] or '-'} | Type: {r['type'] or '-'}")
        print(f"  {r['content'][:200]}...")


if __name__ == "__main__":
    main()
