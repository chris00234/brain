"""Unit tests for the shared recall-governance query analyzer.

Class-level coverage (EN/KO paraphrases + positive/negative controls), not
exact acceptance-probe strings. Pins the contract that every recall surface
consumes: live-state vs durable, out-of-domain world-knowledge, summary intent,
and the composite QueryIntent.
"""

from recall_governance import query_analyzer as qa
from recall_governance.normalization import tokenize

# ── M1: script-boundary tokenization ──────────────────────────────────────


def test_tokenize_splits_latin_hangul_script_boundary():
    assert {"openclaw", "hermes"}.issubset(tokenize("OpenClaw랑 Hermes 차이"))
    assert {"gpt", "claude"}.issubset(tokenize("GPT는 Claude보다"))
    assert "codex" in tokenize("Codex를 어떻게 써")
    assert "런타임" in tokenize("현재 런타임")
    assert {"docker", "deploy"}.issubset(tokenize("docker deploy"))


# ── M2: live-state / present-status classification ─────────────────────────


def test_is_live_state_query_present_state_en_ko():
    positives = [
        "Is Liz done with the Brain recall fix right now?",
        "Is the deploy done right now?",
        "What is happening on the diagnostics tasks at this moment?",
        "what's running currently",
        "current status of the kanban task",
        "브레인 리콜 수정 지금 끝났어?",
        "지금 그 작업 끝났어?",
        "현재 진단 태스크들 어디까지 됐어?",
        "현재 뭐가 돌아가는 중이야?",  # colloquial KO live-state (former regression)
    ]
    for q in positives:
        assert qa.is_live_state_query(q), f"expected live-state: {q!r}"


def test_is_live_state_query_keeps_durable_and_topic_searchable():
    negatives = [
        "What does Chris prefer for coding agents right now?",
        "What is Chris's current tooling preference?",
        "OpenClaw vs Hermes current runtime historical distinction",
        "OpenClaw하고 Hermes 런타임 차이 지금 기준으로 알려줘",
        "지금 브레인 리콜 선호가 뭐야?",
        "Right now, is Brain's recall and prefetch quality healthy or noisy?",  # brain-quality meta, not pure live-state
        "What was the status of the kanban task last week?",
        "지난주 완료한 작업 기록 보여줘",
        "Recommend an LLM tool without extra API billing",
    ]
    for q in negatives:
        assert not qa.is_live_state_query(q), f"should stay searchable: {q!r}"


# ── M4: out-of-domain world-knowledge detection ────────────────────────────


def test_out_of_domain_world_knowledge_en_ko():
    out_of_domain = [
        "tomato pasta recipe please",
        "tell me about the French revolution",
        "explain just the cooking procedure briefly",
        "토마토 파스타 레시피 알려줘.",
        "요리 절차만 간단히 설명해줘.",
        "된장찌개 끓이는 방법 간단히 알려줘",
    ]
    for q in out_of_domain:
        assert qa.is_out_of_domain_world_knowledge_query(q), f"expected out-of-domain: {q!r}"

    in_domain = [
        "what does Chris prefer for coding agents",
        "내 브레인 리콜 선호가 뭐야?",
        "what is my preferred database",
        "what calendar tool do I use",
        "추가 유료 API 없이 자동화 도구 추천해줘.",
    ]
    for q in in_domain:
        assert not qa.is_out_of_domain_world_knowledge_query(q), f"should not be out-of-domain: {q!r}"


# ── Summary intent / exclusion ─────────────────────────────────────────────


def test_summary_intent_and_exclusion():
    assert qa.is_positive_summary_intent_query("give me the weekly summary recap")
    assert qa.is_positive_summary_intent_query("최근 브레인 작업 요약해줘")
    assert qa.is_summary_excluded_query("what is Chris's codex preference, not the summary")
    assert qa.is_summary_excluded_query("요약 말고 직접 알려줘")
    # exclusion wins over positive intent even when both cues appear
    assert not qa.is_positive_summary_intent_query("summary 말고 알려줘")


# ── Composite QueryIntent ──────────────────────────────────────────────────


def test_analyze_query_populates_intent_flags_and_route_tags():
    intent = qa.analyze_query("How should I run Codex through Hermes when quality matters?")
    assert intent.durable_advice is False  # no recommend/prefer marker present
    assert intent.live_state is False
    assert "codex_workflow" in intent.route_tags
    assert "codex" in intent.tokens

    runtime = qa.analyze_query("What is the current OpenClaw vs Hermes runtime distinction?")
    assert "runtime_distinction" in runtime.route_tags
    assert runtime.live_state is False  # distinction is durable, not live-state

    recipe = qa.analyze_query("간단한 토마토 파스타 레시피 알려줘")
    assert recipe.out_of_domain_world_knowledge is True
    assert recipe.route_tags == frozenset()


def test_analyze_query_is_fail_open_on_empty():
    intent = qa.analyze_query("")
    assert intent.live_state is False
    assert intent.tokens == frozenset()
    assert intent.route_tags == frozenset()


def test_personal_factoid_query_terms_are_open_ended_attribute_terms():
    """Self/Chris/user fact probes expose requested attribute/topic terms without
    a roster of exact attributes, so unknown personal facts can abstain when only
    generic profile rows match."""
    assert {"mountain", "patagonia", "hiking", "route"}.issubset(
        qa.personal_factoid_query_terms(
            "Chris favorite mountain in Patagonia favorite hiking route Cerro Torre Fitz Roy"
        )
    )
    assert {"shoe", "size", "sneaker", "foot"}.issubset(
        qa.personal_factoid_query_terms("Chris shoe size sneaker size foot size")
    )
    assert {"childhood", "elementary", "school", "first", "grade", "teacher"}.issubset(
        qa.personal_factoid_query_terms("Chris childhood elementary school first grade teacher")
    )
    assert qa.personal_factoid_query_terms("how do I make tomato pasta sauce recipe steps") == frozenset()


def test_personal_factoid_overlap_gate_requires_requested_attribute_overlap():
    q = "Chris shoe size sneaker size foot size"
    assert (
        qa.personal_factoid_result_has_strong_attribute_overlap(
            q, "Chris profile preferences and Claude Code setup notes."
        )
        is False
    )
    assert (
        qa.personal_factoid_result_has_strong_attribute_overlap(
            q, "Chris shoe size is US 10; sneaker fit is narrow."
        )
        is True
    )


def test_personal_factoid_overlap_ignores_compound_morpheme_collisions():
    """A negative personal-fact probe must NOT read as a strong attribute match
    when its terms surface only as hyphen-compound fragments of an unrelated row —
    'first' inside 'content-first', 'grade' inside 'production-grade'. Overlap is
    counted on whole-word occurrences, so weak morpho-modifier collisions do not
    pass the gate, while a genuine answer row (standalone words) still does."""
    q = "Chris childhood elementary school first grade teacher"
    assert (
        qa.personal_factoid_result_has_strong_attribute_overlap(
            q,
            "erl_extraction design notes: content-first layout, production-grade UI polish.",
        )
        is False
    )
    assert (
        qa.personal_factoid_result_has_strong_attribute_overlap(
            q,
            "Chris's first grade teacher at his elementary school was Mrs. Lee.",
        )
        is True
    )


# ── OpenClaw / agent-runtime query targeting ───────────────────────────────


def test_query_targets_openclaw_or_agents():
    # prompts about the historical runtime / named agents / workspaces
    assert qa.query_targets_openclaw_or_agents("OpenClaw vs Hermes runtime distinction")
    assert qa.query_targets_openclaw_or_agents("오픈클로 에이전트 설정 알려줘")
    assert qa.query_targets_openclaw_or_agents("what is sage working on in its workspace")
    # generic cost/tooling/preference prompts do NOT target OpenClaw/agents
    assert not qa.query_targets_openclaw_or_agents(
        "Recommend an LLM tooling approach without extra API billing"
    )
    assert not qa.query_targets_openclaw_or_agents("브레인 리콜 품질 평가 기준")
    assert not qa.query_targets_openclaw_or_agents("")


