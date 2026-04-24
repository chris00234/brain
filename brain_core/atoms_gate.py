"""brain_core/atoms_gate.py - 30-word atom discipline.

Phase 6 of Brain v1 plan. Enforces a soft/hard length cap on new atoms so the
truth layer stays composed of tight, semantically dense units. Long memories
(typical when copy-pasted into /memory) get re-distilled via Jenna at write
time so the search ranker has a sharper target.

Layered design:
  ok            (≤30 English words / ≤50 tokens Korean) → store unchanged
  warned        (31..50 words)                            → store, flag quality_score=0.7
  needs_redist  (>50 words)                               → re-distill, then store
                                                            (or store with quality 0.3 if redistill fails)

Free of cost: re-distill uses the existing Jenna OpenClaw dispatch.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.atoms_gate")

# Korean-aware word count: jamo blocks count as words too. We approximate by
# tokenizing on whitespace + Hangul syllable boundary. Real tokenizer would
# need mecab/kiwi but that's an extra dep we don't want.
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]+")
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


SOFT_CAP_WORDS = 30
HARD_CAP_WORDS = 50


# ── Prompt-injection / content-safety scanner ──────────────────────────
#
# Adopted from hermes-agent's `agent/prompt_builder.py` threat patterns.
# A poisoned atom in Qdrant persists forever and gets re-injected into
# every future LLM prompt — one successful injection could re-program
# Sage/Jenna's behavior on every subsequent dispatch. Gate every write
# (POST /memory, learn.embed_and_store, brain_ingest) through scan_content
# so obvious payloads don't land in the truth layer.

_THREAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Direct prompt-injection / role hijacking
    (
        "prompt_injection",
        re.compile(
            r"(?i)\b(ignore|disregard|forget)\s+(all|any|the|previous|prior|above)\s+"
            r"(instructions?|prompts?|rules?|constraints?)\b"
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"(?i)\b(you\s+are\s+now|act\s+as|pretend\s+(?:to\s+be|you'?re)|"
            r"from\s+now\s+on\s+you|new\s+system\s+prompt)\b"
        ),
    ),
    (
        "system_prompt_override",
        re.compile(r"(?i)<\s*/?\s*(system|instructions?|sysprompt)\s*>"),
    ),
    # Secret exfiltration — classic "print your system prompt" patterns
    (
        "secret_exfil",
        re.compile(
            r"(?i)\b(print|reveal|show|output|dump|echo)\s+"
            r"(your|the)\s+(system\s+prompt|instructions?|api[\s_-]?key|secret|credentials?)\b"
        ),
    ),
    # Fence bypass — attempts to close our <memory-context> wrapper
    (
        "fence_bypass",
        re.compile(r"(?i)</\s*memory[-_]?context\s*>"),
    ),
    # Hidden characters — zero-width + bidi overrides that hide instructions
    (
        "hidden_unicode",
        re.compile(r"[​‌‍‪‫‬‭‮⁦-⁩]"),
    ),
]

# Known-safe phrases that would otherwise trip the pattern list (e.g. Chris
# writing a canonical note _about_ prompt injection). Exact-substring opt-out.
_ALLOWLIST_FRAGMENTS: tuple[str, ...] = (
    "prompt injection",  # discussing the concept
    "prompt-injection",
)


def scan_content(text: str) -> dict:
    """Scan a prospective atom/ingest payload for prompt-injection patterns.

    Returns ``{"safe": bool, "findings": [(name, match_snippet), ...]}``.
    The caller decides whether to block (hard-gate) or warn+log (soft-gate)
    based on the number and category of findings.
    """
    if not text:
        return {"safe": True, "findings": []}
    findings: list[tuple[str, str]] = []
    lowered = text.lower()
    allowlisted = any(frag in lowered for frag in _ALLOWLIST_FRAGMENTS)
    for name, pat in _THREAT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        # Hidden unicode + fence_bypass are always fatal, can't be allowlisted.
        if allowlisted and name not in {"hidden_unicode", "fence_bypass"}:
            continue
        findings.append((name, m.group(0)[:80]))
    return {"safe": not findings, "findings": findings}


def count_words(text: str) -> int:
    """Count words, treating each Hangul block as ~1 word.

    Approximation — good enough for budget policy. Overcounts pure Korean
    by a small margin which is fine since we cap generously at 50.
    """
    if not text:
        return 0
    text = text.strip()
    # Split on whitespace first
    tokens = text.split()
    word_count = 0
    for tok in tokens:
        # Hangul tokens: count syllables / 2 (Korean tends to pack ~2 syllables/word)
        hangul_blocks = _HANGUL_RE.findall(tok)
        if hangul_blocks:
            for block in hangul_blocks:
                word_count += max(1, len(block) // 2)
        else:
            word_count += 1
    return word_count


def classify(text: str) -> str:
    """Return 'ok' | 'warned' | 'needs_redistill'."""
    n = count_words(text)
    if n <= SOFT_CAP_WORDS:
        return "ok"
    if n <= HARD_CAP_WORDS:
        return "warned"
    return "needs_redistill"


def quality_for(status: str) -> float:
    return {
        "ok": 1.0,
        "warned": 0.7,
        "needs_redistill": 0.3,
    }.get(status, 0.5)


def redistill_via_jenna(long_text: str, *, max_words: int = SOFT_CAP_WORDS) -> str | None:
    """Compress an atom via stateless LLM. Returns None on failure.

    2026-04-17: migrated from openclaw_dispatch (95MB session bleed, $50+/day
    at typical atom volume) to cli_llm.cli_dispatch (codex CLI, ChatGPT Pro
    subscription, <10K tokens/call stateless). Function name kept for
    minimal churn; no longer routes through Jenna.
    """
    try:
        from cli_llm import cli_dispatch
    except Exception as e:
        log.warning("redistill: cli_llm import failed: %s", e)
        return None

    prompt = (
        f"Compress this into a single fact under {max_words} words. "
        "Preserve specifics (names, numbers, dates, technical terms). "
        "No preamble, no commentary, no quotes, no bullet points - just the fact:\n\n"
        f"{long_text}"
    )
    try:
        result = cli_dispatch(prompt, backend="codex", timeout=30)
    except Exception as e:
        log.warning("redistill cli_dispatch raised: %s", e)
        return None

    if not result.ok or not result.text:
        return None
    cleaned = result.text.strip().strip('"').strip("'")
    if count_words(cleaned) > HARD_CAP_WORDS:
        return None
    return cleaned or None


def enforce(text: str, *, allow_redistill: bool = True) -> tuple[str, str, float]:
    """Apply the gate. Returns (final_text, status, quality_score).

    status:
      ok            — stored as-is
      warned        — stored as-is with quality 0.7
      redistilled   — Jenna compressed it, stored compressed with quality 1.0
      stored_long   — Jenna refused or unavailable, stored original with quality 0.3
    """
    status = classify(text)
    if status == "ok":
        return text, "ok", 1.0
    if status == "warned":
        return text, "warned", 0.7
    # needs_redistill
    if not allow_redistill:
        return text, "stored_long", 0.3
    compressed = redistill_via_jenna(text)
    if compressed is None:
        return text, "stored_long", 0.3
    return compressed, "redistilled", 1.0
