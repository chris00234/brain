"""Query-keyed bridge atom demotion (Contract 7).

A "bridge" atom frames its content as the answer to one literal query
phrasing ("For the exact query X: ...", "Knowledge-gap bridge for query Y:
..."). These are data-level retrieval hacks: they win recall by echoing the
keyed query text, masking real retrieval gaps and polluting answers for
every paraphrase. Governance demotes them decisively — never drops them —
so durable, source-anchored rows outrank them. The classifier is anchored
to the LEADING bridge framing of the row text, so documents or transcripts
that merely mention queries, and atoms quoting a user's exact words
mid-text, are never flagged.

No DB rows are read or mutated here; fixtures paraphrase the observed
framing families only.
"""

from __future__ import annotations

import pytest
from recall_governance import source_authority as sa

# ── Classifier: positives (one per observed framing family) ──────────────

BRIDGE_LEADS = [
    'Knowledge-gap bridge for query "website ui overhaul": the source is /tmp/x.md',
    "Knowledge-gap bridge for normalized query `python pipeline`: it means the pipeline package.",
    "Knowledge-gap bridge: For the query “what form is on file,” use the documents index.",
    'Knowledge gap answer for "eval regression gate nightly": the gate is the eval_run job.',
    "Knowledge gap answer for Korean query 'AGENTS 내용이 길어지면?': context limits apply.",
    'Knowledge gap source for normalized query "tesla app container setup": app lives at /tmp/app.',
    'Knowledge gap resolution for query "What service is not on localhost?": the answer is cloudflared.',
    "Retrieval bridge for normalized query `chris location timezone`: answer `Irvine, California`.",
    'Retrieval bridge for exact query: "Chris가 중요라고 하면" — save a core summary immediately.',
    "Korean bridge for exact query: Telegram 명령이 사라지면 Chris가 바로 눈치챈다.",
    'Exact query bridge: When asked in Korean, "Chris는 프론트엔드 기본 스택으로 뭘 써?", answer React + Vite.',
    "Alias source for exact query `search index`: resolve to `RAG Qdrant substrate`.",
    'For the query "last month incidents" in May 2026, interpret "last month" as April 2026.',
    'For the exact query "what is Chris email address": the address is x@example.com.',
    'When asked "when should the cron overview auto update", answer: always after changes.',
]


@pytest.mark.parametrize("lead", BRIDGE_LEADS)
def test_bridge_framed_content_is_flagged(lead):
    assert sa.is_query_keyed_bridge_result({"content": lead}) is True, lead


def test_bridge_framing_in_title_is_flagged():
    row = {"title": 'Knowledge-gap bridge for query "dinner appointment"', "content": "reminders://36"}
    assert sa.is_query_keyed_bridge_result(row) is True


# ── Classifier: negative controls ────────────────────────────────────────

LEGIT_LEADS = [
    # Plain durable fact, no query keying.
    "Sage 브라우징 분류는 Chris 기준으로 INTENTIONAL과 PASSIVE로 나뉜다.",
    # Conversation transcript that mentions queries/probes mid-text.
    "User: 내 생일은 언제야? 라는 질문을 했을 때 오염도는 어때?\nAssistant: 좋은 probe야.",
    # Session log embedding a RAG prompt that contains 'exact query'.
    "Hermes session ended.\nuser: Answer the question using ONLY the retrieved "
    "context. Directly answer the exact query first; do not answer adjacent questions.",
    # Documentation about query parameters.
    "The /recall endpoint accepts a query parameter and returns ranked results.",
    # 'When asked about X' without a quoted literal query is normal preference prose.
    "When asked about deployment, Chris prefers Docker containers on OrbStack.",
    # An atom quoting the user's exact words mid-text is a legitimate quote.
    'Chris said: "for the exact query stuff, stop hard-coding answers" during review.',
    # Korean fact that merely contains the word query.
    "Brain의 recall은 query 토큰을 정규화한 뒤 Qdrant에서 검색한다.",
]


@pytest.mark.parametrize("lead", LEGIT_LEADS)
def test_legitimate_rows_are_not_flagged(lead):
    assert sa.is_query_keyed_bridge_result({"content": lead}) is False, lead


def test_empty_row_is_not_flagged():
    assert sa.is_query_keyed_bridge_result({}) is False


# ── Governance ranking: bridge atom sinks below durable source ───────────