def test_summary_exclusion_handles_indefinite_article():
    # "not a summary" must be EXCLUSION (and therefore not positive intent) —
    # the determiner may be a/an/the or absent.
    for q in (
        "tell me the codex preference, not a summary",
        "the actual preference, not an summary",
        "without a summary please",
    ):
        assert qa.is_summary_excluded_query(q), q
        assert not qa.is_positive_summary_intent_query(q), q
    # positive intent unaffected
    assert qa.is_positive_summary_intent_query("summarize the recent work")
    assert not qa.is_summary_excluded_query("summarize the recent work")


# ── Holdout regressions (t_c7453635): generic class-level robustness ───────


def test_is_live_state_query_korean_running_aspect_continuative_form():
    """Present-progressive RELATIVE / continuative form (중인 / 중이야) across the
    running-aspect verb class (실행/가동/구동/작동), not only the bare 중. Holdout
    paraphrase class ("현재 실행 중인 작업 …"), no probe string."""
    positives = [
        "현재 실행 중인 작업 뭐 있어?",
        "지금 실행 중인 프로세스 알려줘",
        "지금 가동 중인 서비스 있어?",
        "실행 중이야?",
    ]
    for q in positives:
        assert qa.is_live_state_query(q), f"expected live-state: {q!r}"
    negatives = [
        # 중복 (duplicate) / 중단 (halted) are NOT the running aspect
        "실행 중복 제거 방법 알려줘",
        "배포 중단된 이유가 뭐야?",
        # historical override still wins over the running aspect
        "실행 중이었던 과거 작업 기록",
    ]
    for q in negatives:
        assert not qa.is_live_state_query(q), f"should stay searchable: {q!r}"


def test_out_of_domain_excludes_cost_and_tooling_domain_paraphrases():
    """Cost/tooling/model/api/hosting vocabulary is Chris's engineering domain;
    these paraphrases must NOT be world-knowledge, else the gate strips their
    durable cost/tool-policy truth (holdout cost failure class). EN + KO."""
    in_domain = [
        "Choose an AI tooling path that avoids new paid APIs and avoids self-hosting generation models.",
        "Pick an LLM approach with no extra API billing and no local model hosting.",
        "새 유료 API 없이 구독으로 쓸 LLM 모델 골라줘",
    ]
    for q in in_domain:
        assert not qa.is_out_of_domain_world_knowledge_query(q), f"should be in-domain: {q!r}"
    # World-knowledge recipe paraphrase (no domain anchor) stays out-of-domain.
    assert qa.is_out_of_domain_world_knowledge_query("How do I cook spaghetti arrabbiata tonight?")


def test_content_tokens_strips_closed_class_function_words():
    """Distinctive content tokens drop closed-class function words, so an
    out-of-domain prompt never 'overlaps' a row on a bare auxiliary like 'do'."""
    from recall_governance.normalization import content_tokens

    toks = content_tokens("How do I cook spaghetti arrabbiata tonight?")
    assert {"cook", "spaghetti", "arrabbiata"}.issubset(toks)
    assert not ({"do", "how", "the", "is"} & toks)
    # distinctive content survives for an in-domain prompt
    assert {"codex", "hermes"}.issubset(content_tokens("How do I run Codex with Hermes?"))


# ── Negation-scope detection (route-keyword arbitration) ───────────────────


def test_occurrence_is_negated_en_ko():
    """occurrence_is_negated flags a keyword inside an explicit negation scope:
    an EN cue in the few preceding tokens (incl. n't contractions) or a KO marker
    just after the keyword. An unrelated 'not' that negates a different word does
    NOT count — generic linguistic class, no per-route phrases."""
    from recall_governance.normalization import occurrence_is_negated

    def negated(keyword: str, text: str) -> bool:
        lowered = text.lower()
        idx = lowered.index(keyword.lower())
        return occurrence_is_negated(lowered, idx, idx + len(keyword))

    # negated occurrences (EN verbal cue incl. contraction; KO trailing marker)
    assert negated("codex", "this is not about codex, just review the layout")
    assert negated("codex", "I want a design review, this isn't codex work")
    assert negated("codex", "i did not pick codex; show me the layout")
    assert negated("코덱스", "코덱스 얘기 아니라 프론트엔드 디자인 봐줘")
    assert negated("코덱스", "코덱스 말고 다른 도구로 가자")

    # NOT negated: 'not' negates a different word, or no negation present
    assert not negated("codex", "not sure yet — should I use codex here?")
    assert not negated("codex", "use codex through hermes for quality steering")
    assert not negated("코덱스", "코덱스를 헤르메스로 품질 작업에 써야 할까?")

    # bare "no"/"without" is CONSTRAINT phrasing ("no paid API"), NOT topic
    # exclusion — it must not be treated as negation, or the cost/budget routes
    # those phrases DEFINE would stop firing.
    assert not negated("api", "pick an approach with no paid api")
    assert not negated("model", "recommend a tool without local model hosting")


def test_has_unnegated_match_requires_positive_occurrence():
    """has_unnegated_match is True only when at least one match is OUTSIDE a
    negation scope — so a single negated keyword is not positive route evidence,
    but a later positive mention still counts."""
    import re

    from recall_governance.normalization import has_unnegated_match

    pat = re.compile(r"(?<![a-z0-9])codex(?![a-z0-9])")
    assert has_unnegated_match(pat, "should I use codex for this?") is True
    assert has_unnegated_match(pat, "this is not about codex at all") is False
    # negated once, then mentioned positively → still positive evidence
    assert has_unnegated_match(pat, "not about claude; but is codex better here?") is True


# ── Sage REQUEST_CHANGES f1: generic present-state status for ANY system ───


def test_is_live_state_query_generic_current_state_about_arbitrary_systems():
    """Finding 1: a current-state status question about an ARBITRARY system/topic
    (deploy pipeline, data migration, indexing job) is live-state — not just the
    kanban/task/Brain wordings the strict regex already enumerates. The class is
    present-time deixis x a state-probe predicate ('health', 'where ... now', 'how
    is it looking', '상태 어때'), generalized over the named subject. Holdout
    paraphrases — none equal Sage's exact 'what is running right now' / Brain
    strings, and no Brain/recall/prefetch term is treated as special. EN + KO."""
    positives = [
        "What's the current health of the deploy pipeline?",  # health predicate
        "Where is the data migration right now?",  # where-is-it-now
        "Where is the deploy rollout right now?",  # where-is-it-now
        "How is the indexing job looking at the moment?",  # how-is-it-looking
        "현재 배포 파이프라인 상태 어때?",  # KO current state of a system
        "지금 배포 상태 괜찮아?",  # KO present-time health probe
    ]
    for q in positives:
        assert qa.is_live_state_query(q), f"expected live-state: {q!r}"
    # Negative controls: a durable/historical lookup about the SAME systems must
    # still search memory (the present-time deixis is what makes it live-state).
    negatives = [
        "What was the deploy pipeline's health last week?",  # historical
        "Show me the past data migration records.",  # historical
        "배포 파이프라인 과거 상태 기록 보여줘",  # KO historical
    ]
    for q in negatives:
        assert not qa.is_live_state_query(q), f"should stay searchable: {q!r}"


# ── Sage REQUEST_CHANGES f2: durable advice w/ 'running'/'tasks' ≠ live-state ─


def test_is_live_state_query_durable_running_tasks_advice_not_suppressed():
    """Finding 2: durable advice/workflow/preference/procedure prompts must NOT be
    read as live-state just because they contain a 'running tasks/jobs/processes'
    (or KO 실행/가동 중) noun phrase. A recommend/prefer/workflow/procedure frame
    with no present-time 'now/지금/현재' deixis is durable guidance, not a status
    read. Class-level: advice intent overrides the running-aspect keyword. EN+KO."""
    durable = [
        "Recommend a workflow for monitoring running tasks.",
        "What's your preferred way to restart running jobs?",
        "Recommend a procedure for cleaning up running processes.",
        "실행 중인 작업 정리 워크플로 추천해줘",
        "가동 중인 서비스 관리 베스트 프랙티스 추천해줘",
    ]
    for q in durable:
        assert not qa.is_live_state_query(q), f"durable advice misread as live-state: {q!r}"
    # Preserve TRUE live-state suppression (the other half of the finding): what is
    # running now, tasks done right now, current kanban task status, current-progress
    # / diagnostics prompts, and the KO equivalents must still classify as live-state.
    preserved_live_state = [
        "what is running now",
        "are the tasks done right now?",
        "current kanban task status",
        "what's the current progress on the diagnostics run?",
        "지금 뭐 돌아가고 있어?",
        "현재 실행 중인 작업 뭐 있어?",
    ]
    for q in preserved_live_state:
        assert qa.is_live_state_query(q), f"true live-state must stay suppressed: {q!r}"


