"""Recall ↔ store bridge helpers.

When the store says NOOP duplicate but the caller's recall did NOT surface
the duplicate target, the brain still owes the caller two things:

  1. A short, deterministic ``suggested_bridge_query`` that the next recall
     can use to actually reach the missed memory.
  2. A ``recall_repair`` diagnostic listing the exact-alias tokens the
     duplicate target carries that the caller's query never mentioned —
     these are the precise lexical bridges that would have hit.

This module is intentionally tiny + dependency-free so it can be imported by
both the POST /memory route and the recall debug surface. All inputs are
plain dicts / strings; no vector store, no LLM. The aliases come from the
shared ``tokenizer.extract_exact_aliases`` helper so rerank and bridge stay
on the same definition of what counts as a code-ish symbol.
"""

from __future__ import annotations

from typing import Any

from tokenizer import extract_exact_aliases as _extract_aliases

# Cap on aliases appended to the bridge query so we don't balloon a recall
# query past the embedder's 512-token window — the top-N most distinctive
# tokens are the bridge.
_MAX_BRIDGE_ALIASES = 4


def extract_exact_aliases(text: str) -> list[str]:
    """Public re-export. Keeps callers from reaching into tokenizer for
    recall-store bridge behavior.
    """
    return _extract_aliases(text or "")


def _collect_target_aliases(target_doc: str, target_meta: dict[str, Any] | None) -> list[str]:
    """Aliases pulled from explicit metadata first, then mined from the doc
    body, title, path. Order = "explicit-first" so curated aliases lead.
    """
    seen: dict[str, None] = {}
    meta = target_meta or {}

    def _add(value: str) -> None:
        v = (value or "").strip()
        if v and v not in seen:
            seen[v] = None

    for alias in meta.get("source_aliases") or []:
        _add(str(alias))
    for src in (meta.get("title") or "", meta.get("source_path") or "", meta.get("source") or ""):
        for a in _extract_aliases(str(src)):
            _add(a)
    for a in _extract_aliases(target_doc or ""):
        _add(a)

    return list(seen.keys())


def build_suggested_bridge_query(
    new_content: str,
    target_doc: str,
    target_meta: dict[str, Any] | None = None,
) -> str:
    """Compose a recall query that should hit the duplicate target next time.

    Layout: caller's intent first, then up to ``_MAX_BRIDGE_ALIASES`` of the
    duplicate target's exact aliases that the caller's content didn't already
    mention. Keeps the original intent anchored (so vector similarity still
    leans toward the right cluster) while pinning lexical hooks the original
    query missed.
    """
    intent = (new_content or "").strip()
    aliases = _collect_target_aliases(target_doc, target_meta)

    bridge_parts = [intent] if intent else []
    seen_in_intent_lower = intent.lower()
    for alias in aliases:
        if len(bridge_parts) - (1 if intent else 0) >= _MAX_BRIDGE_ALIASES:
            break
        # Substring check: case-insensitive prose (``codex home``) doesn't
        # exempt ``CODEX_HOME`` because the exact env var is what unlocks
        # the lexical bridge.
        if alias.lower() in seen_in_intent_lower and alias.lower() == alias:
            continue
        if alias in intent:
            continue
        bridge_parts.append(alias)

    return " ".join(bridge_parts).strip()


def compute_recall_repair(
    query_content: str,
    target_doc: str,
    target_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the alias-gap diagnostic.

    Shape::

        {
          "exact_aliases":  [...],   # every alias mined from the duplicate
          "missing_tokens": [...],   # aliases the caller's query did NOT mention
          "query_aliases":  [...],   # aliases already present in the query
        }

    Substring match is exact (case-preserving) because that's what the
    rerank exact-alias boost will look for. If we lower-cased we'd silently
    bridge ``codex_home`` to ``CODEX_HOME`` even though the user's literal
    query (``codex_home``) would not actually hit ``CODEX_HOME`` under the
    boosted rerank.
    """
    target_aliases = _collect_target_aliases(target_doc, target_meta)
    query_aliases = _extract_aliases(query_content or "")
    query_text = query_content or ""
    missing = [a for a in target_aliases if a not in query_text]
    return {
        "exact_aliases": target_aliases,
        "missing_tokens": missing,
        "query_aliases": query_aliases,
    }
