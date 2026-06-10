"""OpenClaw/Hermes distinction and noise classifiers for recall governance.

These helpers identify OpenClaw-vs-Hermes current/historical distinction
prompts, distinction-evidence rows, and the accepted noise around them (setup
docs, recall-tuning handoff rows, broad theme summaries). Two classifier
families live here side by side because ``/recall/active``
(brain_core.active_recall) and ``/recall/v2`` (brain_core.routes.recall)
historically drifted; this extraction preserves both behaviors verbatim.

Known drift (intentionally NOT unified here — unification is a behavior
change, not an extraction):

- Prompt vs query gate: ``looks_like_openclaw_hermes_distinction_prompt`` is
  substring-based (so "currently" matches the "current" marker);
  ``is_openclaw_hermes_distinction_query`` is token-based over the same
  marker set.
- Distinction result: the active ``is_openclaw_hermes_distinction_result`` is
  substring-based over ``title\\npath\\ncontent[:800]`` and accepts
  "distinguish"/"provenance" but needs a "current runtime" bigram; the route
  ``is_openclaw_hermes_distinction_token_result`` is token-based, accepts
  bare "current"/"runtime"/"historical"/"distinction" tokens plus the
  "hermes agent is"/"current runtime is hermes" phrases, and drops the
  "history" token that the route's own query gate accepts.
- Handoff noise: identical markers, different haystacks — the active variant
  scans ``title\\npath\\ncontent[:1500]``; the route variant also consults
  result metadata (``source_name``/``source_path``) and scans ``text[:1500]``.
- Surface-only classifiers: setup noise is route-only; broad-theme noise is
  active-only. The route-level aggregate (setup + handoff + live-state
  snapshot) stays in ``routes.recall`` because live-state snapshot detection
  is still a route-local helper.
"""

from __future__ import annotations

from .source_authority import result_metadata

# Task/test handoff markers shared verbatim by both surfaces today (the only
# classifier pair in this family with identical markers).
OPENCLAW_HERMES_HANDOFF_NOISE_MARKERS = (
    "work kanban task t_",
    "acceptance probe",
    "focused tests passed",
    "review-required handoff",
    "dirty patch",
    "verdict: partial",
    "generic regression",
    "generic_recipe_knowledge_gap",
    "spot check",
    "no setup/live_state",
)


def looks_like_openclaw_hermes_distinction_prompt(prompt: str) -> bool:
    """Prompt-shaped classifier for active-recall prehook gating."""
    lower = (prompt or "").lower()
    return (
        "openclaw" in lower
        and "hermes" in lower
        and any(marker in lower for marker in ("distinction", "historical", "history", "runtime", "current"))
    )


def is_openclaw_hermes_distinction_result(title: str, content: str, path: str | None) -> bool:
    """Active-recall distinction-evidence classifier (substring-shaped)."""
    haystack = f"{title}\n{path or ''}\n{content[:800]}".lower()
    return (
        "openclaw" in haystack
        and "hermes" in haystack
        and any(
            marker in haystack
            for marker in (
                "distinction",
                "distinguish",
                "historical",
                "provenance",
                "current runtime",
                "current runtime context",
                "hermes agent is",
            )
        )
    )


def is_openclaw_hermes_handoff_noise(title: str, content: str, path: str | None) -> bool:
    """Active-recall handoff-noise classifier (title/path/content haystack)."""
    haystack = f"{title}\n{path or ''}\n{content[:1500]}".lower()
    return any(marker in haystack for marker in OPENCLAW_HERMES_HANDOFF_NOISE_MARKERS)


def is_broad_openclaw_hermes_theme_noise(title: str, content: str) -> bool:
    """Active-recall gate for "common theme" digests, exempting distinction rows."""
    haystack = f"{title}\n{content[:800]}".lower()
    if is_openclaw_hermes_distinction_result(title, content, None):
        return False
    return "these notes share a common theme" in haystack or (
        "openclaw" in haystack and "hermes" in haystack and "common theme" in haystack
    )


def is_openclaw_hermes_distinction_query(query_tokens: set[str]) -> bool:
    """Route query classifier (token-shaped)."""
    return {"openclaw", "hermes"}.issubset(query_tokens) and bool(
        query_tokens & {"current", "runtime", "historical", "distinction", "history"}
    )


def is_openclaw_hermes_distinction_token_result(result_tokens: set[str], text: str) -> bool:
    """Route distinction-evidence classifier (token-shaped; known drift vs the
    active substring variant — see module docstring)."""
    lower = text.lower()
    return {"openclaw", "hermes"}.issubset(result_tokens) and (
        bool(result_tokens & {"current", "runtime", "historical", "distinction"})
        or "hermes agent is" in lower
        or "current runtime is hermes" in lower
    )


def is_openclaw_setup_noise_result(result: dict, text: str) -> bool:
    """Route classifier for OpenClaw setup-doc rows, exempting current-runtime rows."""
    lower = text.lower()
    meta = result_metadata(result)
    title = str(result.get("title") or meta.get("document_title") or "").lower()
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "").lower()
    return (
        "openclaw multi-agent setup documentation" in title
        or "/.openclaw/workspace-" in path
        or "openclaw-setup" in path
        or "sub-agent configuration" in lower
        or ("active hours for heartbeat" in lower and "openclaw" in lower)
        or ("openclaw setup" in lower and "current runtime is hermes" not in lower)
    )


def is_openclaw_hermes_handoff_noise_result(result: dict, text: str) -> bool:
    """True for task/test handoff rows about recall quality, not durable truth.

    These rows often quote the exact OpenClaw/Hermes acceptance probe plus
    nearby ``live_state``/setup text. They are useful run history, but they are
    meta-evidence about this tuning task rather than the current-runtime fact
    itself, so they should not receive the same distinction boost.
    """
    lower = text.lower()
    meta = result_metadata(result)
    source_hint = " ".join(
        str(part or "")
        for part in (
            result.get("title"),
            result.get("path"),
            meta.get("source_name"),
            meta.get("source_path"),
        )
    ).lower()
    marker_haystack = f"{source_hint}\n{lower[:1500]}"
    return any(marker in marker_haystack for marker in OPENCLAW_HERMES_HANDOFF_NOISE_MARKERS)
