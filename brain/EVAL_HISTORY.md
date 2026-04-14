# Brain eval history

Tracks `/recall/v2` retrieval quality over time on `cli/eval_set_*.json` fixtures. `hit_content@5` is the primary metric — it measures whether the expected content substring appears in any of the top-5 retrieved chunks. `hit_source@5` measures whether the expected source file is in top-5. Latency is mean p50 on a warm brain.

## Stable eval — small, high-signal, no-regression gate

File: `cli/eval_set_stable.json` (138 queries, curated canonical + infra)

| Date | Commit | n | source_hit | content_hit | p50 ms | Notable changes |
|---|---|---|---|---|---|---|
| 2026-04-10 08:13 | bbe6ff2 (initial) | 118 | 91.5% | 96.6% | 108 | Brain post rounds 6/7/8, LoRA pipeline scaffolded |
| 2026-04-13 13:50 | 06e310d (Phase A) | 138 | 91.3% | 95.7% | 327 | Expanded set +20, production hardening, integration test scaffold (latency jump is test infra overhead) |
| 2026-04-13 ~21:00 | cd2a141 (post Phase L / M5 / M6 / M9) | 138 | (not recomputed) | **98.6%** | ~340 | Phase L code health sweep + CRAG scaffold + SearXNG learning loop + slowapi rate limiting. Stable eval baseline file is stale — actual content_hit via live `eval_compare.py` is 98.6. |

## Extended eval — large, hard-set, headroom tracker

File: `cli/eval_set_extended.json` (606 queries, mined from Apple Notes, iMessage, Obsidian, Gmail, sessions)

| Date | Commit | n | source_hit | content_hit | content_loose | p50 ms | Notable |
|---|---|---|---|---|---|---|---|
| 2026-04-13 13:55 | 06e310d (Phase A) | 606 | 78.4% | **68.2%** | 78.2% | 330 | Phase A baseline snapshot. Loose-vs-strict gap of 10pt → many retrieved chunks are in the neighborhood but miss exact substring. |

## Historical recall — pre-git

User memory, undated (late March → early April 2026). Reported as "80 / 70" (approximately `source_hit=80%` and `content_hit=70%`) — likely on a Round 8 or Round 9 era eval set, pre-Round 10 neuromorphic retrieval work. No versioned file exists for that measurement; treat as rough narrative anchor, not a reproducible baseline.

Rounds 6-9 shipped 60+ bug fixes + LoRA finetune pipeline scaffolding. Round 10 (`~/.claude/plans/round10-neuromorphic-brain.md`) landed HippoRAG spreading activation, salience ranking, MMR diversity, time decay, graph entity boost — the major lifts that moved stable from ~70% → 96%+.

## Phase M7 targets

Phase M7's ralph loop is aiming at the extended set specifically. The stable set is close to ceiling and stays above a regression floor.

| Set | Metric | Current (2026-04-13) | M7 target | Rationale |
|---|---|---|---|---|
| Stable | `hit_content_pct` | 98.6% | ≥ 94.0% floor | Can't regress; verify harness gates on this |
| Stable | p50 ms | ~340 | ≤ 500 | Acceptable with CRAG default-on |
| Extended | `hit_content_pct` | **68.2%** | **≥ 80%** (+11.8pt) | Headline M7 goal; biggest architectural levers are CRAG default-on + HippoRAG2 query-triple linking + Docling PDF ingest of new surface |
| Extended | `hit_source_pct` | 78.4% | ≥ 85% (+6.6pt) | Secondary |
| Extended | p50 ms | 330 | ≤ 500 | CRAG adds a retry hop on low-confidence queries |

## Lineage — what moves this number

Ordered by historical impact:

1. **Query expansion / HyDE** (Round 6-7) — `cli/eval_compare.py --hyde --expand` adds +3-5pt content on ambiguous queries at +50-150ms cost. Not yet in the sweep matrix.
2. **Cross-encoder rerank** (Round 8) — `brain_core/cross_encoder_rerank.py`. Single biggest stable-set lift: +8-12pt on content_hit. Already default-on.
3. **RRF + trust-weighted fusion** (Round 9) — `brain_core/rrf.py`, `brain_core/rerank.py` tier tables. +2-4pt on extended set, no latency cost.
4. **HippoRAG spreading activation** (Round 10 A1) — `brain_core/spreading_activation.py`. +3-6pt on follow-up queries where prior session warmed the entity subgraph.
5. **Atoms truth layer with supersession** (v2 Phase 1) — `brain_core/atoms_store.py`. Filters out superseded chunks before fan-out. Key for preference-shift questions.
6. **Phase L code-health cleanup** (cd2a141 and predecessors) — `c75393a` (half_open breaker bypass, BEGIN IMMEDIATE race fixes), `14b020b` (CE top_k 50→20, search_obsidian dedup, pool reuse). Small per-fix deltas that compounded into the stable 95.7 → 98.6 jump.
7. **Phase M9 CRAG (iterative retrieval)** — `brain_core/crag.py`, opt-in via `?iterative=true` in `/recall/v2`. Not yet evaluated against extended set. Phase M7 WS3 flips the default to on.
8. **Phase M6 SearXNG learning loop** — `brain_core/web_search.py`. Augments recall with live web results when trust score is high. Currently unused (see WS6).

## How to refresh this document

```bash
# Stable
cd /Users/chrischo/server/brain
./.venv/bin/python cli/eval_compare.py --json --eval-set cli/eval_set_stable.json | jq '.v2'

# Extended
./.venv/bin/python cli/eval_compare.py --json --eval-set cli/eval_set_extended.json | jq '.v2'
```

Update the table row-by-row. Include commit SHA, date, and one-line "notable changes". Do not overwrite prior rows — append.
