"""Deployment-gate quality-dimension coverage guard.

The stable eval set is the only deployment-blocking recall gate
(``eval_run_stable`` daily + ``recall_v2_content_hit_pct`` SLO read it).
Before 2026-06-11 it was 138 uncategorized lookup cases with zero coverage
of the dimensions Chris judges memory quality by (stale-truth supersession,
temporal history preservation, noise/privacy negatives, identity canon over
stale provenance) — a regression in the recall-governance stack could ship
without turning the gate red. These tests pin that coverage so the quality
slice cannot be silently dropped or de-fanged.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STABLE_SET = ROOT / "cli" / "eval_set_stable.json"


def _load_cases() -> list[dict]:
    return json.loads(STABLE_SET.read_text())


def test_stable_set_is_well_formed():
    cases = _load_cases()
    assert len(cases) >= 145
    queries = [c["query"] for c in cases]
    assert len(queries) == len(set(queries)), "duplicate queries in stable set"
    for case in cases:
        assert case["query"].strip()
    # Promoted quality-slice cases must carry real expectations (the runner
    # auto-passes empty expected fields, which would de-fang the slice).
    for case in cases:
        if case.get("category"):
            assert case.get("expected_content") or case.get("expected_source"), case["query"]


def test_stable_set_covers_stale_truth_supersession():
    cases = [c for c in _load_cases() if c.get("category") == "stale_fact_supersession"]
    assert len(cases) >= 3, "deployment gate lost stale-truth supersession coverage"
    # At least one Korean-language case: supersession quality is bilingual.
    assert any(any("가" <= ch <= "힣" for ch in c["query"]) for c in cases)
    # At least one history-preservation counterpart: demoting stale truth must
    # never make the old fact unreachable for history/provenance queries.
    assert any("history" in c["query"].lower() for c in cases)


def test_stable_set_covers_noise_negative_with_forbidden_content():
    cases = [c for c in _load_cases() if c.get("category") == "privacy_negative_personal_source"]
    assert cases, "deployment gate lost privacy/noise-negative coverage"
    assert any(c.get("forbidden_content") for c in cases), (
        "noise-negative cases must carry forbidden_content so the gate fails "
        "when raw personal content leaks into recall"
    )


def test_stable_set_covers_identity_canon_over_stale_provenance():
    cases = [c for c in _load_cases() if c.get("category") == "identity_canon_over_stale_provenance"]
    assert cases, "deployment gate lost identity-canon provenance coverage"


def test_stable_set_covers_clean_hit_topk_noise():
    cases = [c for c in _load_cases() if c.get("category") == "clean_hit_topk_noise"]
    assert cases, "deployment gate lost clean-hit top-k noise coverage"
    assert any(
        c["query"]
        == "Brain recall quality noise prefetch empty summary Claude Code session canonical_first current useful context eval score"
        for c in cases
    )
    assert any(c.get("forbidden_content") for c in cases), (
        "clean-hit top-k cases must carry forbidden_content so the gate fails "
        "when proposed/session-summary tail rows leak into top-k"
    )
