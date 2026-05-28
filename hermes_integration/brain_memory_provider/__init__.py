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
        is_constraint_query = self._is_constraint_query(query)

        # Status/usage/task questions are usually about live runtime state, not
        # durable memory. Broad RAG here caused Telegram-visible noise such as
        # old local-model or ACP memories. Let explicit Brain tools or task
        # tools answer these instead of injecting low-signal recall blocks.
        # Only a STRONG constraint signal (recommend/choose/preference/no paid/
        # openclaw/hermes/추천/제약/…) overrides the suppression — weak topic
        # words like 'workflow' paired with 'status'/'update' still leak ~2KB
        # of canonical preference blocks into Telegram if allowed through.
        if self._is_low_signal_status_query(query) and not self._is_strong_constraint_query(query):
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

    def _filter_relevant_results(
        self,
        results: list[dict[str, Any]],
        *,
        query: str,
        require_relevance: bool,
        is_constraint_query: bool = False,
    ) -> list[dict[str, Any]]:
        if not require_relevance:
            return results
        filtered: list[dict[str, Any]] = []
        rejected_noise = False
        for result in results:
            if self._is_generic_brain_infra_noise(result, query):
                rejected_noise = True
                continue
            if self._is_brain_quality_query(query) and self._is_brain_quality_noise(result, query):
                rejected_noise = True
                continue
            if self._topical_overlap(result, query) >= _MIN_OVERLAP_NON_CONSTRAINT or (
                is_constraint_query and self._is_hard_constraint_hit(result, query)
            ):
                filtered.append(result)
        if rejected_noise and self._is_brain_quality_query(query):
            return filtered
        # Do not turn ordinary prefetch into an empty string just because a
        # terse prompt shares no literal terms with the returned memory.
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
        if hard_constraint:
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