def _governed(query, rows):
    from routes.recall import _apply_recall_governance_inplace

    fused = [dict(r) for r in rows]
    _apply_recall_governance_inplace(query, fused)
    fused.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
    return fused


def test_bridge_atom_sinks_below_durable_doc_even_with_query_echo_lead():
    bridge = {
        "id": "b1",
        "title": "semantic memory",
        "content": 'Knowledge-gap bridge for query "memory dedup strategy": dedup uses simhash.',
        "collection": "semantic_memory",
        "score": 320.0,  # inflated by echoing the keyed query
        "metadata": {"type": "fact"},
    }
    durable = {
        "id": "d1",
        "title": "brain dedup design",
        "content": "Memory dedup strategy: raw inbox near-dup check, then simhash at the atoms layer.",
        "path": "canonical/decisions/brain-dedup-design.md",
        "collection": "canonical",
        "score": 280.0,
        "metadata": {"review_state": "accepted", "category": "decision"},
    }
    ranked = _governed("memory dedup strategy", [bridge, durable])
    assert [r["id"] for r in ranked] == ["d1", "b1"]
    b_row = next(r for r in ranked if r["id"] == "b1")
    assert "query_keyed_bridge_penalty" in (b_row.get("governance") or []), b_row
    # Demoted, never dropped.
    assert len(ranked) == 2


def test_bridge_atom_does_not_take_personal_attribute_boost():
    """An identity bridge atom must not ride the personal-attribute boost past
    the canonical identity row it plagiarizes."""
    bridge = {
        "id": "b1",
        "title": "semantic memory",
        "content": 'For the exact query "what is Chris email address": Chris Cho\'s email '
        "address is wheogus98@gmail.com. Authoritative source: canonical identity.",
        "collection": "semantic_memory",
        "score": 330.0,
        "metadata": {"type": "fact"},
    }
    canonical = {
        "id": "c1",
        "title": "Chris Cho — identity immutable core",
        "content": "Email: wheogus98@gmail.com. Location: Irvine, California.",
        "path": "canonical/chris/_identity.md",
        "collection": "canonical",
        "score": 300.0,
        "metadata": {"review_state": "accepted", "category": "fact"},
    }
    ranked = _governed("what is Chris email address", [bridge, canonical])
    assert [r["id"] for r in ranked] == ["c1", "b1"]
    b_row = next(r for r in ranked if r["id"] == "b1")
    assert "personal_attribute_match_priority" not in (b_row.get("governance") or []), b_row


def test_quoted_user_words_keep_score_no_penalty():
    quote = {
        "id": "q1",
        "title": "review feedback",
        "content": 'Chris said: "for the exact query stuff, stop hard-coding answers" during review.',
        "collection": "semantic_memory",
        "score": 100.0,
        "metadata": {"type": "correction"},
    }
    ranked = _governed("what did Chris say about hard-coding answers", [quote])
    assert "query_keyed_bridge_penalty" not in (ranked[0].get("governance") or [])


# ── Factoid injection: bridge raw_events rows are never injected ─────────


def _patch_fts(monkeypatch, rows):
    import raw_events_fts

    monkeypatch.setattr(raw_events_fts, "search", lambda q, limit=10, **kw: list(rows))


def test_inject_personal_factoid_answer_skips_bridge_atom(monkeypatch):
    from routes.recall import _inject_personal_factoid_answer

    _patch_fts(
        monkeypatch,
        [
            {
                "id": "b1",
                "content": 'Knowledge-gap bridge for query "Chris OMSCS Fall 2026": Chris is '
                "enrolling in Georgia Tech OMSCS Fall 2026.",
                "raw_source_type": "atoms_hot_path",
            },
        ],
    )
    fused = []
    _inject_personal_factoid_answer("What should I remember about Chris OMSCS Fall 2026?", fused)
    assert fused == []


def test_inject_personal_factoid_answer_still_injects_clean_atom(monkeypatch):
    """Negative control: the bridge guard must not block clean durable atoms."""
    from routes.recall import _inject_personal_factoid_answer

    _patch_fts(
        monkeypatch,
        [
            {
                "id": "e1",
                "content": "OMSCS: Chris is enrolling in Georgia Tech OMSCS Fall 2026.",
                "raw_source_type": "atoms_hot_path",
            },
        ],
    )
    fused = []
    _inject_personal_factoid_answer("What should I remember about Chris OMSCS Fall 2026?", fused)
    assert any("personal_factoid_answer_injected" in (r.get("governance") or []) for r in fused)