# ── Sage f1 (missed class): current <subject> status/state/progress ────────


def test_is_live_state_query_current_subject_status_state_progress_arbitrary():
    """Finding 1 missed class: 'current <arbitrary subject> status/state/progress'
    is live-state even when the subject sits BETWEEN 'current' and the status noun.
    The strict `current status` adjacency regex misses it, and the token-cluster
    path needs a task/job context token the arbitrary subject (deploy pipeline,
    migration, search index) lacks. Class is present-time deixis + a status-noun,
    generic over the named subject — no Brain/recall special-casing. The Brain
    wording is included as just ONE assertion of the original evidence."""
    positives = [
        "what is the current deploy pipeline status?",
        "what is the current deploy pipeline state?",
        "what is the current deploy pipeline progress?",
        "what is the current search index status?",
        "what is the current migration state right now?",
        "what is the current brain recall/prefetch status?",  # original evidence (one assertion)
    ]
    for q in positives:
        assert qa.is_live_state_query(q), f"expected live-state: {q!r}"
    # Durable advice and historical counter-controls must still search memory.
    negatives = [
        "what is the preferred workflow for running tasks?",  # durable advice, not a status read
        "what was the deploy pipeline status last week?",  # historical
        "what is the current tooling preference?",  # durable preference, no status noun
    ]
    for q in negatives:
        assert not qa.is_live_state_query(q), f"should stay searchable: {q!r}"


# ── Sage REQUEST_CHANGES (t_daa410e4): durable procedure/workflow/how-to/policy ─
# WITHOUT a recommend/prefer marker is NOT live-state ──────────────────────────


def test_is_live_state_query_durable_procedure_workflow_howto_policy_without_advice_marker():
    """Durable workflow / procedure / how-to / monitoring-policy prompts that
    merely CONTAIN a 'running tasks/jobs/processes' (or KO 실행/가동 중인 작업) noun
    phrase are NOT live-state — even when they carry NO recommend/prefer/추천/선호
    marker. The discriminator is a present/current-state deixis gate: a durable
    guidance frame (procedure/workflow/how-to/policy/monitoring) with no
    present-time 'now/right now/currently/지금/현재' deixis asks for stored guidance,
    not a status read. The existing durable-advice test only covers the
    recommend/prefer-marked subclass; this pins the unmarked class the
    running-aspect regex currently misclassifies. EN + KO; class-level, no probe
    strings.
    """
    durable = [
        # task-named probes (no recommend/prefer marker)
        "what procedure should we use for running jobs?",
        "what workflow should I use for running tasks?",
        "how should I monitor running tasks?",
        "실행 중인 작업 절차 알려줘",
        "what policy should we follow for monitoring running jobs?",
        # holdout paraphrases BEYOND the task-named strings
        "what monitoring policy applies to running jobs?",  # EN monitoring-policy
        "how do we clean up running jobs as standard practice?",  # EN how-to / standard-practice
        "가동 중인 서비스 점검 절차 알려줘",  # KO procedural paraphrase (가동/점검)
        "실행 중인 작업 관리 방법 알려줘",  # KO how-to (방법) paraphrase
    ]
    for q in durable:
        assert not qa.is_live_state_query(q), f"durable guidance misread as live-state: {q!r}"
        # and none of these carry an explicit recommend/prefer advice marker — the
        # fix must generalize past that marker, not just rely on it.
        assert not qa.is_durable_advice_query(q), f"unexpected recommend/prefer marker in: {q!r}"

    # Present/current-state deixis GATE: the SAME running-aspect phrasing flips to
    # live-state once a present-time deixis is present — so true status reads stay
    # suppressed. EN + KO contrast pairs plus the task's true-live controls.
    preserved_live_state = [
        "current task status right now",
        "what is running now",
        "are tasks done right now?",
        "what's the current progress on the diagnostics run?",
        "지금 뭐 돌아가고 있어?",
        # deixis-gate contrasts on the same running-aspect noun phrase
        "are running tasks done right now?",  # vs "how should I monitor running tasks?"
        "현재 실행 중인 작업 뭐 있어?",  # vs "실행 중인 작업 절차 알려줘"
    ]
    for q in preserved_live_state:
        assert qa.is_live_state_query(q), f"true live-state must stay suppressed: {q!r}"


# ── Sage REQUEST_CHANGES (t_7890c31b): bare `how is/are` status questions are ───
# live-state, NOT durable guidance ─────────────────────────────────────────────


def test_how_is_are_status_questions_are_live_state_not_durable_guidance():
    """A generic `how is/are <subject> …?` question is a present status/progress
    READ — live-state — even though it opens with the bare framing word `how`.

    The discriminating class is the copular `how is/are` probe over an ARBITRARY
    operational subject (running tasks, the deploy pipeline, a data migration, an
    indexing job), with NO procedural modal (do/should/can) and NO recommend/prefer
    marker. Stale recall cannot answer these. Today `is_durable_guidance_query`
    treats the standalone `how` as a how-to frame, which (a) flips these to durable
    guidance and (b) gates off the running-aspect live-state path — so a bare
    status question is misread as recallable guidance and prefetch injects stale
    memory. EN paraphrases (incl. the task-evidence wordings) + arbitrary-system
    holdouts; no probe-string blacklist."""
    status_questions = [
        "how are running tasks?",  # task-evidence paraphrase
        "how are the tasks going?",  # task-evidence paraphrase
        "how is the deploy pipeline doing?",  # arbitrary system
        "how is the data migration going?",  # arbitrary system
        "how are the indexing jobs coming along?",  # arbitrary system
        "how is the search index doing?",  # arbitrary system
    ]
    for q in status_questions:
        assert qa.is_live_state_query(q), f"expected live-state status read: {q!r}"
        assert not qa.is_durable_guidance_query(
            q
        ), f"bare `how is/are` status question misread as durable guidance: {q!r}"
        # …and none of these carry an explicit recommend/prefer advice marker — the
        # fix must narrow the copular `how is/are` case, not lean on advice markers.
        assert not qa.is_durable_advice_query(q), f"unexpected advice marker in: {q!r}"
    # Composite intent agrees: the shared QueryIntent.live_state flag is set, so every
    # surface that consumes analyze_query() suppresses these too.
    assert qa.analyze_query("how are running tasks?").live_state is True


def test_how_procedural_advice_stays_durable_guidance_not_live_state():
    """Positive controls: real procedural / advice prompts — `how do/should/can we
    manage/monitor/use …` — must STAY durable guidance and NOT become live-state.

    The discriminator is the procedural modal/verb (do/should/can + manage/monitor/
    use) versus the copular `how is/are`; the narrowing must touch only the copular
    status case and leave the how-to class intact. Includes Korean controls so the
    EN-targeted narrowing does not disturb Korean classification (KO procedural
    guidance stays durable + searchable; a KO present-state question stays live)."""
    procedural = [
        "how do we manage running tasks?",
        "how should we monitor running jobs?",
        "how can we use the deploy pipeline?",
        "how should I monitor running tasks?",  # mirrors the existing durable class
        "how should I run Codex through Hermes?",  # actor + action verb, not a status read
        "how to monitor running jobs",  # bare `how to` how-to frame
    ]
    for q in procedural:
        assert qa.is_durable_guidance_query(q), f"procedural how-to misread as non-guidance: {q!r}"
        assert not qa.is_live_state_query(q), f"durable how-to misread as live-state: {q!r}"
    # Multilingual controls (must remain unchanged by the EN-only narrowing).
    assert qa.is_durable_guidance_query("실행 중인 작업 관리 방법 알려줘")
    assert not qa.is_live_state_query("실행 중인 작업 관리 방법 알려줘")
    assert qa.is_live_state_query("현재 실행 중인 작업 뭐 있어?")


