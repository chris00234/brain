"""Brain memory provider — Hermes ↔ brain HTTP (127.0.0.1:8791) bridge.

Implements Hermes's MemoryProvider ABC so brain becomes the agent's
canonical memory backend. Profile-aware: each Hermes profile is tagged
as `agent:<profile_name>` so atoms can be filtered/attributed per persona.

Architecture (Phase 3 — 2026-05-23 OpenClaw → Hermes migration):

  Hermes profile (jenna|liz|ellie|sage|market)
    │
    │ initialize(agent_identity="<profile>") ──→ self._profile
    │
    ├─ prefetch(query)   ──→ GET /recall/v2 (filtered by agent/profile)
    │                         returns top-K results as system prompt context
    │
    └─ sync_turn(u, a)   ──→ POST /memory (tagged agent:<profile>)
                              brain cosine update gate + supersedes chain
                              decide canonical promotion

Why this matters: Hermes ships GitHub Issues #6320 (session/memory
contamination across profiles) and #4726 (profile-scoped namespaces
unimplemented). The default holographic provider can leak between
profiles. brain owns canonical truth; this provider routes writes
and reads through brain's existing cross-agent attribution mechanism
(qdrant `agent` filter + tags + cosine update gate).

Tool surface: empty (brain MCP server already exposes 21 tools via
mcp_servers.brain in config.yaml — no duplication).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider

log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

BRAIN_HOST = os.environ.get("BRAIN_HTTP_HOST", "127.0.0.1")
BRAIN_PORT = int(os.environ.get("BRAIN_HTTP_PORT", "8791"))
BRAIN_BASE = f"http://{BRAIN_HOST}:{BRAIN_PORT}"
_DEFAULT_SECRET_NAME = ".brain/credentials/.personal_webhook_secret"  # noqa: S105 - path, not secret
SECRET_FILE = Path(os.environ.get("BRAIN_WEBHOOK_SECRET_FILE", Path.home() / _DEFAULT_SECRET_NAME))

PREFETCH_K = int(os.environ.get("BRAIN_PREFETCH_K", "5"))
PREFETCH_TIMEOUT_S = float(os.environ.get("BRAIN_PREFETCH_TIMEOUT", "3"))
WRITE_TIMEOUT_S = float(os.environ.get("BRAIN_WRITE_TIMEOUT", "5"))


# ─── Shared recall-governance (optional) ──────────────────────────────────
# This provider runs in the Hermes runtime and talks to brain over HTTP, so the
# shared package's import name depends on what the host put on sys.path. It is
# importable EITHER as a top-level ``recall_governance`` (brain's own test env,
# or a deployment that adds brain_core to the path) OR as
# ``brain_core.recall_governance`` (the Hermes runtime, whose sys.path carries
# the repo root but not brain_core itself). We try both so prefetch consumes the
# SAME live-state classifier, source-authority contract, and prefetch policy as
# /recall/v2 and /recall/active — provider prefetch is the strictest surface
# (false-positive bias HIGH: empty beats wrong injected context). If neither
# import resolves we fall back to the local regex gating below; fail-open, never
# break prefetch.
def _load_recall_governance() -> dict[str, Any]:
    """Import the shared recall-governance package under whichever name is on the
    path and return the callables + provider-prefetch policy this module uses.

    ``brain_core/__init__.py`` is an import-light docstring, so resolving via
    ``brain_core.recall_governance`` pulls in the same leaf package (stdlib +
    PyYAML) as the top-level name — no Chroma/Ollama/Neo4j. Returns ``{}`` when
    neither base imports, so the caller degrades to local gating.
    """
    import importlib

    for base in ("recall_governance", "brain_core.recall_governance"):
        try:
            qa = importlib.import_module(f"{base}.query_analyzer")
            sa = importlib.import_module(f"{base}.source_authority")
            pp = importlib.import_module(f"{base}.prefetch_policy")
            rg = importlib.import_module(f"{base}.route_guarantees")
        except (AttributeError, ImportError):
            continue
        return {
            "is_live_state_query": qa.is_live_state_query,
            "is_durable_advice": qa.is_durable_advice_query,
            "is_durable_guidance": qa.is_durable_guidance_query,
            "is_operational_guidance": qa.is_operational_guidance_query,
            "operational_guidance_anchors": qa.operational_guidance_anchors,
            "is_out_of_domain": qa.is_out_of_domain_world_knowledge_query,
            "is_positive_summary_intent": qa.is_positive_summary_intent_query,
            "is_summary_excluded": qa.is_summary_excluded_query,
            "query_targets_openclaw": qa.query_targets_openclaw_or_agents,
            "birthday_query_subject": qa.birthday_query_subject,
            "birthday_fact_subject": qa.birthday_fact_subject,
            "personal_attribute_query_binding": qa.personal_attribute_query_binding,
            "personal_attribute_result_matches_query": qa.personal_attribute_result_matches_query,
            "personal_factoid_query_terms": qa.personal_factoid_query_terms,
            "personal_factoid_result_has_strong_attribute_overlap": (
                qa.personal_factoid_result_has_strong_attribute_overlap
            ),
            "match_route_tags": rg.matched_route_tags,
            "is_low_authority_result": sa.is_low_authority_result,
            "is_openclaw_historical_result": sa.is_openclaw_historical_result,
            "prefetch_policy": pp.policy_for("provider_prefetch"),
        }
    return {}


_govern = _load_recall_governance()
_govern_is_live_state_query = _govern.get("is_live_state_query")
_govern_is_durable_advice = _govern.get("is_durable_advice")
_govern_is_durable_guidance = _govern.get("is_durable_guidance")
_govern_is_operational_guidance = _govern.get("is_operational_guidance")
_govern_operational_guidance_anchors = _govern.get("operational_guidance_anchors")
_govern_is_out_of_domain = _govern.get("is_out_of_domain")
_govern_match_route_tags = _govern.get("match_route_tags")
_govern_is_positive_summary_intent = _govern.get("is_positive_summary_intent")
_govern_is_summary_excluded = _govern.get("is_summary_excluded")
_govern_query_targets_openclaw = _govern.get("query_targets_openclaw")
_govern_birthday_query_subject = _govern.get("birthday_query_subject")
_govern_birthday_fact_subject = _govern.get("birthday_fact_subject")
_govern_personal_attribute_query_binding = _govern.get("personal_attribute_query_binding")
_govern_personal_attribute_result_matches_query = _govern.get("personal_attribute_result_matches_query")
_govern_personal_factoid_query_terms = _govern.get("personal_factoid_query_terms")
_govern_personal_factoid_overlap = _govern.get("personal_factoid_result_has_strong_attribute_overlap")
_govern_is_low_authority_result = _govern.get("is_low_authority_result")
_govern_is_openclaw_historical_result = _govern.get("is_openclaw_historical_result")
_GOVERN_PREFETCH_POLICY = _govern.get("prefetch_policy")

_CONSTRAINT_QUERY_RE = re.compile(
    r"(\b("
    r"recommend(?:ations?|ed|ing|s)?|choose|pick|capabilit(?:y|ies)|tool|workflow|provider|"
    r"image|music|tts|calendar|reminder|billing|paid|api|saas|oauth|local model|"
    r"preferences?|constraints?|avoid|don't|do not|no\s+local|no\s+paid|openclaw|hermes|"
    r"brain|recall|prefetch|retrieval|memory\s+context|noise|noisy|useful|helpful|eval(?:uation)?|fine[- ]?tuning"
    r")\b|추천|선택|골라|도구|워크플로|프로바이더|이미지|음악|음성|캘린더|달력|리마인더|과금|유료|무료|로컬|선호|제약|피해야|헤르메스|브레인|리콜|검색|검색품질|메모리|노이즈|도움|품질|평가|튜닝|수정)",
    re.IGNORECASE,
)

# Stricter subset of _CONSTRAINT_QUERY_RE: only unambiguous intent words. Weak
# topic words like 'tool' / 'workflow' / 'provider' / 'image' are intentionally
# excluded — paired with a status verb they read as live-state questions, not
# constraint queries, and the broad constraint path then leaked Chris's
# preference/identity blocks into Telegram for "what is the workflow status?"
# style queries.
#
# `recommend(?:ations?|ed|ing|s)?` covers recommend / recommends / recommended /
# recommending / recommendation / recommendations. Without the suffix group,
# "Get me updated recommendations" was suppressed (plural form failed strong
# match) even though it is an explicit recommendation request — exactly the
# acceptance case the patch is supposed to surface.
_STRONG_CONSTRAINT_QUERY_RE = re.compile(
    r"(\b("
    r"recommend(?:ations?|ed|ing|s)?|choose|pick|capabilit(?:y|ies)|"
    r"preferences?|constraints?|avoid|don't|do not|"
    r"no\s+local|no\s+paid|openclaw|hermes|"
    r"brain|prefetch|retrieval|recall\s+quality|memory\s+context|no\s+noise|max(?:imally)?\s+helpful|eval(?:uation)?|fine[- ]?tuning"
    r")\b|추천|선택|골라|제약|피해야|헤르메스|브레인|검색품질|노이즈|도움|품질|평가|튜닝|수정)",
    re.IGNORECASE,
)

_BRAIN_QUALITY_QUERY_RE = re.compile(
    r"(\b(brain|recall|prefetch|retrieval|memory\s+context|noise|noisy|useful|helpful|eval(?:uation)?|fine[- ]?tuning)\b|"
    r"브레인|리콜|검색품질|메모리|노이즈|도움|품질|평가|튜닝|수정)",
    re.IGNORECASE,
)

_BRAIN_QUALITY_NOISE_MARKERS = (
    "entity: brain",
    "relationships:",
    "turning brain and openclaw from clever infrastructure",
    "2026-w",
    "underused tools",
    "brain_decide",
    "search index",
    "qdrant vector store",
    "memory dedup",
    "dedup strategy",
    "fastapi server",
    "port 8791",
    "audit rounds",
)

_GENERIC_BRAIN_INFRA_NOISE_MARKERS = (
    "knowledge gap bridge: brain system dependency",
    "brain depends on fastapi brain-server",
    "native qdrant",
    "native ollama",
)

_CONSTRAINT_EXPANSION = (
    " Chris durable constraints preferences corrections prior decisions "
    "negative preferences hard filters billing OAuth no paid SaaS API no local models"
)

# Operational durable-guidance expansion. A passive "how is/are <operational
# subject> managed/used/configured/…?" prompt wants STORED procedure guidance about
# Chris's task/runner/job world, but the terse prompt alone often recalls off-topic
# rows (topical_overlap=0 → the relevance filter zeroes the injection). When the
# shared analyzer classifies the operational-guidance class we issue a generic
# expansion probe built from the query's OWN operational anchors (task/runner/job/…)
# plus a closed procedure/framing vocabulary, so the durable operational rows are
# retrieved and survive filtering. The two term tuples are also matched against
# RESULT text (with an operational anchor) to keep an expansion row that shares no
# literal token with the terse prompt. Class-level — anchors come from the prompt,
# the verbs are a fixed linguistic class; never a probe string or task id. The
# tokens deliberately avoid "durable"/"preferences"/"constraints" so the
# out-of-domain constraint-expansion guard stays distinguishable.
_OPERATIONAL_GUIDANCE_PROCEDURE_TERMS = (
    "managed",
    "monitored",
    "configured",
    "operated",
    "handled",
    "organized",
    "executed",
    "scheduled",
)
_OPERATIONAL_GUIDANCE_FRAMING_TERMS = (
    "workflow",
    "procedure",
    "policy",
    "method",
    "management",
    "guidance",
)
# Fallback anchors when the prompt's operational noun is Hangul-only (the English
# expansion still needs concrete subject words to retrieve durable rows).
_OPERATIONAL_GUIDANCE_DEFAULT_ANCHORS = ("task", "runner", "job", "process")

_LOW_SIGNAL_STATUS_QUERY_RE = re.compile(
    r"(\b("
    r"how\s+(?:is|are)|status|update|updated|progress|start(?:ed)?|done|finished|"
    r"kanban|task|tasks|card|board|usage|remaining|left|quota|limit"
    r")\b|진행|진행상황|상태|업데이트|시작|끝났|완료|남았|잔여|한도|쿼터|작업|태스크|칸반)",
    re.IGNORECASE,
)

_EPISODIC_MARKERS = (
    "session",
    "experience",
    "message",
    "weekly/",
    "week ",
    "w15 ",
    "w20 ",
    "w21 ",
    "trip",
    "boston",
    "claude acp",
)

_RANK_STOPWORDS = {
    "about",
    "and",
    "capability",
    # Owner-name tokens are non-discriminating in an owner-scoped memory corpus
    # (nearly every durable atom about Chris contains "Chris"), so they must not
    # count as topical overlap — otherwise an off-topic row matching only the
    # owner name reads as on-query. Mirrors the owner-name stripping already done
    # in _normalize_recall_signature and the shared personal_factoid stopwords.
    "chris",
    "daehyun",
    "current",
    "generation",
    "recommend",
    "route",
    "the",
    "this",
    "versus",
    "what",
    "with",
    "workflow",
}
_MIN_OVERLAP_NON_CONSTRAINT = 1
_PREFETCH_MIN_CONFIDENCE = 0.4

# Korean domain noun → English topical equivalents. The provider's overlap scorer
# is English-biased (the Hangul tokenizer can't segment phrases), so a Korean
# calendar/reminder prompt (리마인더/캘린더/일정) scores ZERO topical overlap with
# an English durable atom ("Apple Reminders"), letting an off-topic cost/media row
# win the rank. Emitting the English equivalents for any Korean domain noun in the
# prompt restores cross-language overlap. Same KO→EN bridge the recall route uses
# for query augmentation; here it is scoped to the ranking-overlap signal only and
# never drops rows. Class-level domain nouns, not probe strings.
_KO_EN_TOPIC_EQUIV = {
    "캘린더": ("calendar",),
    "달력": ("calendar",),
    "일정": ("calendar", "schedule", "reminder"),
    "리마인더": ("reminder", "reminders"),
    "음악": ("music", "audio"),
    "음성": ("voice", "tts"),
    "이미지": ("image", "images"),
    "헤르메스": ("hermes",),
    "브레인": ("brain",),
    "작업": ("task",),
    "태스크": ("task",),
    "칸반": ("kanban",),
    "과금": ("billing", "cost"),
    "유료": ("paid", "billing"),
    "로컬": ("local",),
}

# Raw conversation / session-turn capture shape: a row whose text is a dialogue
# transcript (role-prefixed 'User:'/'Assistant:' turns). These are ingested
# Hermes/Claude session turns (and validation transcripts that merely QUOTE a
# probe), not curated answer atoms. Format/provenance signal, not a topic marker.
_CONVERSATION_TURN_RE = re.compile(r"(?im)(?:^|\n)\s*(?:user|assistant|human|유저|사용자|어시스턴트)\s*:")


def _candidate_secret_files() -> list[Path]:
    """Likely locations for the local Brain bearer token.

    Hermes profile sandboxes often set HOME to a profile-local directory while
    the user's Brain credentials live in the real user home. Prefer explicit
    configuration, then profile HOME, then common real-home hints.
    """
    candidates = [SECRET_FILE]
    for raw_home in (
        os.environ.get("HERMES_REAL_HOME"),
        os.environ.get("SUDO_USER") and f"/Users/{os.environ['SUDO_USER']}",
        os.environ.get("USER") and f"/Users/{os.environ['USER']}",
    ):
        if raw_home:
            candidates.append(Path(raw_home) / _DEFAULT_SECRET_NAME)
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded not in seen:
            seen.add(expanded)
            unique.append(expanded)
    return unique


# ─── HTTP helpers ────────────────────────────────────────────────────────────


def _load_bearer() -> str | None:
    for candidate in _candidate_secret_files():
        try:
            token = candidate.read_text().strip()
            if token:
                return token
        except FileNotFoundError:
            continue
    return None


def _brain_request(
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """One brain HTTP round-trip. Returns dict or None on failure."""
    bearer = _load_bearer()
    if not bearer:
        log.warning("brain provider: secret file missing — auth will fail")
        return None
    url = BRAIN_BASE + path
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    if actor:
        headers["x-agent"] = actor
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - BRAIN_BASE is fixed to local HTTP.
        url, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover — observability is the only contract
        log.debug("brain request %s %s failed: %s", method, path, exc)
        return None


# ─── Provider ────────────────────────────────────────────────────────────────


class BrainMemoryProvider(MemoryProvider):
    """Brain HTTP-backed memory provider, profile-aware via agent tag."""

    def __init__(self) -> None:
        self._profile: str = "default"
        self._hermes_home: str = ""
        self._platform: str = "cli"
        self._session_id: str = ""
        self._write_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def name(self) -> str:
        return "brain"

    # ── Required: availability check (no network) ────────────────────────────

    def is_available(self) -> bool:
        """Config check only. Secret file must exist."""
        return any(candidate.is_file() for candidate in _candidate_secret_files())

    # ── Required: lifecycle ──────────────────────────────────────────────────

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", "") or ""
        self._platform = kwargs.get("platform", "cli")

        # Profile name comes from agent_identity (kwargs from MemoryManager).
        # Fallback: derive from HERMES_HOME path (~/.hermes/profiles/<name>).
        profile = kwargs.get("agent_identity") or ""
        if not profile and self._hermes_home:
            parts = Path(self._hermes_home).parts
            if "profiles" in parts:
                i = parts.index("profiles")
                if i + 1 < len(parts):
                    profile = parts[i + 1]
        self._profile = profile or "default"

        # Verify brain reachable (best-effort; non-fatal if down).
        health = _brain_request("/brain/health", timeout=3.0)
        if health is None:
            log.warning("brain provider: brain HTTP unreachable at %s — sessions will degrade", BRAIN_BASE)
        else:
            log.info(
                "brain provider initialized: profile=%s session=%s platform=%s",
                self._profile,
                self._session_id,
                self._platform,
            )

        # Start background writer thread.
        self._writer_thread = threading.Thread(target=self._writer_loop, name="brain-mem-writer", daemon=True)
        self._writer_thread.start()

    def shutdown(self) -> None:
        self._write_queue.put(None)  # sentinel
        if self._writer_thread:
            self._writer_thread.join(timeout=2.0)
        self._stopping.set()

    # ── System prompt block (static) ─────────────────────────────────────────

    def system_prompt_block(self) -> str:
        return (
            f"You have access to Chris's brain (canonical memory at "
            f"{BRAIN_BASE}) via this profile: '{self._profile}'.\n"
            f"Memory writes and reads flow through brain's HTTP API, tagged "
            f"with agent:{self._profile} for profile-scoped recall. The brain "
            f"MCP tools (brain_recall, brain_store, brain_decide, etc.) are "
            f"the explicit interface; prefetched recall is injected per turn."
        )

    # ── Prefetch (per-turn recall) ───────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not query.strip():
            return ""
        results: list[dict[str, Any]] = []
        # Out-of-domain world-knowledge prompts (recipes, general how-tos) have no
        # durable personal answer. They must NOT be rewritten into a durable
        # preference/constraint fetch — that is how an anchor-less prompt leaks
        # unrelated preferences. The raw query still defers to /recall/v2 (which
        # drops out-of-domain rows); we only suppress the constraint EXPANSION
        # here so out-of-domain precedence holds over provider expansion. No-op
        # when brain_core is off-path.
        out_of_domain = bool(_govern_is_out_of_domain is not None and _govern_is_out_of_domain(query))
        if out_of_domain and _govern_match_route_tags is not None and _govern_match_route_tags(query):
            # A durable route claims this query (a named runtime/tool/cost topic
            # the generic anchor set does not enumerate, e.g. OpenClaw/Hermes) —
            # it is in-domain after all, so do not suppress its constraint path.
            out_of_domain = False
        is_constraint_query = self._is_constraint_query(query) and not out_of_domain

        # Status/usage/task questions are usually about live runtime state, not
        # durable memory. Broad RAG here caused Telegram-visible noise such as
        # old local-model or ACP memories. Let explicit Brain tools or task
        # tools answer these instead of injecting low-signal recall blocks.
        # Only a STRONG constraint signal (recommend/choose/preference/no paid/
        # openclaw/hermes/추천/제약/…) OR a shared durable-advice intent
        # (recommend/prefer/preference/추천/선호) overrides the suppression — weak
        # topic words like 'workflow' paired with 'status'/'update' still leak ~2KB
        # of canonical preference blocks into Telegram if allowed through. The
        # durable-guidance override reuses the SAME shared class as the live-state
        # classifier — explicit advice (recommend/prefer/추천/선호) OR a how-to/
        # workflow/procedure/policy/method/monitoring framing with no present-time
        # deixis. So a durable workflow/procedure prompt ("what workflow should I
        # use for running tasks?", "실행 중인 작업 관리 방법 알려줘") is not suppressed
        # here merely for containing a 'task'/'running' word that lacks a
        # 'recommend' verb and that _STRONG_CONSTRAINT_QUERY_RE misses. A non-durable
        # present-status prompt ("what is the current task status right now?")
        # carries no durable-guidance intent, so it stays suppressed.
        durable_guidance = bool(
            _govern_is_durable_guidance is not None and _govern_is_durable_guidance(query)
        )
        # A durable-advice intent (recommend/prefer/preference/추천/선호) also
        # overrides the low-signal-status suppression — matching the documented
        # contract above. The strong-constraint regex requires the full
        # "preference(s)" form, so a bare "prefer" verb on a low-signal "usage"/
        # "status" prompt ("What does Chris prefer for … API usage?") was wrongly
        # suppressed to empty even though /recall/v2 had a durable preference row.
        durable_advice = bool(_govern_is_durable_advice is not None and _govern_is_durable_advice(query))
        if (
            self._is_low_signal_status_query(query)
            and not self._is_strong_constraint_query(query)
            and not durable_guidance
            and not durable_advice
        ):
            return ""

        # Shared live-state gate (same classifier as /recall/v2 and
        # /recall/active): present-status / in-progress / done-right-now prompts
        # (EN+KO, incl. colloquial) are answered by live tools, not stale memory.
        # This is an ABSOLUTE precedence gate — it must NOT be overridden by the
        # local strong-constraint regex, which fired on a bare "brain"/"prefetch"
        # term and leaked stale constraint blocks for live-state questions like
        # "brain 진행상황 지금" or "what is the brain prefetch pipeline running now".
        # The shared classifier already exempts genuine durable advice/preference
        # prompts (recommend/prefer/추천/선호), so no local override is needed.
        # /recall/v2 and /recall/active gate live-state unconditionally too. No-op
        # when brain_core is off-path (the broader local status gate handles it).
        if _govern_is_live_state_query is not None and _govern_is_live_state_query(query):
            return ""

        if is_constraint_query:
            # Durable constraints must gate capability/tool recommendations.
            # canonical_first searches the canonical/distilled truth layer; do
            # this before broad RAG so episodic memories cannot crowd it out.
            for recall_query, canonical_first in self._constraint_recall_queries(query):
                results.extend(
                    self._recall(
                        recall_query,
                        n=PREFETCH_K * 2,
                        canonical_first=canonical_first,
                    )
                )
            for recall_query in self._constraint_semantic_recall_queries(query):
                results.extend(
                    self._recall(
                        recall_query,
                        n=PREFETCH_K * 2,
                        collection="semantic_memory",
                    )
                )

        # Operational durable-guidance ("how is the runner configured?", "how are
        # running jobs managed?"): the terse prompt alone recalls off-topic rows
        # (topical_overlap=0 → filtered to empty even though /recall/v2 returns a
        # non-empty count). The shared analyzer classifies this class (durable-
        # guidance frame x operational-domain anchor); we expand the recall with a
        # generic operational-guidance probe built from the prompt's OWN anchors so
        # the durable task/runner/job rows are retrieved and survive filtering. Live-
        # state and out-of-domain prompts never reach here (gated above), and a
        # recipe how-to carries no operational anchor — so this is scoped to the
        # approved passive durable-guidance class. No-op when brain_core is off-path.
        is_operational_guidance = bool(
            _govern_is_operational_guidance is not None and _govern_is_operational_guidance(query)
        )
        if is_operational_guidance:
            for recall_query in self._operational_guidance_recall_queries(query):
                results.extend(self._recall(recall_query, n=PREFETCH_K * 2, canonical_first=True))
                results.extend(self._recall(recall_query, n=PREFETCH_K * 2, collection="semantic_memory"))

        # Open-ended personal-fact / durable-memory probe ("What should I remember
        # about Chris OMSCS Fall 2026?"): the full phrasing is diluted by generic
        # reminder/scaffolding words, so the raw query can miss a durable row that a
        # FOCUSED query on the DISTINCTIVE terms (acronym + supporting tokens)
        # retrieves cleanly from canonical/semantic. Issue that focused variant when
        # the prompt is a pure personal-fact probe (no constraint expansion path of
        # its own). Terms come from the shared personal_factoid analyzer — distinctive
        # content tokens with subject/scaffolding stripped — never a probe string.
        factoid_terms = (
            _govern_personal_factoid_query_terms(query)
            if _govern_personal_factoid_query_terms is not None
            else frozenset()
        )
        if factoid_terms and not is_constraint_query:
            focused = self._personal_factoid_focused_query(query, factoid_terms)
            if focused:
                results.extend(self._recall(focused, n=PREFETCH_K * 2, canonical_first=True))
                results.extend(self._recall(focused, n=PREFETCH_K * 2, collection="semantic_memory"))

        # Broad fallback: do not force collection=semantic_memory. That filter
        # excluded canonical/distilled constraints and caused noisy episodic
        # memories to be injected for tool/capability discussions.
        results.extend(self._recall(query, n=PREFETCH_K))

        if not results:
            return ""
        ranked = self._rank_results(results, query=query)
        filtered = self._filter_relevant_results(
            ranked,
            query=query,
            require_relevance=True,
            is_constraint_query=is_constraint_query,
            is_operational_guidance=is_operational_guidance,
        )
        return self._format_recall(filtered[:PREFETCH_K])

    def _recall(
        self,
        query: str,
        *,
        n: int,
        canonical_first: bool = False,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        params_dict: dict[str, Any] = {
            "q": query,
            "n": n,
            "agent": self._profile,
        }
        if canonical_first:
            params_dict["canonical_first"] = "true"
        if collection:
            params_dict["collection"] = collection
        params = urllib.parse.urlencode(params_dict)
        resp = _brain_request(
            f"/recall/v2?{params}",
            timeout=PREFETCH_TIMEOUT_S,
            actor=self._profile,
        )
        if not resp:
            return []
        raw = resp.get("results")
        return raw if isinstance(raw, list) else []

    @staticmethod
    def _is_constraint_query(query: str) -> bool:
        return bool(_CONSTRAINT_QUERY_RE.search(query or ""))

    @staticmethod
    def _is_low_signal_status_query(query: str) -> bool:
        return bool(_LOW_SIGNAL_STATUS_QUERY_RE.search(query or ""))

    @staticmethod
    def _is_strong_constraint_query(query: str) -> bool:
        return bool(_STRONG_CONSTRAINT_QUERY_RE.search(query or ""))

    @staticmethod
    def _is_brain_quality_query(query: str) -> bool:
        return bool(_BRAIN_QUALITY_QUERY_RE.search(query or ""))

    @staticmethod
    def _constraint_recall_queries(query: str) -> list[tuple[str, bool]]:
        topics = BrainMemoryProvider._query_topics(query)
        topic_text = " ".join(topics)
        base = query + _CONSTRAINT_EXPANSION
        queries = [
            (base, True),
            (
                f"Chris durable preferences constraints corrections prior decisions about {topic_text or query}",
                True,
            ),
            (
                f"Chris hard boundaries avoid negative preferences billing local cloud provider workflow {topic_text}",
                True,
            ),
        ]
        lowered = query.lower()
        if any(term in lowered for term in ("music", "tts", "voice", "audio", "음악", "음성")):
            queries.append(
                ("Chris no local generation models no paid SaaS API billing music TTS subscription CLI", True)
            )
            queries.append(
                (
                    "For music/TTS capability recommendations, Chris has hard constraints against local generation models",
                    True,
                )
            )
            queries.append(("No separate AI API costs paid SaaS API billing music TTS capability", True))
        if any(term in lowered for term in ("calendar", "reminder", "캘린더", "달력", "리마인더")):
            queries.append(("Apple Reminders Apple Calendar Chris preference", True))
        if "image" in lowered or "이미지" in lowered or "gpt-images" in lowered:
            queries.append(("OpenAI Codex OAuth image generation no separate billing", True))
        if "openclaw" in lowered or "hermes" in lowered or "헤르메스" in lowered:
            queries.append(("OpenClaw historical context current agent runtime treated as Hermes", True))
        if BrainMemoryProvider._is_brain_quality_query(query):
            queries.append(
                ("Brain recall prefetch quality no noise maximally helpful context eval score", True)
            )
            queries.append(
                ("Chris wants Brain fine-tuning judged by measurable eval score improvements", True)
            )
            queries.append(("Brain retrieval quality noise suppression canonical-first prefetch eval", True))
        return queries

    @staticmethod
    def _constraint_semantic_recall_queries(query: str) -> list[str]:
        queries: list[str] = []
        lowered = query.lower()
        if any(term in lowered for term in ("calendar", "reminder")):
            queries.append("Apple Calendar Reminders Google Calendar by default")
        if "openclaw" in lowered or "hermes" in lowered:
            queries.append("OpenClaw references historical context current agent runtime Hermes")
        if any(term in lowered for term in ("music", "tts", "voice", "audio")):
            queries.append(
                "local LLM inference local models not generation paid SaaS API billing subscription CLI"
            )
        if BrainMemoryProvider._is_brain_quality_query(query):
            queries.append(
                "Brain prefetch recall quality no noise maximally helpful measurable eval improvements"
            )
        return queries

    @staticmethod
    def _operational_guidance_recall_queries(query: str) -> list[str]:
        """Generic expansion probe(s) for the operational durable-guidance class.

        Built from the prompt's OWN operational anchors (task/runner/job/…, via the
        shared analyzer) plus the closed procedure/framing vocabulary — never the
        raw prompt string. Falls back to default subject anchors when the prompt's
        operational noun is Hangul-only, so the English probe still has concrete
        subject words to retrieve durable rows."""
        anchors = sorted(
            _govern_operational_guidance_anchors(query)
            if _govern_operational_guidance_anchors is not None
            else set()
        )
        latin_anchors = [a for a in anchors if a.isascii()]
        if not latin_anchors:
            latin_anchors = list(_OPERATIONAL_GUIDANCE_DEFAULT_ANCHORS)
        terms = " ".join(
            (
                "Chris",
                *latin_anchors,
                *_OPERATIONAL_GUIDANCE_FRAMING_TERMS,
                *_OPERATIONAL_GUIDANCE_PROCEDURE_TERMS,
            )
        )
        return [terms]

    @staticmethod
    def _personal_factoid_focused_query(query: str, terms: frozenset[str]) -> str:
        """The distinctive personal_factoid terms in the prompt's own order/case —
        i.e. the query with generic reminder/scaffolding words removed. "What should
        I remember about Chris OMSCS Fall 2026?" → "OMSCS Fall 2026". Latin words are
        kept in original order/case; Hangul distinctive terms (script-segmented, so
        the Latin scan misses them) are appended. Falls back to the sorted term join
        if the ordered scan yields nothing."""
        ordered = [w for w in re.findall(r"[A-Za-z0-9]+", query) if w.lower() in terms]
        for term in terms:
            if not term.isascii() and term in query and term not in ordered:
                ordered.append(term)
        return " ".join(ordered) or " ".join(sorted(terms))

    def _rank_results(self, results: list[dict[str, Any]], *, query: str = "") -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for result in results:
            key = self._near_duplicate_key(result) or str(result.get("id") or result.get("path") or "")
            if not key:
                continue
            existing = merged.get(key)
            if not existing or self._rank_score(result, query=query) > self._rank_score(
                existing, query=query
            ):
                merged[key] = result
        ranked = sorted(
            merged.values(), key=lambda result: self._rank_score(result, query=query), reverse=True
        )
        deduped: list[dict[str, Any]] = []
        kept_signatures: list[str] = []
        for result in ranked:
            sig = self._near_duplicate_key(result)
            if self._is_near_duplicate_signature(sig, kept_signatures):
                continue
            deduped.append(result)
            if sig:
                kept_signatures.append(sig)
        return deduped

    @staticmethod
    def _normalize_recall_signature(text: str) -> str:
        lowered = (text or "").lower()
        lowered = re.sub(r"https?://\S+", " ", lowered)
        lowered = re.sub(r"\b20\d{2}(?:[-_/]?w?\d{1,2})?(?:[-_/]\d{1,2})?\b", " ", lowered)
        lowered = re.sub(r"\b\d+(?:\.\d+)?%?\b", " ", lowered)
        tokens = [tok for tok in re.findall(r"[a-z0-9가-힣]+", lowered) if len(tok) > 2]
        stop = {
            "chris",
            "wants",
            "want",
            "prefers",
            "preference",
            "should",
            "that",
            "with",
            "from",
            "into",
            "the",
            "and",
            "for",
            "his",
            "her",
        }
        return " ".join(tok for tok in tokens if tok not in stop)

    @staticmethod
    def _near_duplicate_key(result: dict[str, Any]) -> str:
        text = " ".join(str(result.get(k) or "") for k in ("title", "content", "path"))
        sig = BrainMemoryProvider._normalize_recall_signature(text)
        tokens = set(sig.split())
        if {"brain", "eval", "score"}.issubset(tokens) and ({"improvement", "improvements"} & tokens):
            return "brain-eval-score-improvement-preference"
        if {"브레인", "평가"}.issubset(tokens) and ({"점수", "개선"} & tokens):
            return "brain-eval-score-improvement-preference"
        return sig

    @staticmethod
    def _is_near_duplicate_signature(candidate: str, kept: list[str]) -> bool:
        if not candidate:
            return False
        c_tokens = set(candidate.split())
        if len(c_tokens) < 4:
            return candidate in kept
        for existing in kept:
            if candidate == existing:
                return True
            e_tokens = set(existing.split())
            if len(e_tokens) < 4:
                continue
            overlap = len(c_tokens & e_tokens) / max(1, min(len(c_tokens), len(e_tokens)))
            if overlap >= 0.86:
                return True
        return False

    @staticmethod
    def _result_haystack(result: dict[str, Any]) -> str:
        """Title/content/path text for the shared source-authority classifier
        (used for its distilled-brain-analysis text check)."""
        return " ".join(str(result.get(k) or "") for k in ("title", "content", "path"))

    @staticmethod
    def _is_conversation_transcript_row(result: dict[str, Any]) -> bool:
        """True for a raw conversation / session-turn capture — a row whose text is
        a dialogue transcript (role-prefixed 'User:'/'Assistant:' turns), e.g. an
        ingested Hermes/Claude session turn or a validation transcript that merely
        QUOTES a probe. A FORMAT/provenance signal, not a topic keyword: a
        declarative answer atom ('OMSCS: Chris is enrolling …') has no turn markers
        and is kept. Used to keep the pure personal-fact probe class answer-only —
        these transcript rows share the answer's raw_events provenance, so only
        format separates them."""
        hay = "\n".join(str(result.get(k) or "") for k in ("title", "content"))
        if _CONVERSATION_TURN_RE.search(hay):
            return True
        low = hay.lower()
        return "user:" in low and "assistant:" in low

    @staticmethod
    def _result_confidence(result: dict[str, Any]) -> float | None:
        raw = result.get("confidence")
        if raw is None and isinstance(result.get("metadata"), dict):
            raw = result["metadata"].get("confidence")
        if raw is None:
            return None
        with contextlib.suppress(TypeError, ValueError):
            return float(raw)
        return None

    @staticmethod
    def _is_low_confidence_prefetch_result(result: dict[str, Any]) -> bool:
        conf = BrainMemoryProvider._result_confidence(result)
        if conf is None:
            return False
        # Direct current-truth guarantees are route-policy rows, not uncertain
        # user-memory atoms; do not drop them due to absent/foreign confidence.
        if BrainMemoryProvider._is_route_guarantee_row(result):
            return False
        return conf < _PREFETCH_MIN_CONFIDENCE

    @staticmethod
    def _is_route_guarantee_row(result: dict[str, Any]) -> bool:
        """A server-injected route_guarantee (direct_current_truth). The durable
        fact must survive the summary-excluded filter."""
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        return (
            str(result.get("source_type") or "").lower() == "route_guarantee"
            or str(meta.get("authority_tier") or "").lower() == "direct_current_truth"
        )

    @staticmethod
    def _route_guarantee_serves_query(result: dict[str, Any], query_route_tags: set[str]) -> bool:
        """True when ``result`` is a route_guarantee row whose route is one the
        query matched — i.e. it states the durable answer for THIS query's route.
        Route guarantee rows carry the title ``"<route> route guarantee"`` (the
        convention shared by active-recall and the server injection)."""
        if not query_route_tags or not BrainMemoryProvider._is_route_guarantee_row(result):
            return False
        title = str(result.get("title") or "").lower()
        route = title.replace("route guarantee", "").strip()
        return route in query_route_tags

    @staticmethod
    def _is_distilled_row(result: dict[str, Any]) -> bool:
        """True for rows from the derived distilled layer (collection/source_type
        == 'distilled') — a summary-format provenance regardless of topic."""
        return "distilled" in (
            str(result.get("collection") or "").lower(),
            str(result.get("source_type") or "").lower(),
        )

    def _filter_relevant_results(
        self,
        results: list[dict[str, Any]],
        *,
        query: str,
        require_relevance: bool,
        is_constraint_query: bool = False,
        is_operational_guidance: bool = False,
    ) -> list[dict[str, Any]]:
        if not require_relevance:
            return results
        # Personal-attribute identity guard (strictest surface). A self/possessive
        # attribute query ("what is my address?", "when is Chris's birthday?", "내
        # 주소가 뭐야?", "what is Ellie's phone number?") targets ONE identity's ONE
        # attribute. Inject ONLY a row that states the TARGET identity's SAME
        # attribute — a different entity's value, a different attribute of the same
        # identity, or an unrelated row is identity/attribute contamination, so it
        # is dropped and prefetch is empty when the target value is unknown.
        # Identity+attribute-scoped, not a blanket suppressor: a legitimate explicit
        # third-person query keeps its match. Birthday is one instance of this
        # class. No-op when brain_core is off-path. Shared analyzer class, no probe.
        attr_binding = (
            _govern_personal_attribute_query_binding(query)
            if _govern_personal_attribute_query_binding is not None
            else None
        )
        if attr_binding is not None and _govern_personal_attribute_result_matches_query is not None:
            return [
                result
                for result in results
                if not self._is_low_confidence_prefetch_result(result)
                and _govern_personal_attribute_result_matches_query(query, self._result_haystack(result))
            ]
        # provider_prefetch policy: never inject low-authority session/reflection/
        # summary/procedure rows into a system prompt unless the user explicitly
        # asked for a summary. allow_low_authority is False for this mode; a
        # durable hard-constraint hit is still exempt. No-op when brain_core is
        # off-path.
        allow_low_authority = bool(
            _GOVERN_PREFETCH_POLICY is None or _GOVERN_PREFETCH_POLICY.allow_low_authority
        )
        summary_intent = bool(
            _govern_is_positive_summary_intent is not None and _govern_is_positive_summary_intent(query)
        )
        # OpenClaw is historical context (Hermes is current — the durable
        # runtime_distinction fact). For the strict provider surface, drop stale
        # OpenClaw-provenance rows UNLESS the prompt is actually about OpenClaw or
        # the agents. This catches OpenClaw-era distilled/session restatements
        # that survive as hard-constraint hits and leak "OpenClaw …" provenance
        # into a current cost/tooling/preference recommendation. No-op when
        # brain_core is off-path. Symmetric query/result gate, no per-probe list.
        drop_openclaw_historical = bool(
            _govern_query_targets_openclaw is not None
            and _govern_is_openclaw_historical_result is not None
            and not _govern_query_targets_openclaw(query)
        )
        # Explicit "not a summary" / "요약 말고" intent: drop derived summary and
        # distilled rows even when they read as hard-constraint hits — only direct
        # current truth (route guarantees, clean canonical) belongs in the
        # injection. No-op when brain_core is off-path.
        summary_excluded = bool(
            _govern_is_summary_excluded is not None and _govern_is_summary_excluded(query)
        )
        # Finding 3 (provider_low_authority_topk_leakage): once a route guarantee
        # for THIS query's route already states the durable answer, the hard-
        # constraint exemption below must NOT rescue low-authority distilled/
        # reflection/session/procedure rows that merely contain a constraint
        # phrase — the guarantee is the answer (empty-beats-wrong). Scoped to the
        # query's OWN route tags, so an off-topic guarantee (e.g. a cost guarantee
        # present for an OpenClaw-runtime question) does NOT strip a row the query
        # actually is about. Provenance + route signal, never a per-probe list.
        query_route_tags = _govern_match_route_tags(query) if _govern_match_route_tags is not None else set()
        has_satisfying_route_guarantee = any(
            self._route_guarantee_serves_query(r, query_route_tags) for r in results
        )
        # A route_guarantee row only states THIS query's durable truth when its route
        # is one the ORIGINAL query matched. The provider's constraint EXPANSION
        # (billing/OAuth/no paid SaaS API/…) can make /recall/v2 inject an off-route
        # guarantee (e.g. a cost_billing guarantee for a calendar/reminders prompt);
        # that direct-current-truth row would otherwise lead the injection. Drop it
        # when its route is not in the query's own route tags. Only enforced when the
        # shared route matcher is on-path; fail-open otherwise.
        route_guarantee_route_known = _govern_match_route_tags is not None
        # Open-ended personal_factoid gate (mirror of /recall/v2's quality filter),
        # scoped to PURE personal-fact probes: a query naming a personal subject
        # (Chris/my/user) that carries NO matched route, NO constraint/recommend
        # intent, and is NOT an explicit summary request. For these ("Chris childhood
        # … first grade teacher", "What should I remember about Chris OMSCS Fall
        # 2026?") a row must share the requested attribute terms as WHOLE words to be
        # injected, so an unrelated design/profile row whose only overlap is a
        # hyphen-compound fragment ('content-first'→first, 'production-grade'→grade)
        # abstains to empty. Preference/tooling/route prompts (codex/deployment/
        # calendar/cost) are constraint queries or carry a route tag, so the gate
        # stays off and their existing ranking is untouched.
        personal_factoid_terms = (
            _govern_personal_factoid_query_terms(query)
            if _govern_personal_factoid_query_terms is not None
            else frozenset()
        )
        apply_factoid_gate = bool(
            personal_factoid_terms
            and not summary_intent
            and not is_constraint_query
            and not query_route_tags
            and _govern_personal_factoid_overlap is not None
        )
        filtered: list[dict[str, Any]] = []
        rejected_noise = False
        for result in results:
            if (
                route_guarantee_route_known
                and self._is_route_guarantee_row(result)
                and not self._route_guarantee_serves_query(result, query_route_tags)
            ):
                rejected_noise = True
                continue
            if (
                apply_factoid_gate
                and not self._is_route_guarantee_row(result)
                and _govern_personal_factoid_overlap(query, self._result_haystack(result)) is False
            ):
                rejected_noise = True
                continue
            if (
                apply_factoid_gate
                and not self._is_route_guarantee_row(result)
                and self._is_conversation_transcript_row(result)
            ):
                # Pure personal-fact probe: a raw conversation/session-turn capture
                # that merely QUOTES the probe terms (an ingested validation
                # transcript) is not a durable answer — drop it so negatives stay
                # empty and only a declarative answer atom is injected. The answer
                # and the transcript share raw_events provenance, so format (turn
                # markers) is the only generic separator.
                rejected_noise = True
                continue
            if self._is_low_confidence_prefetch_result(result):
                rejected_noise = True
                continue
            if self._is_generic_brain_infra_noise(result, query):
                rejected_noise = True
                continue
            if self._is_brain_quality_query(query) and self._is_brain_quality_noise(result, query):
                rejected_noise = True
                continue
            if drop_openclaw_historical and _govern_is_openclaw_historical_result(
                result, self._result_haystack(result)
            ):
                rejected_noise = True
                continue
            if (
                summary_excluded
                and not self._is_route_guarantee_row(result)
                and (
                    self._is_distilled_row(result)
                    or (
                        _govern_is_low_authority_result is not None
                        and _govern_is_low_authority_result(result, self._result_haystack(result))
                    )
                )
            ):
                # Summary-excluded query: derived summary/distilled rows are dropped
                # regardless of the hard-constraint exemption below.
                rejected_noise = True
                continue
            if (
                not allow_low_authority
                and not summary_intent
                and _govern_is_low_authority_result is not None
                and _govern_is_low_authority_result(result, self._result_haystack(result))
                and not (
                    is_constraint_query
                    and self._is_hard_constraint_hit(result, query)
                    and not has_satisfying_route_guarantee
                )
            ):
                rejected_noise = True
                continue
            if (
                self._topical_overlap(result, query) >= _MIN_OVERLAP_NON_CONSTRAINT
                or (is_constraint_query and self._is_hard_constraint_hit(result, query))
                or (is_operational_guidance and self._is_operational_guidance_hit(result))
            ):
                filtered.append(result)
        if rejected_noise:
            # Something was rejected as infra-noise, brain-quality noise, or
            # low-authority under the strict provider_prefetch policy. Those are
            # substantive quality decisions, so the filtered set is authoritative
            # — never fall back to the unfiltered results, or the rejected
            # stale/noisy/low-authority rows would be reinjected into the system
            # prompt (empty beats wrong context for this surface).
            return filtered
        # Nothing was rejected as noise/low-authority — rows only dropped by
        # ordinary topical-overlap matching. Don't turn prefetch into an empty
        # string just because a terse prompt shares no literal terms with the
        # returned memory; fall back to the unfiltered results in that case.
        return filtered or results

    @staticmethod
    def _is_brain_quality_noise(result: dict[str, Any], query: str) -> bool:
        haystack = " ".join(
            str(result.get(key) or "").lower()
            for key in ("title", "content", "path", "collection", "source_type")
        )
        lowered_query = (query or "").lower()
        # If Chris asks about a specific Brain subsystem, let those results through.
        if "dedup" in lowered_query or "중복" in lowered_query:
            return False
        if "index" in lowered_query or "qdrant" in lowered_query or "인덱스" in lowered_query:
            return False
        if "brain_decide" in lowered_query or "underused" in lowered_query:
            return False
        return any(marker in haystack for marker in _BRAIN_QUALITY_NOISE_MARKERS)

    @staticmethod
    def _is_generic_brain_infra_noise(result: dict[str, Any], query: str) -> bool:
        haystack = " ".join(
            str(result.get(key) or "").lower()
            for key in ("title", "content", "path", "collection", "source_type")
        )
        lowered_query = (query or "").lower()
        if any(
            term in lowered_query
            for term in ("dependency", "server", "qdrant", "ollama", "fastapi", "의존", "서버")
        ):
            return False
        return any(marker in haystack for marker in _GENERIC_BRAIN_INFRA_NOISE_MARKERS)

    @staticmethod
    def _topical_overlap(result: dict[str, Any], query: str) -> int:
        title = str(result.get("title") or "").lower()
        content = str(result.get("content") or "").lower()
        tokens = set(BrainMemoryProvider._query_topics(query))
        return sum(1 for token in tokens if token in title or token in content)

    @staticmethod
    def _query_topics(query: str) -> list[str]:
        tokens = []
        for token in re.findall(r"[a-z0-9]+", query.lower()):
            if len(token) >= 4 and token not in _RANK_STOPWORDS:
                tokens.append(token)
        # Keep common Korean topic terms even though the simple English tokenizer
        # cannot segment Hangul phrases. This preserves relevance checks for the
        # Telegram path Chris actually uses.
        for term in (
            "브레인",
            "리콜",
            "검색품질",
            "메모리",
            "노이즈",
            "도움",
            "품질",
            "평가",
            "튜닝",
            "이미지",
            "음악",
            "음성",
            "캘린더",
            "달력",
            "리마인더",
            "과금",
            "유료",
            "로컬",
            "헤르메스",
            "작업",
            "태스크",
            "칸반",
        ):
            if term in query:
                tokens.append(term)
        # Cross-language bridge: emit English equivalents for any Korean domain
        # noun in the prompt so overlap with English durable atoms is non-zero
        # (substring match handles particle-glued forms like 리마인더는/일정이랑).
        for ko_term, en_terms in _KO_EN_TOPIC_EQUIV.items():
            if ko_term in query:
                tokens.extend(en_terms)
        return list(dict.fromkeys(tokens))

    @staticmethod
    def _constraint_phrases(query: str) -> list[str]:
        query_lower = query.lower()
        phrases = [
            "no paid",
            "no separate",
            "api billing",
            "ai api costs",
            "subscription-backed",
            "no billing",
            "avoid billing",
        ]
        if any(term in query_lower for term in ("music", "tts", "voice", "audio", "음악", "음성")):
            phrases.extend(
                (
                    "no local",
                    "local models",
                    "local generation models",
                    "local llm",
                    "not generation",
                    "subscription cli",
                    "paid saas",
                )
            )
        if "image" in query_lower or "이미지" in query_lower or "gpt-images" in query_lower:
            phrases.extend(("oauth", "openai codex", "gpt images", "gpt-images"))
        if any(term in query_lower for term in ("calendar", "reminder", "캘린더", "달력", "리마인더")):
            phrases.extend(("apple calendar", "apple reminders", "google calendar"))
        if "openclaw" in query_lower or "hermes" in query_lower or "헤르메스" in query_lower:
            phrases.extend(
                (
                    "historical context",
                    "current agent runtime",
                    "treated as hermes",
                    "architecturally different",
                )
            )
        if BrainMemoryProvider._is_brain_quality_query(query):
            phrases.extend(
                (
                    "brain recall quality",
                    "brain prefetch",
                    "memory injection",
                    "no noise",
                    "maximally helpful",
                    "measurable eval score",
                    "eval score improvements",
                    "retrieval quality",
                    "noise suppression",
                    "canonical-first",
                )
            )
        return phrases

    @staticmethod
    def _is_hard_constraint_hit(result: dict[str, Any], query: str) -> bool:
        title = str(result.get("title") or "").lower()
        content = str(result.get("content") or "").lower()
        return any(
            phrase in title or phrase in content for phrase in BrainMemoryProvider._constraint_phrases(query)
        )

    @staticmethod
    def _is_operational_guidance_hit(result: dict[str, Any]) -> bool:
        """True for a row that states operational procedure guidance: its text
        carries BOTH an operational-domain anchor (task/runner/job/scheduler/…, via
        the shared analyzer vocabulary) AND a procedure/framing term (managed/
        configured/workflow/…). Lets an operational-guidance expansion row survive
        the relevance filter for a terse prompt it shares no literal token with —
        the durable answer to "how is the runner configured?" need not repeat
        "configured". Provenance-neutral; low-authority rows are already dropped
        before this check, so it only rescues curated/canonical operational rows."""
        haystack = " ".join(str(result.get(k) or "") for k in ("title", "content")).lower()
        if not haystack.strip():
            return False
        has_anchor = bool(
            _govern_operational_guidance_anchors is not None
            and _govern_operational_guidance_anchors(haystack)
        )
        if not has_anchor:
            return False
        return any(
            term in haystack
            for term in (
                *_OPERATIONAL_GUIDANCE_PROCEDURE_TERMS,
                *_OPERATIONAL_GUIDANCE_FRAMING_TERMS,
            )
        )

    @staticmethod
    def _rank_score(result: dict[str, Any], *, query: str = "") -> float:
        try:
            raw_score = float(result.get("score") or 0.0)
        except (TypeError, ValueError):
            raw_score = 0.0
        collection = str(result.get("collection") or "").lower()
        source_type = str(result.get("source_type") or "").lower()
        title = str(result.get("title") or "").lower()
        content = str(result.get("content") or "").lower()
        path = str(result.get("path") or "").lower()
        trust_tier = result.get("trust_tier")

        # Tier dominates vector score only when the hit is still topically
        # plausible. Brain canonical-first can return generic canonical pages;
        # those should not beat a directly relevant hard-filter result.
        durable = collection in {"canonical", "distilled"} or source_type in {"canonical", "distilled"}
        episodic = collection in {"experience", "personal"} or any(
            marker in title or marker in content or marker in path for marker in _EPISODIC_MARKERS
        )
        overlap = BrainMemoryProvider._topical_overlap(result, query)
        title_overlap = sum(1 for token in BrainMemoryProvider._query_topics(query) if token in title)
        hard_constraint = BrainMemoryProvider._is_hard_constraint_hit(result, query)
        # A route-guarantee row is the server's first-class current/direct truth
        # for a matched durable route, so it must outrank an incidental
        # hard-constraint phrase match in an older distilled escalation summary.
        route_guarantee = (
            source_type == "route_guarantee"
            or str((result.get("metadata") or {}).get("authority_tier") or "").lower()
            == "direct_current_truth"
        )
        # A hard-constraint phrase only earns the constraint rank-tier when the
        # row is also topically on-query (a discriminating overlap or a title
        # hit). The constraint-phrase set always carries generic cost/billing
        # phrases ("no paid", "subscription-backed", "api billing"); without this
        # relevance gate an off-topic cost/media row matching only those phrases
        # leapfrogs a topically-relevant row on an unrelated-domain prompt (e.g. a
        # cost decision outranking the calendar/reminders answer for a calendar
        # question). Owner-name tokens are stopworded out of overlap, so the owner
        # name alone is not "on-query". Provenance/relevance-neutral, no probe list.
        constraint_on_topic = hard_constraint and (overlap >= 1 or title_overlap >= 1)
        if route_guarantee:
            score = 20_000.0
        elif constraint_on_topic:
            score = 15_000.0
        elif durable and overlap >= 2:
            score = 10_000.0
        else:
            score = 1_000.0
        if durable:
            score += 1_000.0
        score += min(raw_score, 100.0) / 10.0
        score += overlap * 150.0
        score += title_overlap * 500.0

        if any(
            word in title or word in content
            for word in ("correction", "corrected", "explicitly", "constraint")
        ):
            score += 300.0
        if any(
            word in title or word in content
            for word in ("preference", "decision", "no paid", "no local", "oauth")
        ):
            score += 200.0
        if trust_tier is not None:
            with contextlib.suppress(TypeError, ValueError):
                score += max(0.0, 4.0 - float(trust_tier)) * 20.0
        if episodic:
            score -= 500.0
        return score

    def _format_recall(self, results: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for r in results[:PREFETCH_K]:
            title = (r.get("title") or "").strip() or "(untitled)"
            content = (r.get("content") or "").strip().replace("\n", " ")
            if len(content) > 320:
                content = content[:317] + "..."
            score = r.get("score", 0.0)
            collection = (r.get("collection") or r.get("source_type") or "").strip()
            prefix = f"{collection}: " if collection else ""
            lines.append(f"  [{score:.2f}] {prefix}{title} — {content}")
        if not lines:
            return ""
        return "Brain recall (profile=" + self._profile + "):\n" + "\n".join(lines)

    # ── Sync turn (background write) ─────────────────────────────────────────

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not user_content and not assistant_content:
            return
        payload = {
            "content": self._render_turn(user_content, assistant_content),
            "category": "other",
            "agent": self._profile,
            "source": "hermes",
            "confidence": 0.5,  # Hermes turns are raw — brain promotes after cosine gate
            "reason": (
                "kind=session_turn " f"session={session_id or self._session_id} " f"platform={self._platform}"
            )[:300],
        }
        self._write_queue.put(payload)

    def _render_turn(self, user: str, assistant: str) -> str:
        u = (user or "").strip()
        a = (assistant or "").strip()
        if u and a:
            return f"User: {u}\nAssistant: {a}"
        return u or a

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None:
                return  # shutdown sentinel
            try:
                _brain_request(
                    "/memory", method="POST", body=item, timeout=WRITE_TIMEOUT_S, actor=self._profile
                )
            except Exception as exc:  # pragma: no cover
                log.debug("brain provider write failed: %s", exc)

    # ── Tool surface (deliberately empty) ────────────────────────────────────

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        # Brain MCP server (mcp_servers.brain in config.yaml) already exposes
        # 21 tools. No duplication here.
        return []

    # ── Built-in Hermes memory tool mirror ───────────────────────────────────

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if action not in {"add", "replace"} or not content.strip():
            return
        metadata = dict(metadata or {})
        session = metadata.get("session_id") or self._session_id
        payload = {
            "content": content.strip(),
            "category": "preference" if target == "user" else "other",
            "agent": self._profile,
            "source": "hermes",
            "confidence": 0.65,
            "reason": (
                "kind=builtin_memory_write "
                f"action={action} target={target} "
                f"session={session} platform={self._platform}"
            )[:300],
        }
        self._write_queue.put(payload)

    # ── Optional hook: end-of-session distillation ───────────────────────────

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        # Build one session-summary atom so brain has a coarse-grained record
        # even if individual sync_turn writes were sampled out.
        joined: list[str] = []
        for m in messages[-20:]:  # last 20 messages — cap for size
            role = m.get("role", "?")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if len(content) > 200:
                content = content[:197] + "..."
            joined.append(f"{role}: {content}")
        if not joined:
            return
        payload = {
            "content": "Hermes session ended.\n" + "\n".join(joined),
            "category": "other",
            "agent": self._profile,
            "source": "hermes",
            "confidence": 0.4,
            "reason": ("kind=session_summary " f"session={self._session_id} " f"platform={self._platform}")[
                :300
            ],
        }
        _brain_request("/memory", method="POST", body=payload, timeout=WRITE_TIMEOUT_S, actor=self._profile)


# Hermes plugin loader convention: top-level class exposed at module level.
PROVIDER_CLASS = BrainMemoryProvider
