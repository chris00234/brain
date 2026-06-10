"""First-class durable route guarantees for the recall-governance layer.

Loads ``brain_core/route_guarantees.yaml`` and exposes token-boundary-safe
matching plus the declarative-guarantee shape test. Route *facts* (durable
policy statements injectable as standalone memory blocks) are kept separate
from *search variants* (query strings used only to retrieve evidence).

Fail-open everywhere: a missing/invalid YAML file yields zero guarantees, never
an exception, so the active-recall and provider paths degrade to empty rather
than breaking the user prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyYAML ships in the brain venv; degrade to empty if unavailable.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from .normalization import has_unnegated_match, occurrence_is_negated
from .source_authority import AuthorityTier

_ROUTE_GUARANTEES_PATH = Path(__file__).resolve().parent.parent / "route_guarantees.yaml"

_AUTHORITY_BY_NAME = {
    "direct_current_truth": AuthorityTier.DIRECT_CURRENT_TRUTH,
    "curated_canonical": AuthorityTier.CURATED_CANONICAL,
}

# Subject/ownership + policy/modal/constraint cue words — a route's curated
# guarantee text is a declarative policy statement only if it carries a subject
# AND a policy cue (and is long enough). Generic shape test, not a marker list.
_ROUTE_GUARANTEE_SUBJECT_CUES = ("chris", "user", "users", "팀", "사용자")
_ROUTE_GUARANTEE_POLICY_CUES = (
    "prefer",
    "prefers",
    "requires",
    "require",
    "wants",
    "should",
    "must",
    "avoid",
    "no ",
    "without",
    "선호",
    "필수",
    "요구",
    "하지",
    "없이",
)

_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+|[가-힣]+")


@dataclass(frozen=True)
class RouteGuarantee:
    route: str
    id: str
    authority: AuthorityTier
    status: str
    text: str
    search_variants: tuple[str, ...] = field(default_factory=tuple)


_cache: dict | None = None
_cache_mtime: float = -1.0


def _load_raw() -> dict:
    """Load + cache route_guarantees.yaml by mtime. Fail-open to ``{}``."""
    global _cache, _cache_mtime
    if yaml is None:
        return {}
    try:
        mtime = _ROUTE_GUARANTEES_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache is not None and mtime <= _cache_mtime:
        return _cache
    try:
        with _ROUTE_GUARANTEES_PATH.open() as f:
            _cache = yaml.safe_load(f) or {}
        _cache_mtime = mtime
    except Exception:
        if _cache is None:
            _cache = {}
    return _cache


def _keyword_matches(keyword: str, *, lowered: str) -> bool:
    """Token-boundary match for Latin keywords (so ``ui`` does not fire inside
    ``quality``/``TUI``); substring match for non-ASCII (Korean) keywords. A
    keyword occurrence inside an explicit negation scope ("not about codex",
    "코덱스 말고") does NOT count — routes require positive evidence, never
    negated keyword residue (see normalization.occurrence_is_negated)."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if not kw.isascii():
        idx = lowered.find(kw)
        while idx != -1:
            if not occurrence_is_negated(lowered, idx, idx + len(kw)):
                return True
            idx = lowered.find(kw, idx + 1)
        return False
    escaped = re.escape(kw).replace(r"\ ", r"\s+")
    pattern = re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return has_unnegated_match(pattern, lowered)


def _route_matches(match_cfg: dict, lowered: str) -> bool:
    if not isinstance(match_cfg, dict):
        return False
    any_tokens = match_cfg.get("any_tokens") or []
    all_any_groups = match_cfg.get("all_any_groups") or []
    support_any = match_cfg.get("support_any") or []

    if any_tokens and not any(_keyword_matches(t, lowered=lowered) for t in any_tokens):
        return False
    for group in all_any_groups:
        if not any(_keyword_matches(t, lowered=lowered) for t in (group or [])):
            return False
    # A route with neither primary selector never matches.
    if not any_tokens and not all_any_groups:
        return False
    return not support_any or any(_keyword_matches(t, lowered=lowered) for t in support_any)


