"""eval_sweep_matrix.py — experiment matrix for /recall/v2 iterative tuning.

Pure data. Each entry is a single tunable knob:

    knob         : short name, used in logs and state file
    file         : absolute path of the file to patch
    anchor_tpl   : format template that produces the exact line(s) in the file.
                   `old_string` is `anchor_tpl.format(**baseline)`;
                   `new_string` is `anchor_tpl.format(**value)`.
                   Using literal content (not line numbers) makes patches
                   robust against upstream edits.
    baseline     : dict of values that reproduce the current line
    values       : list of dicts — each one gets tested in order
    latency_risk : "none" | "low" | "medium" | "high" — informational only
    hypothesis   : one-line rationale for logs

The driver consumes these one (knob, value_idx) pair at a time. Every decision
is keep-or-revert against the current rolling baseline, so the order in `values`
only affects which local maximum is found first, not correctness.
"""

from __future__ import annotations

BRAIN_ROOT = "/Users/chrischo/server/brain"

MATRIX: list[dict] = [
    # ──────────────────────────────────────────────────────────────────────
    # Knob 1 — Cross-encoder blend ratio (original × 0.4 + CE × 0.6 baseline)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "ce_blend_ratio",
        "file": f"{BRAIN_ROOT}/brain_core/cross_encoder_rerank.py",
        "anchor_tpl": "        blended = original * {a} + ce_norm * 100 * {b}",
        "baseline": {"a": "0.4", "b": "0.6"},
        "values": [
            {"a": "0.3", "b": "0.7"},
            {"a": "0.5", "b": "0.5"},
            {"a": "0.2", "b": "0.8"},
            {"a": "0.25", "b": "0.75"},
        ],
        "latency_risk": "none",
        "hypothesis": "CE is the semantic ground truth; lean heavier on it",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 2 — Cross-encoder top_k window
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "ce_top_k",
        "file": f"{BRAIN_ROOT}/server.py",
        "anchor_tpl": "                fused = rerank_with_cross_encoder(q, fused, top_k=min(n * 3, {k}))",
        "baseline": {"k": "30"},
        "values": [
            {"k": "40"},
            {"k": "50"},
            {"k": "20"},
        ],
        "latency_risk": "medium",
        "hypothesis": "more candidates → CE sees more truth; +10-30ms per +10",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 3 — Trust boost tiers (canonical vs experience)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "trust_boost",
        "file": f"{BRAIN_ROOT}/brain_core/rerank.py",
        "anchor_tpl": "    trust_boost = {{3: {t3}, 2: {t2}}}.get(trust_tier, 1.0)",
        "baseline": {"t3": "1.4", "t2": "1.15"},
        "values": [
            {"t3": "1.3", "t2": "1.1"},
            {"t3": "1.5", "t2": "1.2"},
            {"t3": "1.25", "t2": "1.05"},
            {"t3": "1.6", "t2": "1.25"},
        ],
        "latency_risk": "none",
        "hypothesis": "tier 3 may over-weight canonical, crowding out correct experience hits",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 4 — Title/body overlap weights in the relevance multiplier
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "overlap_weights",
        "file": f"{BRAIN_ROOT}/brain_core/rerank.py",
        "anchor_tpl": "    relevance = 1.0 + ({t} * title_overlap) + ({b} * body_overlap)",
        "baseline": {"t": "2.0", "b": "1.0"},
        "values": [
            {"t": "2.5", "b": "1.5"},
            {"t": "3.0", "b": "0.5"},
            {"t": "1.5", "b": "1.5"},
            {"t": "2.0", "b": "0.5"},
        ],
        "latency_risk": "none",
        "hypothesis": "token overlap may be oversold; CE stage 2 already catches semantic",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 5 — semantic_boost gate (vector_score threshold)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "semantic_boost",
        "file": f"{BRAIN_ROOT}/brain_core/rerank.py",
        "anchor_tpl": "    semantic_boost = 1.0 + ({m} * vector_score) if vector_score > {th} else 1.0",
        "baseline": {"m": "0.3", "th": "0.7"},
        "values": [
            {"m": "0.3", "th": "0.6"},
            {"m": "0.4", "th": "0.7"},
            {"m": "0.5", "th": "0.65"},
            {"m": "0.3", "th": "0.8"},
        ],
        "latency_risk": "none",
        "hypothesis": "current gate may trigger too rarely; amplify semantic signal",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 6 — MMR lambda (relevance vs diversity balance)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "mmr_lambda",
        "file": f"{BRAIN_ROOT}/brain_core/config.py",
        "anchor_tpl": 'BRAIN_MMR_LAMBDA = float(os.getenv("BRAIN_MMR_LAMBDA", "{v}"))  # 0.85 = strongly relevance-biased; 0.6 was too aggressive on single-shot QA',
        "baseline": {"v": "0.85"},
        "values": [
            {"v": "0.95"},
            {"v": "0.75"},
            {"v": "0.9"},
            {"v": "0.7"},
        ],
        "latency_risk": "none",
        "hypothesis": "confidence skip already catches bad cases; tune remaining balance",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 7 — MMR confidence-skip threshold (how ambiguous must top-N be)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "mmr_conf_skip",
        "file": f"{BRAIN_ROOT}/brain_core/search_unified.py",
        "anchor_tpl": "            and (_nth_score / _top_score) >= {v}",
        "baseline": {"v": "0.85"},
        "values": [
            {"v": "0.92"},
            {"v": "0.75"},
            {"v": "0.9"},
            {"v": "0.8"},
        ],
        "latency_risk": "none",
        "hypothesis": "tighter skip = less MMR interference; looser = more diversification",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 8 — Source diversity cap (max results per source file in top window)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "source_diversity_cap",
        "file": f"{BRAIN_ROOT}/brain_core/search_unified.py",
        "anchor_tpl": "        if source_counts[src] <= {v}:",
        "baseline": {"v": "3"},
        "values": [
            {"v": "2"},
            {"v": "5"},
            {"v": "4"},
        ],
        "latency_risk": "none",
        "hypothesis": "affects top-5 source spread — tighter = more variety, looser = more depth",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 9 — RRF smoothing constant k (Cormack 2009 default 60)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "rrf_k",
        "file": f"{BRAIN_ROOT}/brain_core/rrf.py",
        "anchor_tpl": "DEFAULT_K = {v}",
        "baseline": {"v": "60"},
        "values": [
            {"v": "30"},
            {"v": "100"},
            {"v": "15"},
            {"v": "45"},
        ],
        "latency_risk": "none",
        "hypothesis": "lower k = ranks matter more; higher k = smoother blending across fan-out",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 10 — Chroma fan-out (how many candidates per source before RRF)
    # This is the CONTENT recall lever — wider fan-out = more material for
    # the cross-encoder to promote. With limit=5 baseline is 10 items/source.
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "chroma_fanout",
        "file": f"{BRAIN_ROOT}/brain_core/search_unified.py",
        "anchor_tpl": "        raw_results = search_rag(query, limit * {v}, where=local_where or None, collections=collections)",
        "baseline": {"v": "2"},
        "values": [
            {"v": "6"},
            {"v": "10"},
            {"v": "15"},
            {"v": "20"},
        ],
        "latency_risk": "medium",
        "hypothesis": "widening fan-out gives CE more candidates to promote — biggest expected content@5 lever",
    },
    # ──────────────────────────────────────────────────────────────────────
    # Knob 11 — Stage-1 rerank window (how many items CE actually scores)
    # ──────────────────────────────────────────────────────────────────────
    {
        "knob": "rerank_window",
        "file": f"{BRAIN_ROOT}/brain_core/search_unified.py",
        "anchor_tpl": "        unique = _rerank(relevance_query, unique, top_k=limit * {v})",
        "baseline": {"v": "2"},
        "values": [
            {"v": "4"},
            {"v": "6"},
            {"v": "10"},
            {"v": "15"},
        ],
        "latency_risk": "medium",
        "hypothesis": "more items through rerank → CE sees more candidates that were lost by stage-1 cap",
    },
]


def get(knob_idx: int) -> dict:
    return MATRIX[knob_idx]


def knob_count() -> int:
    return len(MATRIX)


if __name__ == "__main__":
    # Smoke test: print matrix summary and verify anchors are well-formed
    print(f"Sweep matrix: {knob_count()} knobs")
    for i, k in enumerate(MATRIX):
        old = k["anchor_tpl"].format(**k["baseline"])
        print(f"\n[{i}] {k['knob']}  ({len(k['values'])} values, {k['latency_risk']} latency risk)")
        print(f"    file: {k['file'].split('/')[-1]}")
        print(f"    baseline: {old[:100]}{'...' if len(old) > 100 else ''}")
        for j, v in enumerate(k["values"]):
            new = k["anchor_tpl"].format(**v)
            print(f"    value[{j}]: {new[:100]}{'...' if len(new) > 100 else ''}")
