"""brain_core/hyde.py — Hypothetical Document Embeddings + query expansion.

HyDE (Gao et al. 2022, https://arxiv.org/abs/2212.10496) improves retrieval
recall for conversational and abstract queries by asking an LLM to first
write a 3-sentence hypothetical answer to the query, then embedding THAT
instead of the raw query. The hypothetical answer lives in the same
embedding space as the real documents, which closes the
"query-doc vocabulary mismatch" gap.

Query expansion generates 3 alternative phrasings of the query and runs each
in parallel, then fuses with RRF. Complementary to HyDE — HyDE helps with
vague/short queries, expansion helps with jargon/typo queries.

Constraint: every LLM call here goes through `openclaw agent --agent jenna`
which uses Chris's existing OpenAI subscription. Zero direct API calls.
Embeddings still go through the indexer's Ollama client (embedder-only rule
preserved).

Simple in-process LRU cache with 5-minute TTL keeps repeated queries fast.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Reuse the sibling indexer's Ollama embedder + resilient dispatcher.
sys.path.insert(0, str(Path(__file__).parent))
from indexer import get_embedding  # noqa: E402
from openclaw_dispatch import dispatch as _dispatch  # noqa: E402

try:
    from config import OPENCLAW_BIN
except ImportError:
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
DISPATCH_TIMEOUT = 60
CACHE_TTL_SECONDS = 300  # 5 minutes
MAX_CACHE_ENTRIES = 256
EMBED_TRUNCATE = 1000

# ── Prompts ────────────────────────────────────────────────
HYDE_PROMPT = """You are Chris's second brain. Write a 3-sentence answer to the following question as if you were retrieving from Chris's knowledge base. Do not speculate outside what a reasonable answer would contain. No prose outside the answer. No preamble.

Question: {query}

Answer:"""

EXPAND_PROMPT = """Rewrite the following search query as 3 alternative phrasings a search engine could match. One per line. No numbering, no bullets, no preamble, no commentary — just the 3 rewrites, one per line.

Query: {query}

Rewrites:"""


# ── Cache ─────────────────────────────────────────────────
class _TTLCache:
    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires, value = entry
            if time.time() > expires:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self.max_entries:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                self._store.pop(oldest, None)
            self._store[key] = (time.time() + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_hyde_cache = _TTLCache(CACHE_TTL_SECONDS, MAX_CACHE_ENTRIES)
_expand_cache = _TTLCache(CACHE_TTL_SECONDS, MAX_CACHE_ENTRIES)


# ── OpenClaw dispatch helper (thin wrapper around shared dispatcher) ─
def _dispatch_to_jenna(prompt: str, thinking: str = "low", timeout: int = DISPATCH_TIMEOUT) -> str:
    """Dispatch to Jenna via the resilient wrapper. Returns "" on any failure."""
    result = _dispatch(agent="jenna", message=prompt, thinking=thinking, timeout=timeout)
    return result.text if result.ok else ""


# ── HyDE ─────────────────────────────────────────────────
def generate_hypothetical(query: str) -> str:
    """Generate a hypothetical answer for the query. Cached for 5 min."""
    if not query or not query.strip():
        return ""
    cached = _hyde_cache.get(query)
    if cached is not None:
        return cached

    # Tight timeout: HyDE is in the search hot path. A slow Jenna (or a dead
    # OpenClaw gateway) must not stall the search pipeline — fall back to the
    # raw query embedding quickly. 10s is enough for a small thinking=low reply.
    reply = _dispatch_to_jenna(HYDE_PROMPT.format(query=query), thinking="low", timeout=10)
    # Strip markdown fences / leading "Answer:" boilerplate the model may add.
    reply = re.sub(r"^```.*?\n", "", reply, flags=re.DOTALL)
    reply = re.sub(r"\s*```$", "", reply)
    reply = re.sub(r"^\s*Answer\s*:\s*", "", reply, flags=re.IGNORECASE)
    reply = reply.strip()

    _hyde_cache.set(query, reply)
    return reply


def hyde_embedding(query: str) -> tuple[list[float] | None, str]:
    """Generate a HyDE-enhanced embedding for the query.

    Returns (embedding, hypothetical_text). If HyDE dispatch fails, falls back
    to embedding the raw query and returns the raw query as the hypothetical
    text for debugging.
    """
    hypothetical = generate_hypothetical(query)
    # Fall back to raw query if the dispatch failed or returned nothing.
    embed_input = hypothetical if hypothetical else query
    try:
        # HyDE text stands in for the QUERY side of an asymmetric model, so it
        # must be embedded with the query prefix — not the default "passage".
        emb = get_embedding(embed_input[:EMBED_TRUNCATE], prefix="query")
    except Exception:
        emb = None
    return emb, hypothetical


# ── Query Expansion ──────────────────────────────────────
def expand_query(query: str, max_variants: int = 3) -> list[str]:
    """Generate alternative phrasings via Jenna. Cached for 5 min.

    Always includes the original query as the first entry. On failure,
    returns just the original.
    """
    if not query or not query.strip():
        return []

    cached = _expand_cache.get(query)
    if cached is not None:
        return cached

    reply = _dispatch_to_jenna(EXPAND_PROMPT.format(query=query), thinking="low", timeout=30)
    variants: list[str] = [query]
    for line in reply.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip common list markers
        line = re.sub(r"^\s*(?:[\*\-•]|\d+[\.\)])\s*", "", line)
        line = line.strip()
        if line and line != query and line not in variants:
            variants.append(line)
        if len(variants) >= max_variants + 1:  # original + N variants
            break

    _expand_cache.set(query, variants)
    return variants


def clear_cache() -> None:
    _hyde_cache.clear()
    _expand_cache.clear()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HyDE + query expansion smoke test")
    parser.add_argument("query")
    parser.add_argument("--expand", action="store_true")
    args = parser.parse_args()

    print(f"Query: {args.query}")
    if args.expand:
        variants = expand_query(args.query)
        print(f"Variants: {variants}")
    else:
        h = generate_hypothetical(args.query)
        print(f"Hypothetical: {h}")
        emb, _ = hyde_embedding(args.query)
        print(f"Embedding dims: {len(emb) if emb else 'none'}")
