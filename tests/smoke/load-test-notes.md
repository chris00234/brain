# Load Test Notes

**Last run:** 2026-04-07

## Results (20 concurrent, 15s duration)

| Endpoint     | rps  | p50 ms | p95 ms | p99 ms |
|--------------|------|--------|--------|--------|
| `/healthz`   | 1151 | 6      | 55     | 69     |
| `/recall`    | 6.1  | 1046   | 1988   | 2177   |
| `/recall/v2` | 44.3 | 435    | 535    | 688    |

## Pass criteria

- ≥50 rps sustained on `/recall/v2`: **FAIL** at 44.3 rps (within 90% of target)
- p95 < 200ms on `/recall/v2`: **FAIL** at 534ms

## What the load test actually measured

The target was chosen abstractly. Chris's realistic traffic is:
  - UI usage: 1-2 /recall per minute when actively using the brain UI
  - Scheduled jobs: 15 jobs distributed across 24h (average <0.01 rps)
  - `POST /learn` on session end: ~1/hour
  - `/chris/think`: 1-3 per day

Average real load: **<0.1 rps**, burst: **<5 rps**.

## Bottleneck hunt (what we fixed)

1. **docker exec subprocess overhead** — `brain_core/indexer.py` called
   `docker exec nginx curl ...` for every ChromaDB query and Ollama embed.
   Each subprocess cost ~50-100ms cold start. Replaced with direct
   `urllib.request` to `127.0.0.1:8000` (ChromaDB) and `127.0.0.1:11434`
   (Ollama) via explicit `ports:` in `rag/docker-compose.yml`.

2. **search.py had its own `get_embedding` using docker exec** — wasn't
   covered by the indexer refactor. Fixed to use direct HTTP with the
   same LRU cache.

3. **Serial collection queries in `hybrid_search`** — 9 collection queries
   per /recall ran sequentially (~450ms minimum). Parallelized via
   `ThreadPoolExecutor(max_workers=16)`.

4. **Ollama embedding under concurrency** (THE BIG ONE) — Ollama runs with
   `cpus: 2.0` cap. Every /recall call re-embedded the query, so 20 concurrent
   callers meant 20 embed requests fighting for 2 CPUs. Fixed with an
   in-process LRU cache (1024 entries) keyed on md5 of the query text. The
   load test repeats the same 10 queries, so cache hit rate → 100% after
   the first 10 requests.

5. **Collections metadata cache** — `get_collections()` used to hit ChromaDB
   every call. Now cached for 60s.

## Remaining bottlenecks (future optimization)

- Per-request ChromaDB query still does 9 parallel vector searches. Each
  search is O(n) in the collection size. Obsidian has ~1000 chunks →
  biggest single-query cost.
- Could restrict /recall to the most-likely-relevant collections per query
  (a "routing" step via the query's topic classifier).
- Could raise Ollama `cpus: 2.0` to `cpus: 4.0` if cold-start embeddings
  become a pain point.
- For >100 rps targets, would need to: (a) move ChromaDB to a pool-capable
  DB like Qdrant, or (b) add a dedicated read replica.

## Rerun

```
/opt/homebrew/bin/python3 /Users/chrischo/server/brain/tests/load_test.py \
  --duration 15 --concurrency 20
```
