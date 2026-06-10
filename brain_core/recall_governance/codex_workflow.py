"""Codex/Hermes TUI workflow classifiers for recall governance.

These helpers identify Codex workflow/preference prompts, Codex-via-Hermes TUI
preference rows, and stale Codex/Claude-Code skill-sync noise. Two classifier
families live here side by side because ``/recall/active``
(brain_core.active_recall) and ``/recall/v2`` (brain_core.routes.recall)
historically drifted; this extraction preserves both behaviors verbatim.

Known drift (intentionally NOT unified here — unification is a behavior
change, not an extraction):

- Prompt vs query gate: ``looks_like_codex_workflow_prompt`` is
  substring-based, accepts Korean "코덱스", and includes markers
  "코딩"/"복잡한"; ``is_codex_hermes_tui_query`` is token-based, requires a
  literal "codex" token, and includes "좋아" instead.
- Preference marker: ``is_codex_current_preference_result`` requires a
  prefers/preference/선호 marker; ``is_codex_hermes_tui_result`` does not.
- Skill-sync exemption: the active variant exempts via the preference result
  classifier over ``content[:800]``; the route variant exempts via the TUI
  result classifier plus preference markers over ``text[:1000]``, with bare
  "headless"/"interactive" tokens accepted as TUI evidence.
"""

from __future__ import annotations

from .normalization import tokenize
from .source_authority import result_metadata


def looks_like_codex_workflow_prompt(prompt: str) -> bool:
    """Prompt-shaped classifier for active-recall prehook gating."""
    lower = (prompt or "").lower()
    if "codex" not in lower and "코덱스" not in (prompt or ""):
        return False
    return any(
        marker in lower
        for marker in (
            "hermes",
            "tmux",
            "tui",
            "headless",
            "steering",
            "quality",
            "coding",
            "preference",
            "recommendation",
            "코딩",
            "복잡한",
            "어떻게",
        )
    )


def is_codex_current_preference_result(title: str, content: str, path: str | None) -> bool:
    """Active-recall result classifier: requires an explicit preference marker."""
    haystack = f"{title}\n{path or ''}\n{content[:800]}".lower()
    return (
        "codex" in haystack
        and "hermes" in haystack
        and any(
            marker in haystack
            for marker in (
                "tmux",
                "tui",
                "terminal-like",
                "terminal like",
                "interactive terminal",
                "headless codex",
            )
        )
        and any(marker in haystack for marker in ("prefers", "preference", "선호"))
    )


def is_codex_skill_sync_noise(title: str, content: str, path: str | None) -> bool:
    """Active-recall skill-sync noise gate, exempting current-preference rows."""
    haystack = f"{title}\n{path or ''}\n{content[:800]}".lower()
    if is_codex_current_preference_result(title, content, path):
        return False
    return (
        "codex/claude code skill" in haystack
        or "skill sync" in haystack
        or "skills/autonomous-ai-agents" in haystack
        or ("codex" in haystack and "claude code" in haystack and "skill" in haystack)
    )


def is_codex_hermes_tui_query(query_tokens: set[str]) -> bool:
    """Route query classifier: token-shaped, literal "codex" token required."""
    return "codex" in query_tokens and bool(
        query_tokens
        & {
            "hermes",
            "tmux",
            "tui",
            "headless",
            "steering",
            "quality",
            "coding",
            "preference",
            "recommendation",
            "어떻게",
            "좋아",
        }
    )


def is_codex_hermes_tui_result(result_tokens: set[str], text: str) -> bool:
    """Route result classifier: no preference marker required (known drift)."""
    lower = text.lower()
    return {"codex", "hermes"}.issubset(result_tokens) and (
        bool(result_tokens & {"tmux", "tui", "headless", "interactive"})
        or "terminal-like" in lower
        or "terminal like" in lower
    )


def is_codex_skill_sync_noise_result(result: dict, text: str) -> bool:
    """Route skill-sync noise gate, exempting TUI-preference rows."""
    lower = text.lower()
    title = str(result.get("title") or result_metadata(result).get("document_title") or "").lower()
    path = str(result.get("path") or result_metadata(result).get("source_path") or "").lower()
    haystack = f"{title}\n{path}\n{lower[:1000]}"
    if is_codex_hermes_tui_result(tokenize(haystack), haystack) and any(
        marker in haystack for marker in ("prefers", "preference", "선호")
    ):
        return False
    return (
        "codex/claude code skill" in haystack
        or "skills/autonomous-ai-agents" in haystack
        or "skill sync" in haystack
        or ("codex" in haystack and "claude code" in haystack and "skill" in haystack)
    )
