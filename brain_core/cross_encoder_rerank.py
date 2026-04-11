"""brain_core/cross_encoder_rerank.py — Cross-encoder reranker via Ollama.

Uses a small LLM to rerank top-N search results by actual query-document relevance.
Feature-flagged (BRAIN_CROSS_ENCODER_ENABLED). Falls back to token-overlap rerank on error.

The Ollama-based approach prompts the model to score each query-document pair
individually. More accurate than token overlap, but ~50-100ms overhead per batch.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.cross_encoder_rerank")

try:
    from config import OLLAMA_URL, BRAIN_CROSS_ENCODER_ENABLED
except ImportError:
    OLLAMA_URL = "http://127.0.0.1:11434"
    BRAIN_CROSS_ENCODER_ENABLED = False

# Use a small model for fast scoring. Falls back to existing rerank if unavailable.
RERANK_MODEL = "qwen2.5:0.5b"  # or other small fast model available in Ollama

SCORING_PROMPT = """Rate how well this document answers the query. Score 0-10.

Query: {query}

Document: {document}

Respond with ONLY a number 0-10. No prose."""


def rerank_with_cross_encoder(query: str, results: list[dict], top_k: int = 20) -> list[dict]:
    """Rerank top-k results using an Ollama-based cross-encoder.

    If flag is off or fails, returns results unchanged.
    """
    if not BRAIN_CROSS_ENCODER_ENABLED or not results:
        return results

    from http_pool import http_json

    to_rerank = results[:top_k]
    scored: list[tuple[float, dict]] = []

    for r in to_rerank:
        doc = (r.get("content") or "")[:500]
        if not doc:
            scored.append((r.get("score", 0), r))
            continue

        prompt = SCORING_PROMPT.format(query=query[:200], document=doc)
        try:
            resp = http_json("POST", f"{OLLAMA_URL}/api/generate", payload={
                "model": RERANK_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0, "num_predict": 5},
            }, timeout=10)

            text = (resp.get("response") or "").strip()
            # Parse score — extract first number
            import re
            m = re.search(r"\d+(?:\.\d+)?", text)
            if m:
                ce_score = float(m.group())
                # Normalize to 0-1, blend with original score
                ce_normalized = min(ce_score / 10.0, 1.0)
                blended = r.get("score", 0) * 0.4 + ce_normalized * 100 * 0.6
                scored.append((blended, r))
            else:
                scored.append((r.get("score", 0), r))
        except Exception as e:
            log.debug("cross_encoder failed for one doc: %s", e)
            scored.append((r.get("score", 0), r))

    # Sort by new score
    scored.sort(key=lambda x: x[0], reverse=True)

    # Update scores in results
    for new_score, r in scored:
        r["cross_encoder_score"] = new_score
        r["score"] = new_score

    # Return reranked top_k + untouched rest
    return [r for _, r in scored] + results[top_k:]
