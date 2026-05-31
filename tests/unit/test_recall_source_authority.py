"""Unit tests for the shared recall-governance source-authority contract.

Provenance/format only — topic-agnostic. Pins the tiering that every surface
ranks by: direct current truth outranks derived summary/reflection/session/
procedure/episodic/source-quote rows.
"""

from recall_governance import source_authority as sa
from recall_governance.source_authority import AuthorityTier


def _text(r: dict) -> str:
    return sa.result_text(r)


def test_low_authority_classifies_summary_reflect_session_procedure():
    low = [
        {"title": "Summary", "collection": "rag", "content": "weekly recap of work"},
        {"title": "### Summary", "collection": "canonical", "content": "rollup"},
        {
            "title": "Reasoning",
            "collection": "canonical",
            "metadata": {"subtype": "brain-analysis"},
            "content": "analysis",
        },
        {
            "title": "note",
            "metadata": {"source_path": "/distilled/brain-reflect/nightly.md"},
            "content": "reflection",
        },
        {"title": "note", "metadata": {"document_type": "session-summary"}, "content": "session"},
        {
            "title": "note",
            "metadata": {"source_path": "/procedures/voyager_skill.md"},
            "content": "procedure",
        },
        {
            "id": "raw_coding_event_1",
            "collection": "raw_events",
            "title": "coding_event: Edit on x.py",
            "content": "edit",
        },
        {
            "title": "### Context\n- Operation attempted: deploy",
            "collection": "experience",
            "content": "context log",
        },
    ]
    for r in low:
        assert sa.is_low_authority_result(r, _text(r)), f"expected low-authority: {r}"

    high = [
        {
            "title": "Codex workflow preference",
            "collection": "semantic_memory",
            "metadata": {"category": "preference"},
            "content": "Chris prefers X",
        },
        {
            "title": "Deploy decision",
            "collection": "canonical",
            "metadata": {"category": "decision", "review_state": "accepted"},
            "content": "decided Y",
        },
        {
            "title": "## Why this matters\n- Recall missed a fact",
            "collection": "experience",
            "content": "durable lesson, keep",
        },
    ]
    for r in high:
        assert not sa.is_low_authority_result(r, _text(r)), f"should not be low-authority: {r}"


def test_durable_truth_classifies_durable_provenance():
    assert sa.is_durable_truth_result({"collection": "semantic_memory", "metadata": {}})
    assert sa.is_durable_truth_result({"collection": "canonical", "metadata": {"review_state": "accepted"}})
    assert sa.is_durable_truth_result({"collection": "rag", "metadata": {"category": "preference"}})
    assert not sa.is_durable_truth_result(
        {"collection": "semantic_memory", "metadata": {"review_state": "superseded"}}
    )
    assert not sa.is_durable_truth_result(
        {"collection": "canonical", "metadata": {"category": "decision", "expired": True}}
    )
    # a durable COLLECTION does not make a derived FORMAT durable
    assert not sa.is_durable_truth_result(
        {"collection": "semantic_memory", "title": "Summary", "metadata": {}}
    )


def test_episodic_and_source_classifiers():
    assert sa.is_episodic_event_log_result(
        {
            "id": "raw_coding_event_x",
            "collection": "raw_events",
            "title": "coding_event: Edit",
            "content": "e",
        },
        "coding_event: Edit",
    )
    assert sa.is_source_or_test_file_result(
        {"id": "s", "title": "test_thing.py", "path": "/srv/tests/unit/test_thing.py"}
    )
    assert not sa.is_source_or_test_file_result(
        {"id": "p", "title": "Codex preference", "collection": "semantic_memory"}
    )