def test_how_do_does_subject_look_status_questions_are_live_not_durable_guidance():
    """Refinement (t_7890c31b): the NON-copular paraphrase of the same false-positive
    class — `how do|does <subject> look/seem/appear?` — is a present status READ,
    NOT durable guidance. The earlier narrowing must not just special-case the
    copular `how is/are`; a how-to is distinguished by addressing an ACTOR
    (`how to …` / `how <modal> we/I/you …`), whereas these address a SUBJECT via a
    determiner ("the deploy pipeline", "the tasks") + an appearance verb. Generic
    over the subject; no probe-string blacklist."""
    status_questions = [
        "how do the tasks look?",  # task-evidence paraphrase (non-copular)
        "how does the deploy pipeline look?",  # arbitrary system
        "how do the running jobs look?",  # arbitrary system
        "how does the data migration seem?",  # appearance verb 'seem'
    ]
    for q in status_questions:
        assert qa.is_live_state_query(q), f"expected live-state status read: {q!r}"
        assert not qa.is_durable_guidance_query(
            q
        ), f"`how do/does <subject> look?` status question misread as durable guidance: {q!r}"
        assert not qa.is_durable_advice_query(q), f"unexpected advice marker in: {q!r}"
    assert qa.analyze_query("how does the deploy pipeline look?").live_state is True


# ── Sage holdout (t_8fb29191): PASSIVE procedure/usage/manage forms are durable ─


def test_is_live_state_query_passive_procedure_usage_forms_are_durable_guidance():
    """Sage holdout (t_8fb29191): a PASSIVE procedure/usage/manage question —
    `how is/are <subject> <procedure-verb>?` and `how are <subjects> supposed to
    be <procedure-verb>?` — asks for durable STORED guidance about how a subject
    IS managed/used/operated, NOT a live status read. Stale recall can answer it.

    Today the past-participle procedure verbs (managed/used/organized/handled/
    operated/configured/run/executed) are absent from the durable-guidance vocab,
    which only carries the base/gerund forms (manage/managing, use/using). So
    'how are running tasks managed?' is misread as live-state (the running-aspect
    keyword fires because durable_guidance is False) and 'how is the task runner
    used?' as non-guidance. The discriminating class is the passive participle
    predicate vs a present-progressive status predicate (going/doing/tracking/
    holding up) — generic verb-paraphrase class, no probe strings.
    """
    durable = [
        "how are running tasks managed?",  # Sage holdout 1 (managed)
        "how is the task runner used?",  # Sage holdout 2 (used)
        "how are running jobs supposed to be managed?",  # Sage holdout 3 (supposed-to-be)
        "how are configs organized?",  # organized
        "how are errors handled?",  # handled
        "how is the pipeline operated?",  # operated
        "how is the runner configured?",  # configured
        "how are jobs run?",  # run (passive participle)
        "how are tasks executed?",  # executed
        "how are jobs monitored?",  # monitored (existing class member)
    ]
    for q in durable:
        assert qa.is_durable_guidance_query(q), f"passive procedure/usage form not durable guidance: {q!r}"
        assert not qa.is_live_state_query(q), f"passive procedure/usage form misread as live-state: {q!r}"
        # none carry a recommend/prefer marker — the fix must recognize the passive
        # procedure frame itself, not lean on advice markers.
        assert not qa.is_durable_advice_query(q), f"unexpected advice marker in: {q!r}"
    # Composite intent agrees so every analyze_query() consumer recalls these.
    assert qa.analyze_query("how are running tasks managed?").live_state is False

    # Negative controls: TRUE live-status forms — copular `how is/are <subject>
    # <present-progressive>` and the `how do/does <subject> look` paraphrase — must
    # STAY live-state and NOT become durable guidance, with the SAME subjects.
    live_status = [
        "how are running tasks?",
        "how are the tasks going?",
        "how is deploy pipeline doing?",
        "how does deploy pipeline look?",
        "how is data migration tracking?",
        "how are indexing jobs holding up?",
    ]
    for q in live_status:
        assert qa.is_live_state_query(q), f"true live-status must stay live-state: {q!r}"
        assert not qa.is_durable_guidance_query(q), f"true live-status misread as durable guidance: {q!r}"

    # Preserve existing ACTIVE how-to controls (actor + procedural modal) and the
    # KO procedural / live controls — the passive-form fix must not disturb them.
    for q in (
        "how do we manage running tasks?",
        "how should I monitor running tasks?",
        "how to monitor running jobs",
    ):
        assert qa.is_durable_guidance_query(q), f"active how-to control broke: {q!r}"
        assert not qa.is_live_state_query(q), f"active how-to misread as live-state: {q!r}"
    assert qa.is_durable_guidance_query("실행 중인 작업 관리 방법 알려줘")
    assert not qa.is_live_state_query("실행 중인 작업 관리 방법 알려줘")
    assert qa.is_live_state_query("현재 실행 중인 작업 뭐 있어?")


# ── Kanban t_d4acddbc: out_of_domain x memory_domain interaction guard ──────
# A passive durable-guidance prompt about an OPERATIONAL subject must be
# IN-domain, or /recall/v2's world-knowledge quality filter drops every candidate
# (the live count=0 regression) even though it is correctly durable, not live.


def test_passive_durable_guidance_about_operational_subjects_is_in_domain():
    """Acceptance #3: pins the out_of_domain x memory_domain interaction the
    existing passive-procedure test left uncovered.

    Before the operational-anchor fix the task/job/runner positives were durable
    (live_state=False) yet out_of_domain=True / memory_domain=False — so the server
    quality filter zeroed all candidates and the live provider prefetch came back
    empty. They are about Chris's engineering domain (the Hermes task runner, the
    brain scheduler, kanban tasks, cron jobs), so they must be in-domain.

    The discriminator is the operational NOUN anchor, NOT a blanket durable-guidance
    exemption: a recipe how-to is ALSO durable_guidance=True yet has no operational
    anchor, so it MUST stay out-of-domain. That is what proves the anchor — not the
    guidance flag — keeps the world-knowledge negative control intact. EN + KO;
    class-level paraphrases, no probe strings."""
    in_domain_durable = [
        "how are running tasks managed?",  # task (managed)
        "how is the task runner used?",  # runner
        "how are running jobs supposed to be managed?",  # job (supposed-to-be)
        "how is the runner configured?",  # runner
        "how are jobs monitored?",  # job
        "how is the pipeline operated?",  # pipeline
        "how are scheduler jobs handled?",  # scheduler + job
        "what's the standard practice for cleaning up worker processes?",  # worker/process
        "실행 중인 작업 관리 방법 알려줘",  # KO: 작업 (task) + 방법 (how-to)
    ]
    for q in in_domain_durable:
        intent = qa.analyze_query(q)
        assert (
            intent.out_of_domain_world_knowledge is False
        ), f"operational durable guidance wrongly out-of-domain: {q!r}"
        assert intent.memory_domain is True, f"operational durable guidance not in memory-domain: {q!r}"
        assert intent.live_state is False, f"durable guidance misread as live-state: {q!r}"
        assert qa.is_durable_guidance_query(q), f"not classified as durable guidance: {q!r}"

    # Recipe / world-knowledge how-to: durable_guidance may be True, but with NO
    # operational anchor it MUST stay out-of-domain. This is the regression the
    # anchor (not a guidance exemption) guards — note these are NOT live-state.
    world_knowledge = [
        "how do I make tomato pasta sauce recipe steps",
        "How do I cook spaghetti arrabbiata tonight?",
        "How do I make a good music playlist for a party?",
        "토마토 파스타 레시피 알려줘.",
    ]
    for q in world_knowledge:
        intent = qa.analyze_query(q)
        assert intent.out_of_domain_world_knowledge is True, f"world-knowledge wrongly in-domain: {q!r}"
        assert intent.memory_domain is False, f"world-knowledge wrongly in memory-domain: {q!r}"

    # The OpenClaw-vs-Hermes runtime-distinction query names Chris's runtimes but
    # has no generic anchor token — it must STAY out-of-domain by the classifier.
    # (Adding 'runtime' to the operational anchors would silently break the
    # routes.recall control that relies on this; the anchor set excludes it.)
    assert (
        qa.is_out_of_domain_world_knowledge_query("what is the OpenClaw versus Hermes runtime distinction")
        is True
    )

    # Live-state status reads about the SAME operational subjects stay suppressed
    # even though they are now in memory-domain — live_state has absolute precedence.
    for q in ("how are running tasks?", "what is running now?", "현재 실행 중인 작업 뭐 있어?"):
        assert qa.analyze_query(q).live_state is True, f"live-state suppression lost: {q!r}"


