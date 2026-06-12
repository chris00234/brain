"""Read-time entity-property temporal resolution (recall_governance.temporal_resolution).

SOTA grounding: Zep/Graphiti soft edge invalidation (arXiv:2501.13956) and
APEX-MEM retrieval-time temporal resolution (arXiv:2604.14362) — see
docs/research/agent-memory-sota-temporal-resolution-2026-06-11.md.

Positive controls prove a stale contradicted durable row demotes below its
newer replacement at ranking time; negative controls prove restatements,
complementary facts, low-authority rows, route-guarantee synthetics, and
timestamp-less rows are never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _durable(content, created_at, *, rid="atom", score=50.0, collection="semantic_memory"):
    return {
        "id": rid,
        "title": "",
        "content": content,
        "collection": collection,
        "source_type": "rag",
        "score": score,
        "created_at": created_at,
        "metadata": {"category": "fact"},
    }


# ── is_conflicting_statement_pair: positive controls ─────────────────────


def test_value_swap_conflict_detected():
    """The exact class write-time supersession misses: same statement frame,
    one swapped value token (cosine 0.70-0.85 window)."""
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert is_conflicting_statement_pair(
        "Chris uses vim as his primary editor on the homelab",
        "Chris uses neovim as his primary editor on the homelab",
    )


def test_runtime_value_swap_conflict_detected():
    """The OpenClaw->Hermes runtime swap, stated symmetrically — the general
    mechanism catches the class the route guarantee covers by topic."""
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert is_conflicting_statement_pair(
        "OpenClaw is Chris's current agent runtime for the homelab",
        "Hermes is Chris's current agent runtime for the homelab",
    )


def test_numeric_mismatch_conflict_detected():
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert is_conflicting_statement_pair(
        "Brain server listens on port 8791 behind the personal webhook",
        "Brain server listens on port 9100 behind the personal webhook",
    )


def test_polarity_flip_conflict_detected():
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert is_conflicting_statement_pair(
        "Ollama generation is enabled for brain synthesis jobs",
        "Ollama generation is disabled for brain synthesis jobs",
    )


# ── is_conflicting_statement_pair: negative controls ─────────────────────


def test_restatement_with_matching_value_is_not_conflict():
    """Equal numeric signatures corroborate — a paraphrase restating the SAME
    value must never read as a contradiction."""
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert not is_conflicting_statement_pair(
        "Brain server listens on port 8791 behind the personal webhook",
        "The brain server is listening on port 8791 behind the personal webhook",
    )


def test_unrelated_topics_are_not_conflict():
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert not is_conflicting_statement_pair(
        "Chris uses neovim as his primary editor",
        "Docker containers on the homelab restart nightly via launchd",
    )


def test_complementary_facts_same_subject_are_not_conflict():
    """Different attributes of the same entity coexist (low frame overlap)."""
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert not is_conflicting_statement_pair(
        "Brain runs as a Docker container on the homelab",
        "Brain exposes recall and memory routes over FastAPI with bearer auth",
    )


def test_capture_dates_are_not_numeric_conflict():
    """Date/clock-shaped digits are provenance noise, not fact values — two
    true statements captured on different days must not pair."""
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert not is_conflicting_statement_pair(
        "Captured 2026-05-01: Chris deploys every service as a Docker container",
        "Captured 2026-06-11: Chris deploys every service as a Docker container",
    )


def test_different_scope_texts_are_not_conflict():
    """Length-ratio gate (conflict_surfacer parity): a one-liner never pairs
    with a long document that merely embeds opposing-polarity vocabulary."""
    from recall_governance.temporal_resolution import is_conflicting_statement_pair

    assert not is_conflicting_statement_pair(
        "Ollama generation enabled for synthesis",
        "Ollama generation was evaluated across brain synthesis jobs and found "
        "disabled in some environments; the rollout doc lists synthesis jobs, "
        "embedding fallbacks, scheduling windows, and operator notes in detail",
    )


# ── stale_conflict_pairs: eligibility guards ─────────────────────────────


def test_pairs_orders_older_first_regardless_of_list_order():
    from recall_governance.temporal_resolution import stale_conflict_pairs

    newer = _durable(
        "Chris uses neovim as his primary editor on the homelab",
        "2026-06-01T00:00:00Z",
        rid="new",
    )
    older = _durable(
        "Chris uses vim as his primary editor on the homelab",
        "2026-01-01T00:00:00Z",
        rid="old",
    )
    assert stale_conflict_pairs([newer, older]) == [(1, 0)]
    assert stale_conflict_pairs([older, newer]) == [(0, 1)]


def test_missing_timestamp_never_pairs():
    from recall_governance.temporal_resolution import stale_conflict_pairs

    a = _durable("Chris uses vim as his primary editor on the homelab", "")
    b = _durable("Chris uses neovim as his primary editor on the homelab", "2026-06-01T00:00:00Z")
    assert stale_conflict_pairs([a, b]) == []


def test_equal_timestamps_never_pair():
    from recall_governance.temporal_resolution import stale_conflict_pairs

    ts = "2026-06-01T00:00:00Z"
    a = _durable("Chris uses vim as his primary editor on the homelab", ts)
    b = _durable("Chris uses neovim as his primary editor on the homelab", ts)
    assert stale_conflict_pairs([a, b]) == []


def test_naive_and_aware_timestamps_stay_comparable():
    from recall_governance.temporal_resolution import stale_conflict_pairs

    aware = _durable(
        "Chris uses neovim as his primary editor on the homelab",
        "2026-06-01T00:00:00Z",
    )
    naive = _durable(
        "Chris uses vim as his primary editor on the homelab",
        "2026-01-01T00:00:00",
    )
    assert stale_conflict_pairs([aware, naive]) == [(1, 0)]


def test_low_authority_rows_never_pair():
    """Derived/summary rows already carry the -45 authority penalty; the
    temporal stage only arbitrates between LIVE durable rows."""
    from recall_governance.temporal_resolution import stale_conflict_pairs

    stale_summary = _durable(
        "Chris uses vim as his primary editor on the homelab",
        "2026-01-01T00:00:00Z",
    )
    stale_summary["metadata"]["source_path"] = "/sessions/2026-01-01-session_summary.md"
    newer = _durable(
        "Chris uses neovim as his primary editor on the homelab",
        "2026-06-01T00:00:00Z",
    )
    assert stale_conflict_pairs([stale_summary, newer]) == []


def test_superseded_rows_never_pair():
    from recall_governance.temporal_resolution import stale_conflict_pairs

    superseded = _durable(
        "Chris uses vim as his primary editor on the homelab",
        "2026-01-01T00:00:00Z",
    )
    superseded["metadata"]["review_state"] = "superseded"
    newer = _durable(
        "Chris uses neovim as his primary editor on the homelab",
        "2026-06-01T00:00:00Z",
    )
    assert stale_conflict_pairs([superseded, newer]) == []


def test_route_guarantee_rows_never_pair():
    from recall_governance.temporal_resolution import stale_conflict_pairs

    guarantee = _durable(
        "Chris uses vim as his primary editor on the homelab",
        "2026-01-01T00:00:00Z",
        collection="canonical",
    )
    guarantee["source_type"] = "route_guarantee"
    guarantee["governance"] = ["route_guarantee"]
    newer = _durable(
        "Chris uses neovim as his primary editor on the homelab",
        "2026-06-01T00:00:00Z",
    )
    assert stale_conflict_pairs([guarantee, newer]) == []


# ── _apply_temporal_resolution_inplace: ranking before/after ─────────────


def test_stale_durable_row_demotes_below_newer_replacement():
    """Before/after control: the stale contradicted row outranks its newer
    replacement on raw score; after the stage it must rank strictly below."""
    from routes.recall import _apply_temporal_resolution_inplace

    older = _durable(
        "Chris uses vim as his primary editor on the homelab",
        "2026-01-01T00:00:00Z",
        rid="old",
        score=90.0,
    )
    newer = _durable(
        "Chris uses neovim as his primary editor on the homelab",
        "2026-06-01T00:00:00Z",
        rid="new",
        score=80.0,
    )
    fused = [older, newer]
    before = sorted(fused, key=lambda r: r["score"], reverse=True)
    assert [r["id"] for r in before] == ["old", "new"]

    _apply_temporal_resolution_inplace(fused)

    after = sorted(fused, key=lambda r: r["score"], reverse=True)
    assert [r["id"] for r in after] == ["new", "old"]
    assert older["score"] == 90.0 - 160.0
    assert "temporal_resolution_stale_penalty" in older["governance"]
    assert older["_debug"]["temporally_contradicted_by"] == "new"
    # The newer (current) row is never touched.
    assert newer["score"] == 80.0
    assert "governance" not in newer


def test_stale_row_conflicting_with_multiple_newer_rows_demotes_once():
    from routes.recall import _apply_temporal_resolution_inplace

    older = _durable(
        "Brain server listens on port 8791 behind the personal webhook",
        "2026-01-01T00:00:00Z",
        rid="old",
        score=90.0,
    )
    newer_a = _durable(
        "Brain server listens on port 9100 behind the personal webhook",
        "2026-06-01T00:00:00Z",
        rid="new-a",
        score=80.0,
    )
    newer_b = _durable(
        "The brain server is listening on port 9100 behind the personal webhook",
        "2026-06-02T00:00:00Z",
        rid="new-b",
        score=70.0,
    )
    fused = [older, newer_a, newer_b]
    _apply_temporal_resolution_inplace(fused)
    _apply_temporal_resolution_inplace(fused)
    assert older["score"] == 90.0 - 160.0
    assert older["governance"].count("temporal_resolution_stale_penalty") == 1


def test_non_conflicting_fused_list_is_untouched():
    from routes.recall import _apply_temporal_resolution_inplace

    rows = [
        _durable(
            "Chris uses neovim as his primary editor on the homelab",
            "2026-06-01T00:00:00Z",
            rid="a",
            score=80.0,
        ),
        _durable(
            "Brain runs as a Docker container on the homelab",
            "2026-01-01T00:00:00Z",
            rid="b",
            score=70.0,
        ),
    ]
    fused = [dict(r) for r in rows]
    _apply_temporal_resolution_inplace(fused)
    assert [r["score"] for r in fused] == [80.0, 70.0]
    assert all("governance" not in r for r in fused)


# ── /recall/v2 query-intent guard ─────────────────────────────────────────


def test_temporal_history_prompt_detects_history_provenance_source_origin_and_asof():
    from recall_governance.temporal_resolution import is_temporal_history_prompt

    prompts = [
        "What is Chris's editor history?",
        "Show the provenance for Chris's editor memory",
        "What source said Chris used Vim?",
        "What was the origin of that editor preference?",
        "What editor did Chris use as of 2026-01-01?",
        "Chris editor 과거 출처 알려줘",
    ]
    assert all(is_temporal_history_prompt(prompt) for prompt in prompts)
    assert not is_temporal_history_prompt("What editor does Chris use now?")


def test_recall_v2_temporal_resolution_skips_plain_text_history_and_provenance_queries():
    from routes.recall import _apply_temporal_resolution_inplace, _should_apply_temporal_resolution

    history_rows = [
        _durable("Chris uses Vim as default editor.", "2026-01-01T00:00:00Z", rid="old", score=90.0),
        _durable("Chris uses Neovim as default editor.", "2026-06-01T00:00:00Z", rid="new", score=80.0),
    ]
    assert not _should_apply_temporal_resolution(
        "What is Chris's editor history and provenance?",
        include_history=False,
        include_obsolete=False,
        as_of=None,
    )
    if _should_apply_temporal_resolution(
        "What is Chris's editor history and provenance?",
        include_history=False,
        include_obsolete=False,
        as_of=None,
    ):
        _apply_temporal_resolution_inplace(history_rows)

    assert history_rows[0]["score"] == 90.0
    assert "governance" not in history_rows[0]


def test_recall_v2_temporal_resolution_still_demotes_current_queries():
    from routes.recall import _apply_temporal_resolution_inplace, _should_apply_temporal_resolution

    current_rows = [
        _durable("Chris uses Vim as default editor.", "2026-01-01T00:00:00Z", rid="old", score=90.0),
        _durable("Chris uses Neovim as default editor.", "2026-06-01T00:00:00Z", rid="new", score=80.0),
    ]
    assert _should_apply_temporal_resolution(
        "What editor does Chris use now?",
        include_history=False,
        include_obsolete=False,
        as_of=None,
    )
    _apply_temporal_resolution_inplace(current_rows)

    assert current_rows[0]["score"] == -70.0
    assert "temporal_resolution_stale_penalty" in current_rows[0]["governance"]
