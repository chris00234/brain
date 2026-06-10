from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
BRAIN_CORE = BRAIN_ROOT / "brain_core"
if str(BRAIN_CORE) not in sys.path:
    sys.path.insert(0, str(BRAIN_CORE))


def test_decide_context_collapses_duplicate_brain_quality_hits(monkeypatch):
    """brain_decide's non-recall evidence path should share retrieval quality
    filtering so duplicate eval-score memories do not dominate evidence.
    """
    import reasoning

    def fake_search_collection(query, limit=10, collections=None, domain=None):
        if collections == ["canonical"]:
            return [
                {
                    "id": "canonical_truth",
                    "path": "/canonical/brain-quality.md",
                    "title": "Brain quality decision",
                    "collection": "canonical",
                    "content": "Brain fine-tuning should improve measurable eval-score improvements, not vibes.",
                    "score": 0.8,
                },
                {
                    "id": "live_state",
                    "path": "/canonical/live-state.md",
                    "title": "Brain live-state suppression",
                    "collection": "canonical",
                    "content": "Live status and quota questions should use live tools instead of stale memory prefetch.",
                    "score": 0.7,
                },
            ]
        if collections == ["semantic_memory"]:
            return [
                {
                    "id": "old_semantic",
                    "path": "/atoms/a",
                    "title": "Brain eval preference",
                    "collection": "semantic_memory",
                    "content": "Chris wants Brain fine tuning judged by measurable eval score improvements.",
                    "score": 0.99,
                }
            ]
        return []

    monkeypatch.setattr(reasoning, "_search_collection", fake_search_collection)
    monkeypatch.setattr(reasoning.rerank, "rerank", lambda query, rows, top_k=12: rows)
    monkeypatch.setattr(reasoning.time_decay, "apply_to_results", lambda rows: rows)
    monkeypatch.setattr(reasoning, "_fetch_chris_corrections_for_domain", lambda domain: [])
    monkeypatch.setattr(reasoning, "get_chris_profile", lambda: "")

    hits, _profile, _context = reasoning.gather_decision_context(
        "Brain recall quality should improve eval score and avoid noisy duplicate prefetch",
        [reasoning.DecisionOption(label="A"), reasoning.DecisionOption(label="B")],
        agent="liz",
        domain="brain",
    )

    eval_hits = [hit for hit in hits if "eval" in hit.content.lower()]
    assert len(eval_hits) == 1
    assert any("Live status" in hit.content for hit in hits)


def test_decide_context_applies_recall_governance_canonical_boost(monkeypatch):
    """gather_decision_context must share /recall/v2's score-governance pass so
    canonical-accepted truth outranks a generic canonical note that would
    otherwise win on RRF rank alone.

    Both rows are canonical so RRF cannot disambiguate; the generic note sits
    first in the source list (RRF rank 0) and so out-scores the
    canonical-accepted decision (rank 1) absent governance. After applying
    `_apply_recall_governance_inplace`, the accepted decision picks up
    +18 (canonical_accepted) +18 (specific_truth) +8 (canonical_truth) and
    rises to the top — matching the contract that brain_decide evidence
    promotes durable truth the same way `/recall/v2` does.
    """
    import reasoning

    def fake_search_collection(query, limit=10, collections=None, domain=None):
        if collections == ["canonical"]:
            return [
                # Rank 0 in the canonical list ⇒ wins RRF by default.
                {
                    "id": "canonical_generic",
                    "path": "/canonical/generic-note.md",
                    "title": "General architecture note",
                    "collection": "canonical",
                    "content": "Use whichever protocol fits the team's experience.",
                    "score": 0.05,
                    "metadata": {"category": "note"},
                },
                # Rank 1 — governance must lift this above the generic note.
                {
                    "id": "canonical_decision",
                    "path": "/canonical/protobuf-decision.md",
                    "title": "Adopt protobuf for service-to-service",
                    "collection": "canonical",
                    "content": "Chris's decision: use protobuf for all service-to-service RPC.",
                    "score": 0.05,
                    "metadata": {"category": "decision", "review_state": "accepted"},
                },
            ]
        return []

    monkeypatch.setattr(reasoning, "_search_collection", fake_search_collection)
    monkeypatch.setattr(reasoning.rerank, "rerank", lambda query, rows, top_k=12: rows)
    monkeypatch.setattr(reasoning.time_decay, "apply_to_results", lambda rows: rows)
    monkeypatch.setattr(reasoning, "_fetch_chris_corrections_for_domain", lambda domain: [])
    monkeypatch.setattr(reasoning, "get_chris_profile", lambda: "")

    hits, _profile, _context = reasoning.gather_decision_context(
        "should we adopt protobuf for service to service rpc",
        [reasoning.DecisionOption(label="A"), reasoning.DecisionOption(label="B")],
        agent="liz",
        domain="coding",
    )

    assert hits, "expected at least one PreferenceHit"
    assert hits[0].content.startswith(
        "Chris's decision: use protobuf"
    ), f"expected canonical-accepted decision to win after governance boost; got {hits[0].content!r}"