def test_is_operational_guidance_query_class_and_anchors():
    """The operational durable-guidance class the provider uses to EXPAND prefetch
    recall: is_operational_guidance_query == durable-guidance x an operational-domain
    anchor (task/runner/job/scheduler/queue/pipeline/…). The discriminator is the
    ANCHOR, not the guidance flag — a recipe how-to is also durable_guidance but has
    no operational anchor, and a live status read is not durable guidance. Both must
    be excluded so the provider never expands recall for them. The 'how is the runner
    configured?' paraphrase (the failing live positive) is included. EN + KO."""
    operational = [
        "how is the runner configured?",  # the failing live positive
        "how are running tasks managed?",
        "how is the task runner used?",
        "how are running jobs supposed to be managed?",
        "how are scheduler jobs handled?",
        "what's the standard practice for cleaning up worker processes?",
        "실행 중인 작업 관리 방법 알려줘",  # KO 작업 (task) + 방법 (how-to)
    ]
    for q in operational:
        assert qa.is_operational_guidance_query(q), f"expected operational guidance: {q!r}"
        assert qa.operational_guidance_anchors(q), f"expected operational anchor tokens: {q!r}"
        assert qa.is_durable_guidance_query(q), f"expected durable guidance: {q!r}"
        assert not qa.is_live_state_query(q), f"operational guidance misread as live-state: {q!r}"

    # Negative control 1 — recipe how-to: durable-guidance is True, but with NO
    # operational anchor it MUST NOT be operational guidance (no provider expansion).
    for q in (
        "how do I make tomato pasta sauce recipe steps",
        "How do I cook spaghetti arrabbiata tonight?",
        "토마토 파스타 레시피 알려줘.",
    ):
        assert not qa.operational_guidance_anchors(q), f"recipe has no operational anchor: {q!r}"
        assert not qa.is_operational_guidance_query(
            q
        ), f"recipe how-to must not be operational guidance: {q!r}"

    # Negative control 2 — live current-status read about an operational subject:
    # not durable guidance, so not operational guidance (live-state has precedence).
    for q in ("what is running now?", "how are running tasks?", "현재 실행 중인 작업 뭐 있어?"):
        assert qa.is_live_state_query(q), f"expected live-state: {q!r}"
        assert not qa.is_operational_guidance_query(
            q
        ), f"live status read must not be operational guidance: {q!r}"

    # anchors() reflects the actual subject tokens, and is empty for an anchor-less
    # prompt — the provider's fallback subject anchors handle the Hangul-only case.
    assert qa.operational_guidance_anchors("how is the runner configured?") == frozenset({"runner"})
    assert qa.operational_guidance_anchors("how are scheduler jobs handled?") == frozenset(
        {"scheduler", "jobs"}
    )


# ── Kanban t_77a7f982: birthday / date-of-birth identity guard ─────────────
# A "when is <person>'s birthday?" question is a `when`-fact about a SPECIFIC
# identity. The personal corpus is Chris's, so a self-referential birthday query
# (my/I/me/Chris/내/제/나 + birthday) targets the owner ('chris'). Surfacing a
# DIFFERENT entity's birthday (Ellie, an agent, a pet) is identity contamination.
# These tests pin the shared query-intent/entity guard both surfaces consume.


def test_birthday_query_subject_resolves_self_referential_to_owner_en_ko():
    """Self-referential birthday/DOB questions (my/I/Chris/내/제/나 + birthday)
    target the corpus owner 'chris' — EN + KO paraphrases, no probe strings."""
    owner_queries = [
        "what is my birthday?",
        "when is Chris's birthday?",
        "when's my date of birth?",
        "when was I born?",
        "내 생일은 언제야?",
        "Chris 생일 언제야?",
        "제 생년월일이 언제죠?",
        "나 언제 태어났어?",
    ]
    for q in owner_queries:
        assert qa.birthday_query_subject(q) == "chris", f"expected owner target: {q!r}"


def test_birthday_query_subject_resolves_explicit_third_person_name():
    """An explicit third-person possessive birthday query targets THAT name, not
    the owner — so a legitimate 'When is Ellie's birthday?' is preserved."""
    assert qa.birthday_query_subject("When is Ellie's birthday?") == "ellie"
    assert qa.birthday_query_subject("when is Jenna's date of birth?") == "jenna"
    assert qa.birthday_query_subject("when was Ellie born?") == "ellie"


def test_birthday_query_subject_none_for_non_birthday_queries():
    """Non-birthday queries are not a birthday class → None (guard does not fire)."""
    for q in [
        "what does Chris prefer for coding agents",
        "what is my deployment preference",
        "토마토 파스타 레시피 알려줘.",
        "when is the next kanban task due?",  # 'when' but no birthday/DOB intent
        "",
    ]:
        assert qa.birthday_query_subject(q) is None, f"unexpected birthday target: {q!r}"


def test_birthday_fact_subject_extracts_row_identity_en_ko():
    """birthday_fact_subject reads WHOSE birthday a result row states — a third
    party's name, or the owner for an owner-named / self-worded row."""
    assert qa.birthday_fact_subject("Ellie's birthday is December 27, 2021") == "ellie"
    assert qa.birthday_fact_subject("Chris's birthday is March 3.") == "chris"
    assert qa.birthday_fact_subject("My birthday is in June.") == "chris"
    assert qa.birthday_fact_subject("엘리 생일은 12월 27일이야") == "엘리"
    assert qa.birthday_fact_subject("조대현 생년월일은 비공개") == "chris"  # owner legal/Hangul name
    # Non-birthday rows are not birthday facts.
    for text in (
        "Chris prefers concise Korean status updates.",
        "Chris tends to send emails in the evening.",
        "Ellie helped review the deploy pipeline.",
    ):
        assert qa.birthday_fact_subject(text) is None, f"not a birthday fact: {text!r}"


def test_birthday_identity_mismatch_drops_cross_identity_when_facts():
    """The composed guard: a Chris-birthday query mismatches a DIFFERENT entity's
    birthday row (Ellie / agent / pet) and unrelated `when` facts never count, while
    the owner's own birthday and a legitimate explicit third-person query do NOT
    mismatch."""
    # Chris birthday query vs Ellie's birthday → contamination (mismatch).
    assert qa.birthday_identity_mismatch("what is my birthday?", "Ellie's birthday is December 27, 2021")
    assert qa.birthday_identity_mismatch("when is Chris's birthday?", "Ellie's birthday is December 27, 2021")
    assert qa.birthday_identity_mismatch("내 생일은 언제야?", "Ellie's birthday is December 27, 2021")
    # Owner's own birthday row → NOT a mismatch.
    assert not qa.birthday_identity_mismatch("what is my birthday?", "Chris's birthday is March 3.")
    # Non-birthday `when`-ish row is not a birthday fact → never a mismatch (the
    # provider's strict surface drops it separately for not stating the birthday).
    assert not qa.birthday_identity_mismatch(
        "when is Chris's birthday?", "Chris tends to send emails in the evening."
    )
    # Explicit third-person query keeps the matching row.
    assert not qa.birthday_identity_mismatch(
        "When is Ellie's birthday?", "Ellie's birthday is December 27, 2021"
    )
    # Non-birthday query → guard never fires.
    assert not qa.birthday_identity_mismatch(
        "what does Chris prefer?", "Ellie's birthday is December 27, 2021"
    )