def match_route_guarantees(text: str) -> list[RouteGuarantee]:
    """Return durable guarantee facts for every route whose match rule fires.

    Token-boundary safe; fail-open to an empty list on any load/parse error.
    """
    raw = _load_raw()
    routes = (raw or {}).get("routes") or {}
    if not routes or not text:
        return []
    lowered = text.lower()
    out: list[RouteGuarantee] = []
    for route_name, cfg in routes.items():
        if not isinstance(cfg, dict):
            continue
        try:
            if not _route_matches(cfg.get("match") or {}, lowered):
                continue
            variants = tuple(str(v) for v in (cfg.get("search_variants") or []))
            for fact in cfg.get("guarantee_facts") or []:
                if not isinstance(fact, dict):
                    continue
                fact_text = " ".join(str(fact.get("text") or "").split())
                if not fact_text:
                    continue
                out.append(
                    RouteGuarantee(
                        route=str(route_name),
                        id=str(fact.get("id") or route_name),
                        authority=_AUTHORITY_BY_NAME.get(
                            str(fact.get("authority") or "").lower(), AuthorityTier.DIRECT_CURRENT_TRUTH
                        ),
                        status=str(fact.get("status") or "current"),
                        text=fact_text,
                        search_variants=variants,
                    )
                )
        except Exception:  # noqa: S112 — skip malformed route entry, fail-open
            continue
    return out


def matched_route_tags(text: str) -> set[str]:
    """Route names whose match rule fires for ``text`` (best-effort)."""
    try:
        return {g.route for g in match_route_guarantees(text)}
    except Exception:
        return set()


def is_declarative_route_guarantee(text: str) -> bool:
    """True when a string is a declarative policy/preference statement worth
    surfacing as a standalone memory block — not a short keyword search probe.

    Generic shape test: length + subject/ownership cue + policy/modal/constraint
    cue. No task-specific marker list.
    """
    lower = (text or "").lower()
    if len(re.findall(r"[a-z0-9가-힣]+", lower)) < 5:
        return False
    has_subject = any(cue in lower for cue in _ROUTE_GUARANTEE_SUBJECT_CUES)
    has_policy = any(cue in lower for cue in _ROUTE_GUARANTEE_POLICY_CUES)
    return has_subject and has_policy


# Function words + subject/ownership + policy/modal cues carry no DISTINCTIVE
# route meaning. Stripping them leaves only the discriminating vocabulary
# (current/historical/headless/bounded/tmux/tui/runtime/distinction/…), so a
# row "serves" a guarantee only when it shares the distinctive terms — not when
# it merely overlaps on common function words.
_GUARANTEE_TOKEN_STOPWORDS = frozenset(
    {
        "chris",
        "user",
        "users",
        "team",
        "his",
        "her",
        "their",
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "of",
        "to",
        "as",
        "is",
        "are",
        "do",
        "not",
        "no",
        "when",
        "only",
        "through",
        "with",
        "in",
        "on",
        "by",
        "via",
        "that",
        "this",
        "it",
        "be",
        "if",
        "use",
        "using",
        "used",
        "prefer",
        "prefers",
        "preferred",
        "preference",
        "wants",
        "want",
        "should",
        "must",
        "avoid",
        "without",
        "over",
        "new",
        "선호",
        "필수",
        "요구",
        "하지",
        "없이",
    }
)


def guarantee_tokens(guarantee: RouteGuarantee) -> set[str]:
    """Distinctive vocabulary of a guarantee (route name + fact text), minus
    generic function/subject/policy words, used to decide whether an existing
    retrieved row already states the route's durable fact."""
    text = " ".join([guarantee.route.replace("_", " "), guarantee.text])
    toks = {t for t in _WORD_TOKEN_RE.findall(text.lower()) if len(t) > 1}
    return toks - _GUARANTEE_TOKEN_STOPWORDS