def test_reason_deep_provenance_applies_governance_and_quality_filter(monkeypatch):
    """reason_deep (brain_reason / MCP brain_reason) must run results through
    the same recall governance + retrieval quality pass as /recall/v2, so its
    provenance evidence is not dominated by stale generic summaries or
    near-duplicate brain-quality preferences.

    Today reason_deep skips both filters entirely and returns raw RRF +
    token-rerank + decay output. After this change, the provenance list must:
      1. Drop the generic "Knowledge gap bridge: Brain system dependency"
         summary noise that the shared retrieval-quality filter suppresses for
         brain-quality queries.
      2. Collapse the duplicated eval-score preference (semantic + canonical)
         to a single canonical-accepted row.
      3. Place the canonical-accepted truth above the higher-raw-score semantic
         memory thanks to the governance boost.
    """
    import reasoning

    def fake_search_collection(query, limit=8, collections=None, domain=None):
        if collections == ["canonical"]:
            return [
                {
                    "id": "canonical_truth",
                    "path": "/canonical/brain-quality.md",
                    "title": "Brain quality decision",
                    "collection": "canonical",
                    "content": (
                        "Brain fine-tuning should improve measurable eval-score" " improvements, not vibes."
                    ),
                    "score": 0.40,
                    "metadata": {"category": "decision", "review_state": "accepted"},
                },
                {
                    "id": "generic_summary",
                    "path": "weekly/2026-W20.md",
                    "title": "W20 weekly brain summary",
                    "collection": "canonical",
                    "content": (
                        "Knowledge Gap Bridge: Brain system dependency. Brain depends"
                        " on FastAPI brain-server and native Qdrant."
                    ),
                    "score": 0.90,
                    "metadata": {"category": "summary"},
                },
            ]
        if collections == ["semantic_memory"]:
            return [
                {
                    "id": "semantic_dupe",
                    "path": "/atoms/a",
                    "title": "Brain eval preference",
                    "collection": "semantic_memory",
                    "content": (
                        "Chris wants Brain fine tuning judged by measurable eval" " score improvements."
                    ),
                    "score": 0.99,
                    "metadata": {"category": "preference"},
                }
            ]
        return []

    monkeypatch.setattr(reasoning, "_search_collection", fake_search_collection)
    monkeypatch.setattr(reasoning.rerank, "rerank", lambda query, rows, top_k=10: rows)
    monkeypatch.setattr(reasoning.time_decay, "apply_to_results", lambda rows: rows)
    monkeypatch.setattr(reasoning, "get_chris_profile", lambda: "")

    # Stub LLM dispatch so we exercise only the retrieval/provenance path.
    monkeypatch.setattr(
        reasoning,
        "dispatch",
        lambda **_kwargs: type(
            "Fake",
            (),
            {"ok": False, "text": "", "model": "stub"},
        )(),
    )

    result = reasoning.reason_deep(
        "Brain recall quality should improve eval score and avoid noisy duplicate prefetch",
        agent="claude",
        domain="brain",
    )

    contents = [hit.content for hit in result.provenance]
    # Quality filter must suppress the generic infra-summary noise.
    assert not any(
        "Knowledge Gap Bridge" in c for c in contents
    ), f"expected stale brain-summary noise to be dropped; got {contents}"
    # Near-duplicate eval-score memories must collapse to a single row.
    eval_hits = [c for c in contents if "eval" in c.lower() and "score" in c.lower()]
    assert len(eval_hits) == 1, f"expected duplicate eval-score memories collapsed; got {eval_hits}"
    # Canonical-accepted truth must be ranked first thanks to governance boost.
    assert result.provenance, "expected at least one provenance hit"
    assert result.provenance[0].collection == "canonical"
