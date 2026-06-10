"""Identity-attribute classes: email / location-timezone / KO name labels.

Contract 3: current authoritative identity facts must outrank stale/archived
provenance, and KO/EN paraphrases must bind the same attribute class — with NO
exact-query hacks. These tests pin the generic class boundaries: the email
class binds only the contact-attribute ask (never the email-message corpus
vocabulary), the location class never binds determiner-led thing-subjects, and
label-form identity docs bind to the owner only when the owner is named.
Fixture values are invented placeholders.
"""

from __future__ import annotations

import pytest
from recall_governance import query_analyzer as qa

# ── Query-side binding: positives (EN + KO paraphrases) ─────────────────

BINDING_POSITIVES = [
    ("What is the best email address to reach Chris?", "chris", "email"),
    ("what is Chris email address", "chris", "email"),
    ("Chris email address wheogus", "chris", "email"),
    ("what's my email address?", "chris", "email"),
    ("크리스의 이메일 주소가 뭐야?", "chris", "email"),
    ("Where is Chris based and what timezone does he work in?", "chris", "location"),
    ("Chris location timezone", "chris", "location"),
    ("what timezone does Chris work in?", "chris", "location"),
    ("크리스 시간대가 어떻게 돼?", "chris", "location"),
    ("크리스의 한국 이름이 뭐야?", "chris", "legal_name"),
    ("Chris Korean name", "chris", "legal_name"),
    ("내 한국 이름 기억해?", "chris", "legal_name"),
]


@pytest.mark.parametrize(("query", "subject", "attribute"), BINDING_POSITIVES)
def test_attribute_query_binds(query, subject, attribute):
    binding = qa.personal_attribute_query_binding(query)
    assert binding is not None, query
    assert (binding.subject, binding.attribute) == (subject, attribute), (query, binding)


# ── Query-side binding: negative controls (must NOT bind) ────────────────
# Email-message corpus vocabulary and infra/thing subjects share surface words
# with the attribute classes; binding them would empty legitimate recalls.

BINDING_NEGATIVES = [
    "What emails should Chris keep for six months?",
    "Which email categories count as noise for Chris?",
    "What Intuit email confirms Chris made a payment?",
    "What script sends Chris the evening email digest?",
    "Chris는 어떤 이메일만 오래 보관하길 원해?",
    "저녁 이메일 요약은 몇 시에 Chris한테 가야 해?",
    "What design replaced Owntracks and Mosquitto for location ingestion?",
    "What browser-based check does Chris want for UI proof?",
    "What should opportunity evaluation be based on for Chris?",
    "where is the database based?",
    "파일 이름이 뭐야?",
    "qdrant named vectors hybrid retrieval",
]


@pytest.mark.parametrize("query", BINDING_NEGATIVES)
def test_non_attribute_query_does_not_bind(query):
    assert qa.personal_attribute_query_binding(query) is None, query


# ── Fact-side binding: sentence and label forms ──────────────────────────


def test_sentence_form_email_fact_binds_owner():
    text = "Chris Cho's email address is owner@example.com."
    assert qa.personal_attribute_result_matches_query("what is Chris email address", text) is True


def test_compound_script_name_fact_binds_owner():
    text = "Chris's Korean/Hangul name is 홍길동; full/legal-style name is Gildong Hong."
    assert qa.personal_attribute_result_matches_query("크리스의 한국 이름이 뭐야?", text) is True


def test_label_form_identity_doc_binds_owner():
    doc = (
        "## Identity\n"
        "- **Name:** Chris Cho\n"
        "- **Korean/Hangul name:** 홍길동\n"
        "- **Location:** Springfield, Anystate (XST/XDT)\n"
        "- **Email:** owner@example.com\n"
    )
    assert qa.personal_attribute_result_matches_query("what is Chris email address", doc) is True
    assert (
        qa.personal_attribute_result_matches_query(
            "Where is Chris based and what timezone does he work in?", doc
        )
        is True
    )
    assert qa.personal_attribute_result_matches_query("크리스의 한국 이름이 뭐야?", doc) is True


def test_label_form_doc_without_owner_name_does_not_bind():
    """Negative control: a third-party profile in label form (owner not named)
    must NOT bind to the owner — subject mismatch keeps it droppable."""
    doc = "## Profile\n- **Email:** someone-else@example.com\n- **Location:** Elsewhere (ZST)\n"
    assert qa.personal_attribute_result_matches_query("what is Chris email address", doc) is False


def test_third_party_attribute_query_rejects_owner_fact():
    """Negative control: 'Ellie's email' must not be answered by the owner's."""
    owner_fact = "Chris Cho's email address is owner@example.com."
    assert qa.personal_attribute_result_matches_query("what is Ellie's email address?", owner_fact) is False


def test_topic_row_without_attribute_fact_rejected():
    """A row that merely TALKS about email (blog/how-to) states no attribute
    fact — it must be droppable for the attribute query (forbidden-control
    mirror of the gmail blog-post leak)."""
    blog = "Ghost blog post: How to Set Up a Custom Domain Email with Gmail (For Free)."
    assert (
        qa.personal_attribute_result_matches_query("What is the best email address to reach Chris?", blog)
        is False
    )


# ── Route-level guard: archived/noise rows drop, identity fact survives ──


def _row(rid, content, collection="canonical", **extra):
    row = {
        "id": rid,
        "content": content,
        "collection": collection,
        "metadata": {"category": "fact", "review_state": "accepted"},
        "score": 40.0,
    }
    row.update(extra)
    return row


@pytest.mark.parametrize(
    "query",
    [
        "Where is Chris based and what timezone does he work in?",
        "What is the best email address to reach Chris?",
        "크리스의 한국 이름이 뭐야?",
    ],
)
def test_quality_filter_keeps_identity_fact_drops_archived_and_topic_noise(query):
    from routes.recall import _apply_retrieval_quality_filter

    identity_doc = (
        "## Identity\n- **Name:** Chris Cho\n- **Korean/Hangul name:** 홍길동\n"
        "- **Location:** Springfield, Anystate (XST/XDT)\n- **Email:** owner@example.com\n"
    )
    rows = [
        _row("identity", identity_doc, title="Identity"),
        _row(
            "archived_decision",
            "Chris uses outcome-based acceptance for UI work when the calendar view changes.",
            path="canonical/archived/decisions/old-decision.md",
            score=300.0,
        ),
        _row(
            "blog",
            "Ghost blog post: How to Set Up a Custom Domain Email with Gmail (For Free).",
            collection="knowledge",
            score=290.0,
        ),
    ]
    kept = [r["id"] for r in _apply_retrieval_quality_filter(query, [dict(r) for r in rows])]
    assert kept == ["identity"], (query, kept)
