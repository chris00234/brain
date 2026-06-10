"""Mode-aware recall policy for the recall-governance layer.

A single declarative description of how strict each recall surface should be,
so the provider prefetch path (and any future caller) applies a shared policy
object instead of hand-maintained regex gating. Provider prefetch is the
strictest surface: empty is better than wrong injected context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["interactive", "active", "provider_prefetch", "raw"]
FalsePositiveBias = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RecallPolicy:
    mode: Mode
    allow_semantic: bool
    allow_route_guarantees: bool
    allow_low_authority: bool
    max_results: int
    min_topical_overlap: int
    false_positive_bias: FalsePositiveBias


_POLICIES: dict[str, RecallPolicy] = {
    # Interactive /recall/v2: relevant low-authority evidence may follow direct
    # truth; user is in the loop and can ignore noise.
    "interactive": RecallPolicy(
        mode="interactive",
        allow_semantic=True,
        allow_route_guarantees=True,
        allow_low_authority=True,
        max_results=10,
        min_topical_overlap=1,
        false_positive_bias="low",
    ),
    # /recall/active: stricter than interactive — injected per turn, so prefer
    # fewer false positives.
    "active": RecallPolicy(
        mode="active",
        allow_semantic=True,
        allow_route_guarantees=True,
        allow_low_authority=False,
        max_results=5,
        min_topical_overlap=1,
        false_positive_bias="medium",
    ),
    # Provider prefetch: strictest. Goes straight into a system prompt, so empty
    # is acceptable; never inject low-authority session/reflection/procedure rows
    # unless summary intent is explicit.
    "provider_prefetch": RecallPolicy(
        mode="provider_prefetch",
        allow_semantic=True,
        allow_route_guarantees=True,
        allow_low_authority=False,
        max_results=5,
        min_topical_overlap=1,
        false_positive_bias="high",
    ),
    # Deprecated raw recall: no governance.
    "raw": RecallPolicy(
        mode="raw",
        allow_semantic=True,
        allow_route_guarantees=False,
        allow_low_authority=True,
        max_results=10,
        min_topical_overlap=0,
        false_positive_bias="low",
    ),
}


def policy_for(mode: str) -> RecallPolicy:
    """Return the :class:`RecallPolicy` for ``mode`` (defaults to interactive)."""
    return _POLICIES.get(mode, _POLICIES["interactive"])
