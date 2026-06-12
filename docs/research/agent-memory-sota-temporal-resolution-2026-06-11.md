# Agent-memory SOTA review → read-time temporal resolution (2026-06-11)

Mission: verify SOTA agent-memory leads, pick ONE low/medium-risk high-ROI
pattern applicable to Brain now, implement repo-local with tests. Outcome:
**read-time entity-property temporal resolution** in recall governance
(`brain_core/recall_governance/temporal_resolution.py`, applied by
`/recall/v2` as `_apply_temporal_resolution_inplace`).

## Verified sources

| Lead | Status | Citation |
|---|---|---|
| Mem0 | verified | Chhikara et al., "Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory", arXiv:2504.19413 (2025) |
| Zep/Graphiti | verified | Rasmussen et al., "Zep: A Temporal Knowledge Graph Architecture for Agent Memory", arXiv:2501.13956 (2025) |
| APEX-MEM | verified | Banerjee et al., "APEX-MEM: Agentic Semi-Structured Memory with Temporal Reasoning for Long-Term Conversational AI", ACL 2026, arXiv:2604.14362 |
| Tenure / structured belief state | verified (single-author preprint, self-benchmarked — treat claims cautiously) | Flynt, "Structured Belief State and the First Precision-Aware Benchmark for LLM Memory Retrieval", arXiv:2605.11325v2 (2026) |

Secondary (survey sweep): LOCOMO arXiv:2402.17753 (5 QA categories:
single-hop / multi-hop / temporal / open-domain / adversarial); LongMemEval
arXiv:2410.10813 (adds Knowledge Updates + Abstention); Memora/FAMA
arXiv:2604.20006 (forgetting-aware metric penalizing reliance on obsolete
memory); State Contamination arXiv:2605.16746 (summary compression launders
noise below detection thresholds; pre-summarization sanitization is the fix);
A-Mem arXiv:2502.12110; MemGPT arXiv:2310.08560; CRAG arXiv:2401.15884.

## Key findings vs Brain's architecture

What the papers prescribe that Brain already has: authority/source tiers
(`source_authority.AuthorityTier` ≈ Tenure's epistemic status), summary
contamination suppression (`is_low_authority_result`, the parent card's
matched-route residue filter), write-time op classification
(`memory_operations.classify_operation` ≈ Mem0's ADD/UPDATE/DELETE/NOOP, but
deterministic and cheaper), supersession chains + `valid_from`/`valid_until`
on atoms, and CRAG-style retry.

The confirmed gap (cross-checked in code): **no read-time arbitration between
two LIVE durable rows that contradict on the same entity-property.**
`ingest_mirror` invalidates at cosine < 0.70 and keeps both at ≥ 0.85, so the
0.70–0.85 window — typical for same-frame value swaps ("uses vim" → "uses
neovim", "port 8791" → "port 9100") — stores both atoms unlinked. At recall,
durable collections get no time decay and `_sort_and_diversify` has no
recency key, so the stale fact can outrank its replacement indefinitely.
`fact_store.py` has true (entity, attribute, value) supersession with
validity intervals but is not wired into recall at all. `conflict_surfacer`
detects exactly this pair class — but nightly, capped at 5 review tasks/run,
write-side only.

This is the gap the literature is loudest about: APEX-MEM's central result is
that append-only storage + **retrieval-time** resolution ("most recent valid
entry" selected at query time) beats eager write-time consolidation by
15–25pp on temporal questions (90.63% vs Mem0 75.71 / MIRIX 65.62 on LOCOMO
temporal). Zep's core mechanism is the same shape: a newer contradicting fact
soft-invalidates the older edge (sets `t_invalid`), never deletes. Temporal
reasoning is also LOCOMO's weakest category across all systems (73% below
human in the original paper).

## Selected pattern and rejections

**Selected: read-time entity-property temporal invalidation as a ranking
demotion.** Detection is structural, conflict_surfacer-parity heuristics
(shared token frame + polarity flip / numeric mismatch / 1–2-token value
swap, dates stripped as provenance noise), older row by `created_at` gets the
decisive −160 governance penalty (`temporal_resolution_stale_penalty`) —
same demote-never-drop family as `vanished_source_penalty`. No LLM, no IO,
no storage mutation, fail-open everywhere; skipped for historical-intent
requests (`include_history`/`include_obsolete`/`as_of`). General mechanism —
no topic/keyword markers — so it covers the class the OpenClaw/Hermes parent
card had to address per-route.

Rejected for this card:
- **Authority/source tier gate** (Tenure/AuthorityBench): already exists
  (`source_authority.py`); marginal ROI.
- **Session-summary contamination filter** (State Contamination): largely
  exists (low-authority penalties + parent card's residue filter); the
  remaining write-side pre-distill gate is a different, bigger card.
- **Full Graphiti/APEX-MEM structured store** (bi-temporal KG, 35-class
  ontology, LLM contradiction detection per ingest): violates the cost
  contract (LLM per write) and the no-wholesale-replacement constraint;
  the read-time demotion captures the highest-value slice at zero LLM cost.
- **Retrieval-time structured projection**: needs reliable subject/predicate
  extraction (only `_extract_preference_subject` exists); higher risk, weaker
  precedent.
- **LOCOMO eval taxonomy**: partial overlap already in
  `cli/eval_set_adversarial.json` categories (`stale_fact_supersession`);
  worth extending later, not the highest-ROI code change.

## Evidence (tests, 2026-06-11)

`tests/unit/test_recall_temporal_resolution.py` — 19 tests. Before/after
control: stale row (score 90, 2026-01) vs newer replacement (score 80,
2026-06) ranks `[old, new]` before the stage and `[new, old]` after, newer
row untouched. Negative controls: same-value restatements (numeric
corroboration veto), capture-date differences, complementary same-subject
facts, different-scope texts, low-authority/superseded/route-guarantee rows,
missing/tied timestamps — all untouched. Parent-card OpenClaw/Hermes route
guarantee suites unchanged and green (329 tests); full unit suite green.

## Known boundaries (v1) / next contract

- `created_at` is transaction time, not event time — no bi-temporality. A
  backdated statement of an old fact would win recency. Next: thread
  `valid_from` (already on atoms) as the event-time key when present.
- Subsuming statements ("Hermes is current; OpenClaw historical" ⊃ "OpenClaw
  is current runtime") don't fire the value-swap signal (zero exclusive
  tokens on one side); that class stays covered by route guarantees. Next:
  a subset-frame + invalidity-marker signal, evaluated against FAMA-style
  negative controls.
- Verb-paraphrase pairs ("prefers X"/"likes X") can demote the older copy of
  an equivalent fact — benign reordering (content survives via the newer
  row), never loss.
- Next contract candidates: (1) wire the same detector into Hermes provider
  prefetch (strictest surface); (2) add `stale_fact_supersession`-category
  eval cases to `cli/eval_set_adversarial.json` exercising live contradictory
  durable pairs end-to-end; (3) feed detected pairs to `conflict_surfacer`'s
  review-task path so read-time detections become write-time supersessions.