def test_classify_result_tiers():
    assert (
        sa.classify_result(
            {
                "collection": "semantic_memory",
                "metadata": {"category": "preference"},
                "content": "Chris prefers X",
            }
        )
        == AuthorityTier.DIRECT_CURRENT_TRUTH
    )
    assert (
        sa.classify_result({"collection": "semantic_memory", "metadata": {"review_state": "superseded"}})
        == AuthorityTier.OBSOLETE_OR_SUPERSEDED
    )
    assert (
        sa.classify_result({"title": "test_x.py", "path": "/repo/tests/test_x.py", "collection": "rag"})
        == AuthorityTier.SOURCE_OR_TEST_QUOTE
    )
    assert (
        sa.classify_result(
            {"id": "raw_coding_event_1", "collection": "raw_events", "title": "coding_event: e"}
        )
        == AuthorityTier.EPISODIC_LOG
    )
    assert (
        sa.classify_result({"title": "Summary", "collection": "rag", "content": "weekly recap"})
        == AuthorityTier.DERIVED_SUMMARY
    )
    assert (
        sa.classify_result({"title": "Some note", "collection": "obsidian", "content": "general doc"})
        == AuthorityTier.CURATED_CANONICAL
    )


def test_block_level_authority_duck_typed():
    assert sa.is_generic_summary_title("Summary")
    assert sa.is_generic_summary_title("Summary (part 2)")
    assert not sa.is_generic_summary_title("Codex preference")

    # dict-shaped block (InjectionBlock.to_dict parity)
    assert sa.is_low_authority_block({"title": "x", "source": "semantic", "path": "/sessions/2026-05-10.md"})
    assert not sa.is_low_authority_block(
        {"title": "Codex preference", "source": "semantic:semantic_memory", "path": "/sem/codex.md"}
    )

    class _Block:
        title = "Summary"
        source = "semantic"
        path = None

    assert sa.is_low_authority_block(_Block())


# ── Historical-runtime provenance (OpenClaw historical, Hermes current) ────


def test_is_openclaw_historical_result_flags_openclaw_provenance_en_ko():
    # distilled/session restatement carrying OpenClaw provenance
    assert sa.is_openclaw_historical_result(
        {
            "title": "Decision: media generation approach",
            "collection": "distilled",
            "content": "# Summary OpenClaw jenna session: prefer existing subscriptions.",
        }
    )
    # Korean provenance marker
    assert sa.is_openclaw_historical_result({"title": "오픈클로 세션 기록", "content": "에이전트 작업 요약"})
    # explicit text override path
    assert sa.is_openclaw_historical_result({}, "captured from an openclaw workspace run")
    # a current, OpenClaw-free durable row is NOT historical
    assert not sa.is_openclaw_historical_result(
        {
            "title": "cost_billing route guarantee",
            "collection": "canonical",
            "content": "Chris prefers existing subscriptions over new paid API billing.",
        }
    )


def test_summary_shaped_content_is_low_authority_even_in_canonical():
    """A row whose CONTENT leads with a '# Summary' header is a derived
    distillation-format artifact — low authority even in a durable collection.
    Provenance/format signal, not a topic marker."""
    # leading '# Summary' / '## Summary' content -> low authority
    assert sa.is_low_authority_result(
        {
            "title": "Chris prefers contract-first execution",
            "collection": "canonical",
            "metadata": {"category": "preference"},
            "content": "# Summary Chris prefers contract-first execution with explicit constraints.",
        },
        "x",
    )
    assert sa.is_low_authority_result(
        {
            "title": "ops note",
            "collection": "canonical",
            "content": "## Summary This page captures operating rules and workflow preferences.",
        },
        "x",
    )
    # such a row is therefore NOT durable truth despite canonical+preference
    assert not sa.is_durable_truth_result(
        {
            "title": "p",
            "collection": "canonical",
            "metadata": {"category": "preference"},
            "content": "# Summary distilled preference body.",
        }
    )
    # a lesson/root-cause header ('## Why this matters') and plain prose stay high
    assert not sa.is_low_authority_result(
        {
            "title": "lesson",
            "collection": "experience",
            "content": "## Why this matters\n- recall missed a fact; keep this durable lesson.",
        },
        "x",
    )
    assert not sa.is_low_authority_result(
        {
            "title": "pref",
            "collection": "canonical",
            "metadata": {"category": "preference"},
            "content": "Chris prefers existing subscriptions over new paid API billing.",
        },
        "x",
    )