# ── Kanban t_21eba883: generalized personal-attribute identity/attribute binding ─
# Birthday is one instance of a broader class — a question about a SPECIFIC
# personal ATTRIBUTE (address, phone, legal name, residence) of a SPECIFIC
# identity. The same self/possessor + attribute-noun linguistic classes resolve
# (subject, attribute); EN + KO; no hardcoded roster or probe strings.


def test_personal_attribute_query_binding_self_referential_to_owner_en_ko():
    """Self/possessive attribute queries bind to (owner='chris', attribute)."""
    cases = {
        "what is my address?": ("chris", "address"),
        "what's my home address?": ("chris", "address"),
        "where do I live?": ("chris", "address"),
        "what is my phone number?": ("chris", "phone"),
        "what's my cell phone?": ("chris", "phone"),
        "what is my legal name?": ("chris", "legal_name"),
        "what is Chris's full name?": ("chris", "legal_name"),
        "내 주소가 뭐야?": ("chris", "address"),
        "제 전화번호가 뭐죠?": ("chris", "phone"),
        "내 본명이 뭐야?": ("chris", "legal_name"),
    }
    for q, (subject, attribute) in cases.items():
        b = qa.personal_attribute_query_binding(q)
        assert b is not None, f"expected binding for {q!r}"
        assert (b.subject, b.attribute) == (subject, attribute), f"{q!r} -> {b}"


def test_personal_attribute_query_binding_explicit_third_person_name():
    """Explicit third-person possessive attribute queries bind to THAT name."""
    assert qa.personal_attribute_query_binding("what is Ellie's address?") == qa.PersonalAttributeBinding(
        "ellie", "address"
    )
    assert qa.personal_attribute_query_binding(
        "what is Ellie's phone number?"
    ) == qa.PersonalAttributeBinding("ellie", "phone")
    assert qa.personal_attribute_query_binding("where does Ellie live?") == qa.PersonalAttributeBinding(
        "ellie", "address"
    )
    # No third-party name romanization (only the owner's own aliases fold), so a
    # Korean third-person name stays Hangul — consistent with birthday_fact_subject.
    assert qa.personal_attribute_query_binding("엘리 주소가 뭐야?") == qa.PersonalAttributeBinding(
        "엘리", "address"
    )


def test_personal_attribute_query_binding_none_for_non_attribute_queries():
    for q in [
        "what is my deployment preference",
        "what calendar tool do I use",
        "Recommend an LLM tooling approach without extra API billing",
        "how is the runner configured?",
        "토마토 파스타 레시피 알려줘.",
        "",
    ]:
        assert qa.personal_attribute_query_binding(q) is None, f"unexpected binding: {q!r}"


def test_personal_attribute_fact_binding_extracts_row_identity():
    assert qa.personal_attribute_fact_binding(
        "Ellie's address is 12 Oak St, Irvine."
    ) == qa.PersonalAttributeBinding("ellie", "address")
    assert qa.personal_attribute_fact_binding("Chris's phone is 555-0100.") == qa.PersonalAttributeBinding(
        "chris", "phone"
    )
    assert qa.personal_attribute_fact_binding("My address is 1 Main St.") == qa.PersonalAttributeBinding(
        "chris", "address"
    )
    for text in (
        "Chris prefers concise Korean status updates.",
        "Chris runs the brain server on port 8791.",
        "Ellie reviewed the deploy pipeline.",
    ):
        assert qa.personal_attribute_fact_binding(text) is None, f"not an attribute fact: {text!r}"


def test_personal_attribute_result_matches_query_subject_and_attribute():
    q = "what is my address?"
    assert qa.personal_attribute_result_matches_query(q, "Chris's address is 1 Main St.") is True
    assert qa.personal_attribute_result_matches_query(q, "Ellie's address is 12 Oak St.") is False
    assert qa.personal_attribute_result_matches_query(q, "Chris's phone is 555-0100.") is False
    assert qa.personal_attribute_result_matches_query(q, "Ellie's birthday is Dec 27.") is False
    assert qa.personal_attribute_result_matches_query(q, "Chris runs the brain server.") is False
    # Non-scoped query → None (guard off), never accidentally True/False.
    assert qa.personal_attribute_result_matches_query("what tool do I use", "anything") is None
    # Explicit third-person query keeps the matching row.
    assert (
        qa.personal_attribute_result_matches_query(
            "what is Ellie's address?", "Ellie's address is 12 Oak St."
        )
        is True
    )


def test_birthday_is_a_personal_attribute_binding_instance():
    """Birthday is one instance of the generalized class; address/birthday do not
    cross-satisfy, and the existing birthday wrappers stay consistent."""
    assert qa.personal_attribute_query_binding("what is my birthday?") == qa.PersonalAttributeBinding(
        "chris", "birthday"
    )
    assert qa.personal_attribute_query_binding("when is Ellie's birthday?") == qa.PersonalAttributeBinding(
        "ellie", "birthday"
    )
    assert (
        qa.personal_attribute_result_matches_query("what is my address?", "Chris's birthday is March 3.")
        is False
    )
    assert (
        qa.personal_attribute_result_matches_query("what is my birthday?", "Chris's address is 1 Main St.")
        is False
    )


# ── t_7c27ae38: residence (lives in/at, 살아/거주) is an address-fact form ─────
# A declarative residence statement ("Chris lives in Irvine", "크리스는 Irvine에 살아")
# states the SAME attribute as a where-do-I-live / address query, so it must bind
# to the address class. Generic verb-locative class (EN lives/resides + in/at/on;
# KO locative + 살/거주), never a place roster or probe string.


def test_personal_attribute_fact_binding_residence_declarative_forms_en_ko():
    cases = {
        "Chris lives in Irvine": ("chris", "address"),
        "Chris lives at 1 Main St.": ("chris", "address"),
        "Chris resides in Irvine.": ("chris", "address"),
        "I live in Irvine": ("chris", "address"),
        "크리스는 Irvine에 살아": ("chris", "address"),
        "크리스는 어바인에 거주한다": ("chris", "address"),
    }
    for text, (subject, attribute) in cases.items():
        b = qa.personal_attribute_fact_binding(text)
        assert b == qa.PersonalAttributeBinding(subject, attribute), f"{text!r} -> {b}"


def test_residence_declarative_matches_self_and_third_person_address_query():
    # Self/owner address queries (EN + KO) are answered by a residence fact.
    for q in ("where do I live?", "what is my address?", "내 주소가 뭐야?"):
        assert qa.personal_attribute_result_matches_query(q, "Chris lives in Irvine") is True
        assert qa.personal_attribute_result_matches_query(q, "크리스는 Irvine에 살아") is True
    # A different identity's residence is NOT the owner's address.
    assert qa.personal_attribute_result_matches_query("where do I live?", "Ellie lives in Boston") is False
    # Explicit third-person residence query keeps the matching residence fact.
    assert (
        qa.personal_attribute_result_matches_query("where does Ellie live?", "Ellie lives in Boston") is True
    )


def test_personal_attribute_fact_binding_declarative_copular_bare_name_en():
    """A declarative COPULAR bare-name RESULT row ("Chris address is …", "Chris
    birthday is …", "Ellie phone is …") — no possessive 's, value AFTER the copula —
    binds to the stated (subject, attribute). This is the result-only fact form the
    live zero-results repair (t_4e0974f3) needed; the query classifier is NOT
    broadened by it (see negative assertions below)."""
    cases = {
        "Chris address is 100 Example Ave.": ("chris", "address"),
        "Chris birthday is March 3.": ("chris", "birthday"),
        "Ellie phone is 555-0100.": ("ellie", "phone"),
        "Chris legal name is on file.": ("chris", "legal_name"),
    }
    for text, (subject, attribute) in cases.items():
        b = qa.personal_attribute_fact_binding(text)
        assert b == qa.PersonalAttributeBinding(subject, attribute), f"{text!r} -> {b}"
    # End-to-end relevance on the copular form: owner row matches a self query;
    # wrong subject / wrong attribute in the same shape do not.
    assert (
        qa.personal_attribute_result_matches_query("what is my address?", "Chris address is 100 Example Ave.")
        is True
    )
    assert (
        qa.personal_attribute_result_matches_query("what is my address?", "Ellie address is 12 Oak St.")
        is False
    )
    assert (
        qa.personal_attribute_result_matches_query("what is my address?", "Chris phone is 555-0100.") is False
    )
    # Query classification is NOT broadened: a copular declarative sentence is not a
    # personal-attribute QUERY (the value-after-copula form is result-only). The
    # query side keeps its trailing-focus bare-name binding.
    assert qa.personal_attribute_query_binding("Chris address is wrong, fix the handling") is None


