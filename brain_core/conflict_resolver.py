"""Deterministic contradiction resolution policy.

This module is intentionally pure and cheap. It does not call an LLM, does not
schedule work, and does not mutate storage. Existing contradiction queues and
lifecycle jobs call it so there is one policy surface instead of another
parallel resolver.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

ResolutionAction = Literal["keep_new", "keep_old", "dismiss", "merge", "needs_review"]


@dataclass(frozen=True)
class ConflictRecommendation:
    action: ResolutionAction
    reason: str
    confidence: float
    review_required: bool
    auto_apply: bool
    old_authority: float
    new_authority: float
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TIER_AUTHORITY = {
    "core": 0.45,
    "canonical": 0.40,
    "semantic": 0.30,
    "episodic": 0.10,
    "raw": 0.05,
    "obsolete": -0.60,
}
_KIND_AUTHORITY = {
    "decision": 0.12,
    "preference": 0.10,
    "correction": 0.10,
    "fact": 0.06,
    "entity": 0.04,
    "other": 0.0,
}
_NEGATIVE_REVIEW_STATES = {"deleted", "rejected", "obsolete", "superseded"}
_POSITIVE_REVIEW_STATES = {"approved", "verified", "reviewed", "promoted"}


def _float(meta: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(meta.get(key, default))
    except (TypeError, ValueError):
        return default


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_expired(meta: dict[str, Any], now: datetime) -> bool:
    valid_until = str(meta.get("valid_until") or "").strip()
    if not valid_until:
        return False
    parsed = _parse_dt(valid_until)
    return bool(parsed and parsed <= now)


def _is_inactive(meta: dict[str, Any], now: datetime) -> bool:
    review_state = str(meta.get("review_state") or "").lower()
    tier = str(meta.get("tier") or "").lower()
    return bool(
        str(meta.get("superseded_by") or "").strip()
        or tier == "obsolete"
        or review_state in _NEGATIVE_REVIEW_STATES
        or _is_expired(meta, now)
    )


def authority_score(
    meta: dict[str, Any] | None, *, now: datetime | None = None
) -> tuple[float, dict[str, Any]]:
    """Return a bounded authority score and the signals that produced it."""
    now = now or datetime.now(UTC)
    meta = meta or {}
    tier = str(meta.get("tier") or "episodic").lower()
    kind = str(meta.get("kind") or meta.get("category") or "other").lower()
    review_state = str(meta.get("review_state") or "").lower()

    confidence = _float(meta, "confidence", 0.5)
    trust_score = _float(meta, "trust_score", 0.5)
    score = 0.0
    score += _TIER_AUTHORITY.get(tier, 0.08)
    score += _KIND_AUTHORITY.get(kind, 0.0)
    score += max(0.0, min(1.0, confidence)) * 0.35
    score += max(0.0, min(1.0, trust_score)) * 0.20

    if bool(meta.get("canonical")):
        score += 0.15
    if review_state in _POSITIVE_REVIEW_STATES:
        score += 0.10
    if _is_inactive(meta, now):
        score -= 0.85

    created = _parse_dt(str(meta.get("created_at") or ""))
    age_days = None
    if created:
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)

    signals = {
        "tier": tier,
        "kind": kind,
        "confidence": round(confidence, 4),
        "trust_score": round(trust_score, 4),
        "canonical": bool(meta.get("canonical")),
        "review_state": review_state,
        "inactive": _is_inactive(meta, now),
        "age_days": round(age_days, 2) if age_days is not None else None,
    }
    return round(score, 4), signals


def recommend_resolution(
    contradiction_meta: dict[str, Any] | None,
    old_meta: dict[str, Any] | None,
    new_meta: dict[str, Any] | None,
    *,
    old_exists: bool = True,
    new_exists: bool = True,
    now: datetime | None = None,
    stale_days: int = 14,
) -> ConflictRecommendation:
    """Recommend one contradiction action without side effects."""
    now = now or datetime.now(UTC)
    contradiction_meta = contradiction_meta or {}
    old_meta = old_meta or {}
    new_meta = new_meta or {}
    old_authority, old_signals = authority_score(old_meta, now=now)
    new_authority, new_signals = authority_score(new_meta, now=now)

    try:
        distance = float(contradiction_meta.get("distance", 1.0))
    except (TypeError, ValueError):
        distance = 1.0
    try:
        token_overlap = float(contradiction_meta.get("token_overlap", 0.0))
    except (TypeError, ValueError):
        token_overlap = 0.0

    signals: dict[str, Any] = {
        "distance": round(distance, 4),
        "token_overlap": round(token_overlap, 4),
        "old": old_signals,
        "new": new_signals,
    }

    if not old_exists or not new_exists:
        missing = "old" if not old_exists else "new"
        return ConflictRecommendation(
            action="dismiss",
            reason=f"{missing} side is missing; contradiction record is stale",
            confidence=0.96,
            review_required=False,
            auto_apply=True,
            old_authority=old_authority,
            new_authority=new_authority,
            signals=signals,
        )

    old_inactive = _is_inactive(old_meta, now)
    new_inactive = _is_inactive(new_meta, now)
    if old_inactive != new_inactive:
        action: ResolutionAction = "keep_new" if old_inactive else "keep_old"
        return ConflictRecommendation(
            action=action,
            reason="one side is already inactive, expired, or superseded",
            confidence=0.94,
            review_required=False,
            auto_apply=True,
            old_authority=old_authority,
            new_authority=new_authority,
            signals=signals,
        )

    if distance < 0.05 and token_overlap >= 0.70:
        return ConflictRecommendation(
            action="keep_new",
            reason="near-duplicate rephrasing; newer memory replaces older wording",
            confidence=0.92,
            review_required=False,
            auto_apply=True,
            old_authority=old_authority,
            new_authority=new_authority,
            signals=signals,
        )

    authority_gap = new_authority - old_authority
    signals["authority_gap"] = round(authority_gap, 4)
    if abs(authority_gap) >= 0.25:
        action = "keep_new" if authority_gap > 0 else "keep_old"
        return ConflictRecommendation(
            action=action,
            reason="authority gap favors one side",
            confidence=min(0.93, round(0.70 + abs(authority_gap) / 2, 4)),
            review_required=False,
            auto_apply=True,
            old_authority=old_authority,
            new_authority=new_authority,
            signals=signals,
        )

    old_conf = _float(old_meta, "confidence", 0.5)
    new_conf = _float(new_meta, "confidence", 0.5)
    confidence_gap = new_conf - old_conf
    signals["confidence_gap"] = round(confidence_gap, 4)
    if abs(confidence_gap) > 0.20:
        action = "keep_new" if confidence_gap > 0 else "keep_old"
        return ConflictRecommendation(
            action=action,
            reason="confidence gap favors one side",
            confidence=min(0.90, round(0.68 + abs(confidence_gap), 4)),
            review_required=False,
            auto_apply=True,
            old_authority=old_authority,
            new_authority=new_authority,
            signals=signals,
        )

    created_at = _parse_dt(str(contradiction_meta.get("created_at") or ""))
    if created_at and created_at < now - timedelta(days=stale_days):
        return ConflictRecommendation(
            action="keep_new",
            reason=f"unreviewed contradiction is older than {stale_days} days",
            confidence=0.72,
            review_required=False,
            auto_apply=True,
            old_authority=old_authority,
            new_authority=new_authority,
            signals=signals,
        )

    return ConflictRecommendation(
        action="needs_review",
        reason="no deterministic authority, confidence, duplicate, or staleness signal is decisive",
        confidence=0.55,
        review_required=True,
        auto_apply=False,
        old_authority=old_authority,
        new_authority=new_authority,
        signals=signals,
    )
