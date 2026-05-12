"""Escalation routing policy for Brain notifications.

Chris-facing notifications should be scarce. Most "escalations" are still
LLM-handleable: debugging, analysis, retry planning, summaries, or routing to
another agent can be handled by subscription-backed Codex/Claude CLIs without
extra API spend. When evaluation finds a true blocker, send Chris an action
summary of what Brain did rather than an input-request alert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EscalationRoute:
    """Routing decision for an escalation candidate."""

    target: str  # "llm" | "human"
    reason: str

    @property
    def notify_human(self) -> bool:
        return self.target == "human"


_HUMAN_REQUIRED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "explicit_human_request",
        re.compile(
            r"\b("
            r"ask chris|tell chris|notify chris|relay to chris|chris must|"
            r"needs? chris|requires? chris|human approval|manual approval|"
            r"manual intervention|needs? manual|requires? human|ask user|"
            r"need(?:s)? user input|waiting for user"
            r")\b",
            re.I,
        ),
    ),
    (
        "missing_private_knowledge",
        re.compile(
            r"\b("
            r"unknown preference|preference unknown|missing context|missing information|"
            r"not enough context|do(?:es)? not have (?:the )?knowledge|"
            r"private knowledge|personal knowledge|need(?:s)? (?:the )?answer|"
            r"cannot know|can't know|outside (?:my|our|the llm'?s) knowledge"
            r")\b",
            re.I,
        ),
    ),
    (
        "credential_or_account_blocker",
        re.compile(
            r"\b("
            r"password|passcode|2fa|mfa|otp|one-time code|"
            r"credential|secret|api key|billing|payment|account owner|"
            r"(?:need|needs|require|requires|waiting for|blocked by|provide|enter).{0,40}"
            r"(?:login|log in|sign in|account access|token)"
            r")\b",
            re.I,
        ),
    ),
    (
        "irreversible_or_external_authority",
        re.compile(
            r"\b("
            r"irreversible|destructive|delete production|wipe|factory reset|"
            r"purchase|buy|cancel subscription|legal approval|medical|financial advice|"
            r"physical access|on-device|phone confirmation"
            r")\b",
            re.I,
        ),
    ),
)

_AGENT_ROUTING_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhandle it yourself if possible;?\s*", re.I),
    re.compile(r"\bnotify chris only for (?:a )?true human(?: blocker)?\.?", re.I),
    re.compile(r"\bdo not (?:alert|notify) chris if\b[^.]{0,240}\.", re.I),
)


def _candidate_text(title: str, content: str, metadata: dict[str, Any]) -> str:
    text = " ".join(str(part or "") for part in (title, content, metadata.get("reason", "")))
    for pattern in _AGENT_ROUTING_INSTRUCTION_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def classify_escalation(
    *,
    title: str = "",
    content: str = "",
    metadata: dict[str, Any] | None = None,
    default_target: str = "llm",
) -> EscalationRoute:
    """Classify whether an escalation needs Chris or can stay with LLM agents.

    This is deliberately local and deterministic: no local generation model and
    no paid API call. If callers need nuanced reasoning, they should first send
    the candidate to the subscription CLI LLM and only notify Chris if the LLM
    returns a concrete human blocker.
    """

    meta = metadata or {}
    if meta.get("requires_human") is True or meta.get("notify_chris") is True:
        return EscalationRoute("human", "metadata_requires_human")
    if str(meta.get("escalation_target", "")).lower() in {"human", "chris"}:
        return EscalationRoute("human", "metadata_escalation_target")

    text = _candidate_text(title, content, meta)
    # Knowledge-gap tasks are remediation work, not proof that Chris must be
    # interrupted. Even when the query mentions private domains such as email
    # or credentials, the first action is still agent-side source discovery
    # (mail/index/local config search) or a concise "source missing" finding.
    # Escalate only if metadata explicitly marks the task human-only above.
    if str(title).lower().startswith("knowledge gap:"):
        return EscalationRoute(default_target, "knowledge_gap_agent_remediation")

    for reason, pattern in _HUMAN_REQUIRED_PATTERNS:
        if pattern.search(text):
            return EscalationRoute("human", reason)

    return EscalationRoute(default_target, "llm_handleable")


def llm_review_prompt(source: str, body: str) -> str:
    """Build the subscription-LLM review prompt for an escalation candidate."""

    return (
        "Review this Brain escalation candidate.\n\n"
        "Policy:\n"
        "- Use subscription-backed Codex/Claude CLI reasoning only; do not use paid API billing.\n"
        "- Do not alert Chris if the issue is handleable by an LLM agent through reasoning, code review, "
        "debugging, retry planning, documentation lookup, or agent handoff.\n"
        "- Return HUMAN_NEEDED only if progress is blocked by missing private/current knowledge, credentials, "
        "account access, physical access, irreversible authority, or an explicit personal preference that "
        "is not in Brain memory; the queue will send an action summary, not an input-request alert.\n\n"
        f"Source: {source}\n"
        f"Candidate:\n{body[:4000]}\n\n"
        "Respond with exactly one of:\n"
        "HANDLEABLE: <one concise next action for the LLM/agent>\n"
        "HUMAN_NEEDED: <the specific missing knowledge/authority Chris must provide>"
    )


def llm_says_human_needed(text: str) -> bool:
    """True when the subscription LLM explicitly asks for human input."""

    return bool(re.search(r"^\s*HUMAN_NEEDED\s*:", text or "", re.I | re.M))