def test_residence_declarative_does_not_overfire_on_non_residence_text():
    # "lives" requires a locative (in/at/on); KO 살 requires a residence ending —
    # generic verbs (runs/believes, 살펴봐/살림) must NOT read as an address fact.
    for text in (
        "Chris runs the brain server on port 8791.",
        "Chris believes in shipping fast.",
        "크리스는 회사에서 코드를 살펴봐",
    ):
        assert qa.personal_attribute_fact_binding(text) is None, f"not a residence fact: {text!r}"


# ── t_7c27ae38: self/possessive personal-attribute query is IN-domain ────────
# A self/possessive attribute query ("내 주소가 뭐야?", "where do I live?", "제
# 전화번호가 뭐죠?") is about a person in Chris's personal domain — never external
# world-knowledge — even when its only surviving tokens are a bare attribute noun
# because the single-syllable owner anchor (내/제) was dropped by the len>1
# tokenizer rule. The world-knowledge gate must not strip such a query's rows.


def test_self_personal_attribute_query_is_not_out_of_domain_world_knowledge():
    for q in (
        "내 주소가 뭐야?",
        "where do I live?",
        "제 전화번호가 뭐죠?",
        "내 본명이 뭐야?",
        "what is Ellie's address?",
    ):
        assert not qa.is_out_of_domain_world_knowledge_query(q), f"should be in-domain: {q!r}"
    # Genuine world-knowledge with no personal-attribute binding stays out-of-domain.
    assert qa.is_out_of_domain_world_knowledge_query("토마토 파스타 레시피 알려줘.")
    assert qa.is_out_of_domain_world_knowledge_query("How do I cook spaghetti arrabbiata tonight?")


# ── t_d52e9116: multi-word (full-name) possessor resolution ──────────────────
# The possessor of an attribute can be a multi-word name ("Chris Cho's address",
# "Jenna Yoonjung Cho's birthday", "Chris Cho lives in …", "Chris currently lives
# in …"). The single-token-adjacent extractor bound to the LAST token before the
# possessive/verb ("cho", "currently"), so an owner full-name fell to the family
# name instead of folding to the owner, and a third-party full-name fell to the
# shared family name instead of its own first name. The identity is the HEAD of
# the name phrase, folding owner-aliases. Generic class (any multi-word name),
# never a hardcoded roster; no private attribute VALUE is asserted here.


def test_full_name_owner_possessor_folds_to_owner_birthday_and_address():
    """An owner full-name possessor ("Chris Cho's …") folds to the owner, not the
    bare family name — the prior extractor bound to 'cho'."""
    assert qa.personal_attribute_fact_binding(
        "Chris Cho's birthday is in spring."
    ) == qa.PersonalAttributeBinding("chris", "birthday")
    assert qa.personal_attribute_fact_binding(
        "Chris Cho's address is on file."
    ) == qa.PersonalAttributeBinding("chris", "address")


def test_full_name_residence_and_adverb_fold_to_owner():
    """A full-name residence fact ("Chris Cho lives in …") and an adverb between
    the name and the verb ("Chris currently lives in …") still bind to the owner —
    the prior extractor bound to 'cho' / 'currently' and dropped the owner row."""
    assert qa.personal_attribute_fact_binding(
        "Chris Cho lives in southern California."
    ) == qa.PersonalAttributeBinding("chris", "address")
    assert qa.personal_attribute_fact_binding(
        "Chris currently lives in southern California."
    ) == qa.PersonalAttributeBinding("chris", "address")


def test_full_name_third_party_possessor_resolves_to_first_name_not_family_name():
    """A third-party full-name possessor resolves to its OWN first name, never the
    shared family name and never the owner — 'Jenna Yoonjung Cho' is jenna."""
    assert qa.personal_attribute_fact_binding(
        "Jenna Yoonjung Cho's birthday is in winter."
    ) == qa.PersonalAttributeBinding("jenna", "birthday")


def test_full_name_possessor_match_matrix_owner_vs_third_party():
    """The composed guard: owner queries match owner full-name rows; a third-party
    full-name row is excluded from an owner query (contamination) but preserved for
    the matching third-person query."""
    # Owner self/possessive queries match the owner full-name rows.
    assert (
        qa.personal_attribute_result_matches_query(
            "what is my birthday?", "Chris Cho's birthday is in spring."
        )
        is True
    )
    assert (
        qa.personal_attribute_result_matches_query(
            "내 주소가 뭐야?", "Chris Cho lives in southern California."
        )
        is True
    )
    # An owner query must NOT match a different person's full-name row.
    assert (
        qa.personal_attribute_result_matches_query(
            "what is my birthday?", "Jenna Yoonjung Cho's birthday is in winter."
        )
        is False
    )
    # The matching third-person query keeps that person's row.
    assert (
        qa.personal_attribute_result_matches_query(
            "when is Jenna's birthday?", "Jenna Yoonjung Cho's birthday is in winter."
        )
        is True
    )


def test_metaphorical_lives_in_is_not_owner_address():
    """A non-residence "lives in" ("Mutable state lives in core") is not the
    owner's address — it must stay off-target for a self-address query, so the
    wrong-subject exclusion is preserved."""
    assert (
        qa.personal_attribute_result_matches_query(
            "what is my address?", "Mutable state lives in the core module."
        )
        is False
    )


def test_email_address_wording_is_not_physical_address_for_self_query():
    """An email-address row ("Chris Cho's email address is on file") must NOT be
    treated as the physical address for a self-address query — the desired
    attribute is the physical address, not email."""
    assert (
        qa.personal_attribute_result_matches_query(
            "내 주소가 뭐야?", "what is Chris email address: Chris Cho's email address is on file"
        )
        is False
    )


# ── t_d52e9116 follow-up: English non-possessive "<Name> <attribute>" forms ──
# A bare explicit name immediately before an attribute noun ("Chris birthday?",
# "what is Chris address?", "what is Ellie birthday?") is the SAME (subject,
# attribute) query as the possessive form, just without the apostrophe-s. The
# possessive/"of" extractors missed it, so the analyzer left these unbound. The
# detection is deliberately bounded: the name must be a CAPITALIZED proper noun and
# the attribute noun must be the trailing focus of the question, so a generic noun
# phrase (a lowercase modifier, or the attribute used as a modifier of another head
# noun) never binds a person. Generic class, no probe strings; "cho" stays out of
# the owner-alias set.


def test_personal_attribute_query_binding_bare_explicit_name_en():
    """Bare '<Name> <attribute>' (no apostrophe-s) binds to that name + attribute,
    folding the owner's own name to 'chris' and keeping a third party as itself."""
    cases = {
        "what is Chris birthday?": ("chris", "birthday"),
        "Chris birthday?": ("chris", "birthday"),
        "what is Chris address?": ("chris", "address"),
        "what is Ellie birthday?": ("ellie", "birthday"),
    }
    for q, (subject, attribute) in cases.items():
        b = qa.personal_attribute_query_binding(q)
        assert b == qa.PersonalAttributeBinding(subject, attribute), f"{q!r} -> {b}"


def test_personal_attribute_query_binding_bare_name_negative_controls():
    """Generic noun phrases must NOT bind a person: the attribute noun used as a
    MODIFIER of another head noun ("birthday policy", "address handling", "phone
    config") or preceded by a lowercase common word ("current", "production") is
    not an explicit-name attribute query."""
    for q in (
        "what is birthday policy?",
        "what is current address handling?",
        "what is production phone config?",
    ):
        assert qa.personal_attribute_query_binding(q) is None, f"unexpected binding: {q!r}"


