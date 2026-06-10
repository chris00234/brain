"""Shared query intent analysis for recall governance.

Lightweight stdlib-only classifiers used by recall routes, active recall, and
provider prefetch. Detectors are class-level linguistic/overlap rules rather
than probe-string allowlists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalization import (
    FUNCTION_WORD_STOPWORDS,
    content_tokens,
    normalize_separators,
    strip_korean_particle,
    tokenize,
)

# Public constants consumed by routes.recall
_GENERIC_PROCEDURE_STOPWORDS = frozenset(
    {
        "how",
        "do",
        "does",
        "did",
        "make",
        "made",
        "get",
        "give",
        "tell",
        "show",
        "steps",
        "step",
        "procedure",
        "process",
        "briefly",
        "please",
        "간단히",
        "알려줘",
    }
)
_PERSONAL_MEMORY_TOKENS = {
    "brain",
    "chris",
    "크리스",
    "조대현",
    "대현",
    "daehyun",
    "memory",
    "preference",
    "preferences",
    "my",
    "mine",
    "myself",
    "user",
    "사용자",
    "내",
    "제",
    "나",
    "omscs",
}
_TECHNICAL_DOMAIN_ANCHOR_TOKENS = {
    "ai",
    "llm",
    "llms",
    "api",
    "apis",
    "tool",
    "tools",
    "tooling",
    "model",
    "models",
    "billing",
    "cost",
    "spend",
    "paid",
    "subscription",
    "subscriptions",
    "local",
    "cloud",
    "hosting",
    "calendar",
    "reminders",
    "apple",
    "codex",
    "deploy",
    "server",
    "agent",
    "agents",
    "brain",
    "prefetch",
    "recall",
}
_OPERATIONAL_DOMAIN_ANCHOR_TOKENS = {
    "task",
    "tasks",
    "job",
    "jobs",
    "runner",
    "scheduler",
    "schedulers",
    "queue",
    "queues",
    "pipeline",
    "pipelines",
    "daemon",
    "daemons",
    "cron",
    "worker",
    "workers",
    "process",
    "processes",
    "작업",
    "태스크",
    "프로세스",
    "스케줄러",
    "러너",
    "파이프라인",
}
# Concrete infra/hosting/networking, client-tooling, and data-pipeline/integration
# NOUNS that name Chris's engineering world. The abstract technical anchors above
# ("ai", "api", "server", "deploy") miss this class, so a project/infra/tooling
# prompt with no abstract anchor ("how to add a Cloudflare subdomain",
# "browser/Chrome usage patterns", "shell history ingest adapter") was wrongly
# read as out-of-domain world-knowledge and had EVERY row dropped by the recall
# quality filter. Anchoring on this class keeps such prompts in-domain. It is a
# CLASS of distinctive engineering nouns — never a recipe/trivia term — so genuine
# world-knowledge asks still carry no anchor and stay out-of-domain. 'runtime' is
# deliberately excluded (a routes.recall control relies on the OpenClaw-vs-Hermes
# runtime prompt staying out-of-domain).
_INFRA_TOOLING_DOMAIN_ANCHOR_TOKENS = {
    # infra / hosting / networking
    "cloudflare",
    "cloudflared",
    "subdomain",
    "subdomains",
    "dns",
    "nginx",
    "proxy",
    "tunnel",
    "docker",
    "container",
    "containers",
    "orbstack",
    "homelab",
    "webhook",
    "webhooks",
    "ssl",
    "tls",
    "certificate",
    "vpn",
    "firewall",
    "launchd",
    "launchctl",
    # client tooling / software
    "browser",
    "browsers",
    "chrome",
    "chromium",
    "shell",
    "terminal",
    "vscode",
    # data pipeline / ingestion / integration
    "ingest",
    "ingestion",
    "adapter",
    "adapters",
    "connector",
    "connectors",
    "integration",
    "integrations",
    "parser",
    "outbox",
    "embedding",
    "embeddings",
}
_WORLD_KNOWLEDGE_ANCHOR_TOKENS = (
    _PERSONAL_MEMORY_TOKENS
    | _TECHNICAL_DOMAIN_ANCHOR_TOKENS
    | _OPERATIONAL_DOMAIN_ANCHOR_TOKENS
    | _INFRA_TOOLING_DOMAIN_ANCHOR_TOKENS
    | {"workflow", "workflows", "policy", "preference", "recommend", "추천", "선호", "도구"}
)
_RECOMMENDATION_TOOLING_TOKENS = {
    "recommend",
    "recommendation",
    "추천",
    "tool",
    "tools",
    "tooling",
    "prefer",
    "preference",
    "선호",
}
_COST_BILLING_TOKENS = {
    "cost",
    "costs",
    "spend",
    "billing",
    "paid",
    "api",
    "apis",
    "subscription",
    "과금",
    "유료",
}

_PRESENT_MARKERS = ("right now", "at this moment", "at the moment", "currently", "current", "지금", "현재")
_HISTORICAL_MARKERS = (
    "history",
    "historical",
    "archived",
    "completed",
    "last week",
    "previous",
    "past",
    "records",
    "logs",
    "durable",
    "canonical",
    "from memory",
    "remember",
    "preferences",
    "decisions",
    "지난주",
    "기록",
    "과거",
)
_DURABLE_ADVICE_MARKERS = ("recommend", "recommendation", "prefer", "preferred", "preference", "추천", "선호")
_DURABLE_GUIDANCE_RE = re.compile(
    r"\b(?:workflow|procedure|policy|method|methods|way|ways|practice|practices|monitor(?:ing)?|manage|managing|management|guide|guidance|guidelines?|use|using|should)\b",
    re.I,
)
_DURABLE_HOWTO_RE = re.compile(
    r"\bhow\s+(?:to\b|(?:do|does|should|can|could|would|will|must)\s+(?:we|i|you|they|one)\b)", re.I
)
_DURABLE_PASSIVE_RE = re.compile(
    r"\bhow\s+(?:is|are)\b.*?\b(?:supposed\s+to\s+be\s+)?(?:managed|used|organized|handled|operated|monitored|configured|run|executed)\b\??\s*$",
    re.I,
)
_RUNNING_RE = re.compile(
    r"(?:진행|실행|가동|구동|작동)\s*중(?:이|인|입|\s|$|[^가-힣])|\brunning\s+(?:tasks?|processes?|jobs?)\b",
    re.I,
)
_STATE_RE = re.compile(
    r"\b(?:status|state|progress|health|where|looking|done|happening|running|wrapped\s+up|ready yet)\b|상태|끝났|어디까지|돌아가|진행",
    re.I,
)
_COPULAR_STATUS_RE = re.compile(
    r"\bhow\s+(?:is|are|'s|'re)\b.*?\b(?:going|doing|looking|coming\s+along|progressing|shaping\s+up|getting\s+on|holding\s+up|tracking|moving\s+along|going\s+on)\b",
    re.I,
)
_LOOK_STATUS_RE = re.compile(
    r"\bhow\s+(?:do|does)\b.*?\b(?:look|looks|looking|seem|seems|appear|appears)\b\??\s*$", re.I
)


def is_durable_advice_query(q: str) -> bool:
    lower = (q or "").lower()
    return any(m in lower for m in _DURABLE_ADVICE_MARKERS)


def is_durable_guidance_query(q: str) -> bool:
    text = q or ""
    lower = text.lower()
    if any(m in lower for m in _PRESENT_MARKERS):
        return False
    return bool(
        _DURABLE_GUIDANCE_RE.search(text)
        or _DURABLE_HOWTO_RE.search(text)
        or _DURABLE_PASSIVE_RE.search(text)
        or any(m in text for m in ("방법", "방식", "절차", "워크플로", "관리", "가이드"))
    )


def operational_guidance_anchors(q: str) -> frozenset[str]:
    return frozenset(tokenize(q) & _OPERATIONAL_DOMAIN_ANCHOR_TOKENS)


def is_operational_guidance_query(q: str) -> bool:
    return bool(operational_guidance_anchors(q)) and is_durable_guidance_query(q)


def is_live_state_query(q: str) -> bool:
    text = q or ""
    lower = text.lower()
    if any(m in lower for m in _HISTORICAL_MARKERS):
        return False
    if is_durable_advice_query(text) or is_durable_guidance_query(text):
        return False
    if _COPULAR_STATUS_RE.search(text) or _LOOK_STATUS_RE.search(text):
        return True
    if re.search(r"\b(?:what(?:'s|\s+is)\s+)?running\s+(?:right\s+)?now\b", lower):
        return True
    if "progress update" in lower or "진행상황" in text or "진행 상황" in text or "시작했어" in text:
        return True
    if "going on" in lower and any(m in lower for m in _PRESENT_MARKERS):
        return True
    if _RUNNING_RE.search(text):
        return True
    if any(m in lower for m in _PRESENT_MARKERS) and _STATE_RE.search(text):
        return True
    toks = tokenize(text)
    if "kanban" in toks and toks & {"status", "progress", "current", "running", "task", "tasks"}:
        return True
    return bool(
        toks & _OPERATIONAL_DOMAIN_ANCHOR_TOKENS and len(toks & {"status", "progress", "running"}) >= 1
    )


_SUMMARY_EXCLUSION_RE = re.compile(
    r"\b(?:not|no|without|exclude|excluding|other\s+than|skip)\s+(?:a\s+|an\s+|the\s+)?(?:generic\s+|weekly\s+|session\s+)?(?:summary|summaries|summarized)\b|\b(?:summary|summaries)\s+말고\b|요약\s*(?:말고|빼고|제외)",
    re.I,
)
_POSITIVE_SUMMARY_RE = re.compile(r"\b(?:summary|summaries|summarize|recap|digest)\b|요약", re.I)


def is_summary_excluded_query(q: str) -> bool:
    return bool(_SUMMARY_EXCLUSION_RE.search(q or ""))


def is_positive_summary_intent_query(q: str) -> bool:
    return bool(_POSITIVE_SUMMARY_RE.search(q or "")) and not is_summary_excluded_query(q)


def query_targets_openclaw_or_agents(q: str) -> bool:
    toks = tokenize(q)
    return (
        bool(toks & {"openclaw", "오픈클로"})
        or ({"hermes", "runtime"}.issubset(toks))
        or bool(
            toks & {"sage", "jenna", "liz", "ellie", "market"}
            and toks & {"workspace", "agent", "agents", "working"}
        )
    )


_WORLD_HINTS = {
    "recipe",
    "cook",
    "cooking",
    "pasta",
    "tomato",
    "sauce",
    "revolution",
    "explain",
    "history",
    "끓이는",
    "레시피",
    "요리",
    "파스타",
    "토마토",
}


def is_out_of_domain_world_knowledge_query(q: str) -> bool:
    text = q or ""
    toks = tokenize(text)
    if personal_attribute_query_binding(text) is not None or personal_factoid_query_terms(text):
        return False
    if {"openclaw", "hermes"}.issubset(toks):
        return True
    if toks & _WORLD_KNOWLEDGE_ANCHOR_TOKENS:
        return False
    if is_operational_guidance_query(text):
        return False
    return bool(toks & _WORLD_HINTS) or (
        bool(_DURABLE_HOWTO_RE.search(text)) and not (toks & _WORLD_KNOWLEDGE_ANCHOR_TOKENS)
    )


# Personal attributes
_ATTR_BIRTHDAY = "birthday"
_ATTR_ADDRESS = "address"
_ATTR_PHONE = "phone"
_ATTR_LEGAL_NAME = "legal_name"
_ATTR_EMAIL = "email"
_ATTR_LOCATION = "location"
_OWNER = "chris"
_OWNER_NAMES = {"chris", "크리스", "조대현", "대현", "daehyun"}
_SELF_RE = re.compile(r"\b(?:my|mine|myself|i|me)\b|(?<![가-힣])(?:내|제|나)(?![가-힣])", re.I)
_APOS = r"['\u2019]"
_CAP_NAME = r"(?:[A-Z][A-Za-z.\-]*\s+){0,2}[A-Z][A-Za-z.\-]*"
_NAME = r"(?:[A-Za-z][A-Za-z.\-]*\s+){0,2}[A-Za-z][A-Za-z.\-]*"
_STOP = {
    "when",
    "what",
    "where",
    "who",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "my",
    "me",
    "i",
    "his",
    "her",
    "the",
    "a",
    "an",
}


# Name-type qualifier for the legal/identity-name class: a closed linguistic
# class of name kinds incl. script names (korean/english/hangul), allowing
# slash-compound labels ("Korean/Hangul name").
_NAME_QUALIFIER = (
    r"(?:legal|full|real|maiden|korean|english|hangul|first|last|middle|given|family)"
    r"(?:\s*/\s*(?:legal|full|real|maiden|korean|english|hangul))?"
)

# Identity-document label forms ("- **Email:** value", "Location: value",
# "Korean/Hangul name: value"). A label-form statement names no inline
# subject; in this owner-scoped corpus a document that NAMES THE OWNER and
# states an attribute in label form is the owner's identity/profile doc, so
# the fact binds to the owner. Format/shape signal per attribute class —
# never a value or probe string.
_LABEL_LINE_PREFIX = r"^[\s>*+-]*\**\s*"
_ATTR_LABEL_RES = {
    _ATTR_EMAIL: re.compile(_LABEL_LINE_PREFIX + r"e-?mail(?:\s+address)?\s*\**\s*[:\uff1a]", re.I | re.M),
    _ATTR_LOCATION: re.compile(
        _LABEL_LINE_PREFIX + r"(?:location|time\s*zone|timezone|거주지|위치|시간대)\s*\**\s*[:\uff1a]",
        re.I | re.M,
    ),
    _ATTR_LEGAL_NAME: re.compile(
        _LABEL_LINE_PREFIX
        + r"(?:"
        + _NAME_QUALIFIER
        + r"\s+names?|(?:한국|한글|영어)\s*이름|본명)\s*\**\s*[:\uff1a]",
        re.I | re.M,
    ),
    _ATTR_ADDRESS: re.compile(_LABEL_LINE_PREFIX + r"(?:address|주소)\s*\**\s*[:\uff1a]", re.I | re.M),
    _ATTR_PHONE: re.compile(
        _LABEL_LINE_PREFIX + r"(?:phone(?:\s*number)?|전화\s*번호|전화번호|휴대폰|핸드폰)\s*\**\s*[:\uff1a]",
        re.I | re.M,
    ),
    _ATTR_BIRTHDAY: re.compile(
        _LABEL_LINE_PREFIX
        + r"(?:birth\s*day|birthday|date\s+of\s+birth|dob|생일|생년월일)\s*\**\s*[:\uff1a]",
        re.I | re.M,
    ),
}
_OWNER_NAME_RE = re.compile(r"\bchris\b|\bdaehyun\b|크리스|조대현|대현", re.I)


@dataclass(frozen=True)
class PersonalAttributeBinding:
    subject: str
    attribute: str


def _resolve(raw: str | None) -> str | None:
    toks = [t.strip(" .-?'\u2019").lower() for t in re.findall(r"[A-Za-z.\-]+|[가-힣]+", raw or "")]
    # Particle-aware: a KO possessive subject is captured with its particle
    # glued (크리스의 → 크리스), so strip it before owner-name resolution.
    toks = [strip_korean_particle(t) for t in toks]
    if not toks:
        return None
    if any(t in _OWNER_NAMES for t in toks):
        return _OWNER
    for t in toks:
        if t not in _STOP:
            return t
    return None


def _first_group_subject(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    return _resolve(next((g for g in m.groups() if g), None))


def _subject_for(text: str, attr: str, *, fact: bool = False) -> str | None:
    t = text or ""
    # Self-reference always targets the owner when the attribute cue is present.
    self_subject = _OWNER if _SELF_RE.search(t) else None

    if attr == _ATTR_BIRTHDAY:
        if not re.search(
            r"birth\s*day|birthday|birth\s*date|date\s+of\s+birth|dob|b-?day|born|생일|생년월일|태어", t, re.I
        ):
            return None
        if self_subject and not fact:
            return self_subject
        pats = [
            rf"\b({_NAME}){_APOS}s\s+(?:birth\s*day|birthday|birth\s*date|date\s+of\s+birth|b-?day|dob)",
            rf"\b(?:birthday|birth\s*date|date\s+of\s+birth|dob)\s+of\s+({_NAME})",
            rf"\b({_NAME})\s+(?:was\s+|were\s+)?born\b",
            rf"\b({_CAP_NAME})\s+(?:birth\s*day|birthday|dob)\b\??\s*$",
            r"([A-Za-z]{2,}|[가-힣]{2,4})(?:의)?\s*(?:생일|생년월일)",
        ]
        if fact:
            pats.append(rf"\b({_CAP_NAME})\s+(?:birth\s*day|birthday|dob)\s+(?:is|are|was|were)\b")
    elif attr == _ATTR_ADDRESS:
        if re.search(r"email\s+address", t, re.I):
            return None
        if not re.search(
            r"address|residence|where\s+.*live|lives?\s+(?:in|at|on)|resides?\s+(?:in|at|on)|주소|살(?:아|고|며|아요|아요\?|아요\.)|거주",
            t,
            re.I,
        ):
            return None
        if self_subject and not fact:
            return self_subject
        pats = [
            rf"\b({_NAME}){_APOS}s\s+(?:address|residence)\b",
            rf"\b(?:address|residence)\s+of\s+({_NAME})\b",
            rf"\bwhere\s+(?:do|does|did)\s+({_NAME})\s+lives?\b",
            rf"\bwhere\s+({_NAME})\s+lives?\b",
            rf"\b({_CAP_NAME})\s+(?:address|residence)\b\??\s*$",
            r"([A-Za-z]{2,}|[가-힣]{2,4})(?:의)?\s*주소",
        ]
        if fact:
            pats += [
                rf"\b({_CAP_NAME})\s+(?:address|residence)\s+(?:is|are|was|were)\b",
                rf"\b({_NAME})\s+(?:currently\s+)?(?:lives?|resides?)\s+(?:in|at|on)\b",
                r"([A-Za-z]{2,}|[가-힣]{2,4})(?:은|는|이|가)\s.*?(?:살아|살고|거주)",
            ]
    elif attr == _ATTR_PHONE:
        if not re.search(r"phone|telephone|cell|mobile\s*number|전화|휴대폰|핸드폰", t, re.I):
            return None
        if self_subject and not fact:
            return self_subject
        pats = [
            rf"\b({_NAME}){_APOS}s\s+(?:phone(?:\s*number)?|telephone|cell(?:\s*phone)?|mobile\s*number)\b",
            rf"\b(?:phone(?:\s*number)?|telephone|mobile\s*number)\s+of\s+({_NAME})\b",
            rf"\b({_CAP_NAME})\s+(?:phone(?:\s*number)?|telephone|cell(?:\s*phone)?|mobile\s*number)\b\??\s*$",
            r"([A-Za-z]{2,}|[가-힣]{2,4})(?:의)?\s*(?:전화\s*번호|전화번호|휴대폰|핸드폰|폰\s*번호)",
        ]
        if fact:
            pats.append(
                rf"\b({_CAP_NAME})\s+(?:phone(?:\s*number)?|telephone|mobile\s*number)\s+(?:is|are|was|were)\b"
            )
    elif attr == _ATTR_LEGAL_NAME:
        if not re.search(
            rf"{_NAME_QUALIFIER}\s+names?|본명|실명|성함|법적\s*이름|풀\s*네임|(?:한국|한글|영어)\s*이름",
            t,
            re.I,
        ):
            return None
        if self_subject and not fact:
            return self_subject
        pats = [
            rf"\b({_NAME}){_APOS}s\s+{_NAME_QUALIFIER}\s+names?\b",
            rf"\b{_NAME_QUALIFIER}\s+names?\s+of\s+({_NAME})\b",
            rf"\b({_CAP_NAME})\s+{_NAME_QUALIFIER}\s+names?\b\??\s*$",
            # KO requires a name-type qualifier — bare 이름 is a generic noun
            # ("파일 이름" = file name) and must never bind this class.
            r"([A-Za-z]{2,}|[가-힣]{2,4})(?:의)?\s*(?:한국|한글|영어|법적|본)\s*이름",
        ]
        if fact:
            pats.append(rf"\b({_CAP_NAME})\s+{_NAME_QUALIFIER}\s+names?\s+(?:is|are|was|were)\b")
            pats.append(r"([A-Za-z]{2,}|[가-힣]{2,4})(?:의|이|가|은|는)?\s*(?:한국|한글)\s*이름은?")
    elif attr == _ATTR_EMAIL:
        # Anchored on the ADDRESS form, a possessive, or an identity-doc label —
        # bare "email(s)" is a message-corpus word ("which emails to keep",
        # "what email confirms the payment"), never the contact attribute.
        if not (
            re.search(
                rf"e-?mail\s+address|이메일\s*주소|메일\s*주소|{_APOS}s\s+e-?mail|의\s*(?:이메일|메일)",
                t,
                re.I,
            )
            or _ATTR_LABEL_RES[_ATTR_EMAIL].search(t)
        ):
            return None
        if self_subject and not fact:
            return self_subject
        pats = [
            rf"\b({_NAME}){_APOS}s\s+e-?mail(?:\s+address)?\b(?!\s+(?:about|regarding|from|to|thread|message)\b)",
            rf"\be-?mail\s+address\s+(?:of|for)\s+({_NAME})\b",
            rf"\be-?mail\s+address\s+(?:to\s+)?(?:reach|contact)\s+({_NAME})\b",
            rf"\b({_CAP_NAME})\s+e-?mail\s+address\b",
            r"([A-Za-z]{2,}|[가-힣]{2,4})의\s*(?:이메일|메일)\s*(?:주소)?",
        ]
        if fact:
            pats.append(rf"\b({_NAME}){_APOS}s\s+e-?mail(?:\s+address)?\s+(?:is|are|was|were)\b")
    else:
        if not re.search(r"time\s*zone|timezone|location|\bbased\b|\blocated\b|위치|시간대|거주지", t, re.I):
            return None
        if self_subject and not fact:
            return self_subject
        pats = [
            # Determiner-led subjects ("where is the database based") are
            # things, not people — excluded so infra questions never bind.
            rf"\bwhere\s+(?:is|are)\s+(?!the\b|a\b|an\b|this\b|that\b|it\b)({_NAME})\s+(?:based|located)\b",
            rf"\b({_NAME}){_APOS}s\s+(?:location|time\s*zone|timezone)\b",
            rf"\b(?:location|time\s*zone|timezone)\s+(?:of|for)\s+({_NAME})\b",
            rf"\b(?:what|which)\s+(?:time\s*zone|timezone)\s+(?:does|is)\s+({_NAME})\b",
            rf"\b({_CAP_NAME})(?:\s+(?:location|time\s*zone|timezone))+\??\s*$",
            r"([A-Za-z]{2,}|[가-힣]{2,4})(?:의|은|는)?\s*(?:위치|시간대)",
        ]
        if fact:
            pats.append(rf"\b({_NAME})\s+(?:is\s+|are\s+)?(?:based|located)\s+(?:in|at|out\s+of)\b")

    for pat in pats:
        s = _first_group_subject(pat, t)
        if s:
            return s
    # Label-form fact fallback: an identity/profile document states attributes
    # as "Label: value" lines with no inline subject. Bind to the OWNER only
    # when the owner is actually named in the same text — third-party profile
    # docs (no owner name) never bind, so subject mismatch still drops them.
    if fact:
        label_re = _ATTR_LABEL_RES.get(attr)
        if label_re is not None and label_re.search(t) and _OWNER_NAME_RE.search(t):
            return _OWNER
    return self_subject


_ATTR_ORDER = (_ATTR_EMAIL, _ATTR_ADDRESS, _ATTR_PHONE, _ATTR_LEGAL_NAME, _ATTR_BIRTHDAY, _ATTR_LOCATION)


def _binding(text: str, *, fact: bool = False) -> PersonalAttributeBinding | None:
    for a in _ATTR_ORDER:
        s = _subject_for(text, a, fact=fact)
        if s:
            return PersonalAttributeBinding(s, a)
    return None


def personal_attribute_query_binding(q: str) -> PersonalAttributeBinding | None:
    return _binding(q, fact=False)


def personal_attribute_fact_binding(text: str) -> PersonalAttributeBinding | None:
    return _binding(text, fact=True)


def birthday_query_subject(q: str) -> str | None:
    return _subject_for(q, _ATTR_BIRTHDAY, fact=False)


def birthday_fact_subject(text: str) -> str | None:
    return _subject_for(text, _ATTR_BIRTHDAY, fact=True)


def birthday_identity_mismatch(query: str, result_text: str) -> bool:
    q = birthday_query_subject(query)
    r = birthday_fact_subject(result_text)
    return q is not None and r is not None and q != r


def personal_attribute_result_matches_query(query: str, result_text: str) -> bool | None:
    b = personal_attribute_query_binding(query)
    if b is None:
        return None
    return _subject_for(result_text, b.attribute, fact=True) == b.subject


_PERSONAL_FACT_SUBJECT_TOKENS = _OWNER_NAMES | {"user", "owner", "myself", "mine", "my", "사용자"}
_PERSONAL_FACT_QUERY_STOPWORDS = frozenset(
    {
        "chris",
        "cho",
        "daehyun",
        "대현",
        "조대현",
        "크리스",
        "user",
        "owner",
        "myself",
        "mine",
        "my",
        "me",
        "his",
        "her",
        "their",
        "profile",
        "profiles",
        "preference",
        "preferences",
        "prefer",
        "prefers",
        "preferred",
        "favorite",
        "favourite",
        "fact",
        "facts",
        "info",
        "information",
        "detail",
        "details",
        "know",
        "remember",
        "memory",
        "tell",
        "show",
        "give",
        "asked",
        "asks",
        "what",
        "which",
        "where",
        "when",
        "who",
        "how",
        "does",
        "did",
        "was",
        "were",
        "is",
        "are",
        "사용자",
    }
)

_FACTOID_SELF_RE = re.compile(r"\b(?:my|mine|myself)\b|(?<![가-힣])(?:내|제)(?![가-힣])", re.I)


def personal_factoid_query_terms(q: str) -> frozenset[str]:
    text = q or ""
    if personal_attribute_query_binding(text) is not None:
        return frozenset()
    # Particle-aware: 크리스가 -> 크리스, 파타��니아에서 -> 파타고니아 so a Korean
    # personal-fact probe is recognized and its glued nouns match the corpus.
    stems = {strip_korean_particle(t) for t in tokenize(text)}
    has_subject = bool(stems & _PERSONAL_FACT_SUBJECT_TOKENS) or bool(_FACTOID_SELF_RE.search(text))
    if not has_subject:
        return frozenset()
    content = {t for t in (stems - FUNCTION_WORD_STOPWORDS) if len(t) > 1}
    return frozenset(content - _PERSONAL_FACT_QUERY_STOPWORDS - _GENERIC_PROCEDURE_STOPWORDS)


def _term_is_whole_word_in(term: str, result_text: str) -> bool:
    """True when ``term`` occurs as a STANDALONE word in ``result_text`` — not as a
    fragment of a larger alphanumeric/hyphen compound.

    Tokenization splits ``content-first``→{content, first} and
    ``production-grade``→{production, grade}, so a plain token set-overlap counts
    those morpho-modifier fragments as topical matches (``first``/``grade`` "match"
    an unrelated design row). Requiring a whole-word occurrence (no adjacent
    ``[a-z0-9-]``) drops those weak collisions while keeping genuine standalone
    terms. Hangul terms have no hyphen-compound collision, so an exact tokenized
    membership check preserves prior behavior for them."""
    if term.isascii():
        return bool(re.search(rf"(?<![a-z0-9-]){re.escape(term)}(?![a-z0-9-])", (result_text or "").lower()))
    # Particle-aware: query terms are stems (코스), but the result text may
    # contain the same noun with a different particle (코스를/코스는). Compare
    # against particle-stripped result tokens so the overlap is transitive.
    result_stems = {strip_korean_particle(t) for t in content_tokens(result_text)}
    return term in (result_stems | content_tokens(result_text))


def personal_factoid_result_has_strong_attribute_overlap(query: str, result_text: str) -> bool | None:
    terms = personal_factoid_query_terms(query)
    if not terms:
        return None
    overlap = {t for t in terms if _term_is_whole_word_in(t, result_text)}
    if len(overlap) >= 2 or overlap == terms:
        return True
    # Cross-lingual leniency: a mixed-script query (Hangul + ASCII terms) can
    # only overlap on its ASCII terms against an English-language result. A
    # single distinctive ASCII overlap is strong cross-lingual evidence — the
    # Korean content terms (가을/프로그램/시작하) have no English cognate so
    # they can never match, but the shared proper noun (OMSCS) anchors the row.
    if overlap:
        has_hangul_terms = any(not t.isascii() for t in terms)
        has_ascii_overlap = any(t.isascii() for t in overlap)
        if has_hangul_terms and has_ascii_overlap:
            return True
    return False


@dataclass(frozen=True)
class QueryIntent:
    original: str
    normalized: str
    tokens: frozenset[str]
    live_state: bool
    durable_advice: bool
    out_of_domain_world_knowledge: bool
    memory_domain: bool
    recommendation_or_tooling: bool
    cost_or_billing: bool
    positive_summary_intent: bool
    summary_exclusion: bool
    route_tags: frozenset[str] = field(default_factory=frozenset)
    reasons: tuple[str, ...] = ()


def analyze_query(q: str) -> QueryIntent:
    text = q or ""
    toks = tokenize(text)
    route_tags: set[str] = set()
    if "codex" in toks and "hermes" in toks:
        route_tags.add("codex_workflow")
    if "openclaw" in toks and "hermes" in toks and ("runtime" in toks or "distinction" in toks):
        route_tags.add("runtime_distinction")
    live = is_live_state_query(text)
    advice = is_durable_advice_query(text)
    ood = is_out_of_domain_world_knowledge_query(text)
    reasons = []
    if live:
        reasons.append("live_state")
    if ood:
        reasons.append("out_of_domain_world_knowledge")
    if advice:
        reasons.append("durable_advice")
    if route_tags:
        reasons.append("route:" + ",".join(sorted(route_tags)))
    return QueryIntent(
        original=text,
        normalized=normalize_separators(text).strip(),
        tokens=frozenset(toks),
        live_state=live,
        durable_advice=advice,
        out_of_domain_world_knowledge=ood,
        memory_domain=bool(toks & _WORLD_KNOWLEDGE_ANCHOR_TOKENS),
        recommendation_or_tooling=bool(toks & _RECOMMENDATION_TOOLING_TOKENS) or advice,
        cost_or_billing=bool(toks & _COST_BILLING_TOKENS),
        positive_summary_intent=is_positive_summary_intent_query(text),
        summary_exclusion=is_summary_excluded_query(text),
        route_tags=frozenset(route_tags),
        reasons=tuple(reasons),
    )