def test_bare_name_form_preserves_email_and_metaphor_address_exclusions():
    """The bare-name detection must not regress the wrong-attribute (email) or
    metaphorical ("state lives in") exclusions for a self physical-address query."""
    assert (
        qa.personal_attribute_result_matches_query(
            "what is Chris address?", "what is Chris email address: Chris Cho's email address is on file"
        )
        is False
    )
    assert (
        qa.personal_attribute_result_matches_query(
            "what is Chris address?", "Mutable state lives in the core module."
        )
        is False
    )


# ── Korean particle (josa) stripping and particle-aware factoid analysis ──────


def test_strip_korean_particle_basic_cases():
    """strip_korean_particle strips recognized trailing particles while keeping
    stems with >= 2 Hangul syllables; short stems and Latin tokens unchanged."""
    from recall_governance.normalization import strip_korean_particle

    # Subject particle
    assert strip_korean_particle("크리스가") == "크리스"
    # Locative particle (3-syllable)
    assert strip_korean_particle("파타고니아에서") == "파타고니아"
    # Topic particle
    assert strip_korean_particle("코스는") == "코스"
    # Short noun that ends in a particle syllable must NOT be butchered
    assert strip_korean_particle("회의") == "회의"
    # No-particle token unchanged
    assert strip_korean_particle("하이킹") == "하이킹"
    # Latin token unchanged
    assert strip_korean_particle("omscs") == "omscs"
    # Empty/short unchanged
    assert strip_korean_particle("") == ""
    assert strip_korean_particle("가") == "가"
    # Object particle
    assert strip_korean_particle("도구를") == "도구"
    # 이나 (disjunctive particle)
    assert strip_korean_particle("산이나") == "산이나"  # stem "산" is 1 syllable, too short


def test_personal_factoid_query_terms_particle_aware_korean():
    """After the particle-aware fix, a Korean personal-fact probe with particles
    glued to the subject and nouns returns non-empty terms and excludes the
    subject in any form."""
    terms = qa.personal_factoid_query_terms(
        "크리스가 파타고니아에서 제일 좋아하는 산이나 하이킹 코스는 뭐야?"
    )
    assert terms, "expected non-empty terms for particle-glued Korean factoid probe"
    # Subject stripped to 크리스 then removed by _PERSONAL_FACT_QUERY_STOPWORDS
    assert "크리스" not in terms
    assert "크리스가" not in terms
    # Attribute nouns (particle-stripped stems) should be present
    assert "파타고니아" in terms
    assert "하이킹" in terms
    assert "코스" in terms


def test_personal_factoid_query_terms_english_unchanged():
    """English space-separated queries must still work identically after the fix."""
    terms = qa.personal_factoid_query_terms("Chris favorite mountain in Patagonia favorite hiking route")
    assert "patagonia" in terms
    assert "mountain" in terms
    assert "hiking" in terms
    assert "chris" not in terms


def test_personal_factoid_result_overlap_transitive_with_different_particles():
    """A particle-stripped query term (코스) matches a result containing the same
    noun with a different particle (코스를/코스는) — transitivity."""
    q = "크리스가 파타고니아에서 제일 좋아하는 하이킹 코스는 뭐야?"
    # Result uses different particles on the same nouns
    result = "파타고니아의 하이킹 코스를 추천합니다: Torres del Paine W-trek."
    assert qa.personal_factoid_result_has_strong_attribute_overlap(q, result) is True


def test_personal_factoid_result_overlap_no_match_unrelated():
    """An unrelated result with no attribute overlap still returns False."""
    q = "크리스가 파타고니아에서 제일 좋아하는 하이킹 코스는 뭐야?"
    result = "Claude Code 설정과 브레인 리콜 품질 개선 작업 노트."
    assert qa.personal_factoid_result_has_strong_attribute_overlap(q, result) is False


def test_personal_factoid_overlap_disjoint_script_returns_none():
    """A pure-Hangul factoid query can NEVER whole-word-overlap an English-only
    result — every query term is non-ASCII and every result token is ASCII, so
    empty overlap carries zero relevance signal. The gate must answer None
    ("cannot judge") instead of False so the quality filter keeps the row;
    drop-on-False would erase every English answer for Korean phrasings of the
    same durable fact."""
    q = "Chris는 자동화 성공을 증거 없이 말하면 안 된다"
    result = "Chris explicitly rejected claiming success without proof and wants actual submit and result confirmation."
    assert qa.personal_factoid_result_has_strong_attribute_overlap(q, result) is None


def test_personal_factoid_overlap_disjoint_script_keeps_false_for_korean_results():
    """Same pure-Hangul query against a result that CONTAINS Hangul tokens stays
    judgeable: zero overlap there is genuine evidence of irrelevance, not a
    script artifact, so the strict False is preserved."""
    q = "Chris는 자동화 성공을 증거 없이 말하면 안 된다"
    result = "파타고니아의 하이킹 코스를 추천합니다: Torres del Paine W-trek."
    assert qa.personal_factoid_result_has_strong_attribute_overlap(q, result) is False


# ── t_ce0490ac: infra/tooling/integration prompts are IN-domain ─────────────
# recall_v2_content_hit_pct SLO breach: project/infra/tooling/integration prompts
# whose only domain nouns were concrete infra/client/pipeline terms (cloudflare,
# subdomain, browser, chrome, shell, ingest, adapter) carried NO abstract
# technical anchor, so the world-knowledge gate flagged them out-of-domain and the
# recall quality filter dropped EVERY candidate (result_count=0 with
# candidate_count>0). They name Chris's engineering world, so they must be
# in-domain. The discriminator is the concrete infra/tooling NOUN class, not the
# query shape — a recipe/trivia ask carries none of these and stays out-of-domain.


def test_infra_tooling_integration_prompts_are_in_domain():
    """Concrete infra/hosting, client-tooling, and data-pipeline/integration
    prompts are in-domain (Chris's engineering world), so the world-knowledge gate
    must not empty their rows. Includes the 'how to <infra noun>' shape that the
    bare how-to fallback previously misread as a generic world how-to."""
    in_domain = [
        # the three SLO-miss queries (the original evidence)
        "how to add new cloudflare subdomain step by step",
        "browser history Chrome usage patterns",
        "shell history ingest adapter",
        # class-level holdout paraphrases (not the exact eval strings)
        "nginx reverse proxy config for the homelab",
        "docker container restart loop on orbstack",
        "cloudflared tunnel dns record setup",
        "chrome extension that reads the page",
        "webhook connector adapter for the ingest pipeline",
        "tls certificate renewal for the subdomain",
    ]
    for q in in_domain:
        assert not qa.is_out_of_domain_world_knowledge_query(
            q
        ), f"infra/tooling/integration prompt wrongly out-of-domain: {q!r}"
        assert qa.analyze_query(q).out_of_domain_world_knowledge is False, q
        assert qa.analyze_query(q).memory_domain is True, q

    # Negative controls: genuine world-knowledge — a recipe/trivia ask and a
    # generic non-personal how-to (no infra/tooling anchor) MUST stay out-of-domain
    # so the gate still empties their rows. This proves the fix anchors on the
    # engineering-noun CLASS, not on the "how to …" shape.
    world_knowledge = [
        "tomato pasta sauce recipe",
        "recipe for chocolate cake",
        "tell me about the French revolution",
        "how to tie a bow tie step by step",  # generic non-personal how-to
        "how to make a good cup of pour-over coffee",  # generic non-personal how-to
    ]
    for q in world_knowledge:
        assert qa.is_out_of_domain_world_knowledge_query(q), f"world-knowledge wrongly in-domain: {q!r}"

    # The OpenClaw-vs-Hermes runtime-distinction control must STILL be out-of-domain
    # — 'runtime' is deliberately excluded from the infra/tooling anchors.
    assert qa.is_out_of_domain_world_knowledge_query("what is the OpenClaw versus Hermes runtime distinction")
