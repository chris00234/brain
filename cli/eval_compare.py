#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_compare.py — compare /recall vs /recall/v2 on eval_set.json.

Runs every test case against both endpoints and reports:
  - hit@1, hit@5 (expected source appears in top-N)
  - content@5 (expected_content substring appears in top-5 content)
  - mean latency per endpoint
  - per-test winner

Usage:
  eval_compare.py                  # basic run: /recall vs /recall/v2 default
  eval_compare.py --hyde           # add &hyde=true to v2
  eval_compare.py --expand         # add &expand=true to v2
  eval_compare.py --hyde --expand  # both
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import re as _re_eval
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_EVAL_SET = Path("/Users/chrischo/server/brain/cli/eval_set.json")
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BASE = "http://127.0.0.1:8791"
BRAIN_CORE = Path("/Users/chrischo/server/brain/brain_core")
DIVERSITY_HIGH_COSINE_THRESHOLD = 0.92


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(  # noqa: S310 - BASE is a fixed local brain URL.
        BASE + path,
        # M9.4: x-agent=eval so action_audit attributes eval runs correctly
        # instead of lumping them into the generic "unknown" bucket. The
        # brain's /brain/usage endpoint now shows eval load separately.
        headers={"Authorization": f"Bearer {token}", "x-agent": "eval"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed local brain URL.
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "results": []}




def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _topk_diversity_metrics(results: list[dict], *, top_k: int = 5) -> dict:
    """Measure final top-k semantic redundancy using the existing e5 embedder.

    This is deliberately diagnostic-only. It lets eval reports test Claude's
    challenge to the xMemory idea: redundancy should matter only if it
    correlates with downstream source/content failures.
    """

    texts = [str(row.get("content") or row.get("title") or "")[:1000] for row in results[:top_k]]
    texts = [text for text in texts if text.strip()]
    if len(texts) < 2:
        return {
            "status": "insufficient_results",
            "result_count": len(texts),
            "mean_pairwise_cosine": 0.0,
            "max_pairwise_cosine": 0.0,
            "high_similarity_pair_count": 0,
        }
    try:
        if str(BRAIN_CORE) not in sys.path:
            sys.path.insert(0, str(BRAIN_CORE))
        from indexer import get_embeddings_batch

        embeddings = get_embeddings_batch(texts, prefix="passage", use_cache=True)
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc)[:160],
            "result_count": len(texts),
            "mean_pairwise_cosine": 0.0,
            "max_pairwise_cosine": 0.0,
            "high_similarity_pair_count": 0,
        }
    pair_scores: list[float] = []
    for idx, left in enumerate(embeddings):
        for right in embeddings[idx + 1 :]:
            pair_scores.append(_cosine(left, right))
    if not pair_scores:
        return {
            "status": "insufficient_embeddings",
            "result_count": len(texts),
            "mean_pairwise_cosine": 0.0,
            "max_pairwise_cosine": 0.0,
            "high_similarity_pair_count": 0,
        }
    high_pairs = sum(1 for score in pair_scores if score >= DIVERSITY_HIGH_COSINE_THRESHOLD)
    return {
        "status": "ok",
        "result_count": len(texts),
        "mean_pairwise_cosine": round(sum(pair_scores) / len(pair_scores), 4),
        "max_pairwise_cosine": round(max(pair_scores), 4),
        "high_similarity_pair_count": high_pairs,
        "high_similarity_threshold": DIVERSITY_HIGH_COSINE_THRESHOLD,
    }


def _aggregate_diversity(per_test: list[dict]) -> dict:
    rows = [row for row in per_test if isinstance(row.get("diversity"), dict)]
    usable = [row for row in rows if row["diversity"].get("status") == "ok"]

    def _mean(selected: list[dict], key: str = "mean_pairwise_cosine") -> float:
        values = [float(row["diversity"].get(key) or 0.0) for row in selected]
        return round(sum(values) / len(values), 4) if values else 0.0

    content_failed = [row for row in usable if not row.get("hit_content_loose")]
    source_failed = [row for row in usable if not row.get("hit_source")]
    passed = [row for row in usable if row.get("hit_content_loose") and row.get("hit_source")]
    return {
        "status": "ok" if usable else "missing",
        "coverage_level": "final_topk_e5_cosine_v1",
        "case_count": len(usable),
        "error_count": len(rows) - len(usable),
        "mean_pairwise_cosine": _mean(usable),
        "max_pairwise_cosine": max(
            (float(row["diversity"].get("max_pairwise_cosine") or 0.0) for row in usable),
            default=0.0,
        ),
        "high_similarity_pair_count": sum(
            int(row["diversity"].get("high_similarity_pair_count") or 0) for row in usable
        ),
        "passed_mean_pairwise_cosine": _mean(passed),
        "content_failed_mean_pairwise_cosine": _mean(content_failed),
        "source_failed_mean_pairwise_cosine": _mean(source_failed),
        "interpretation": (
            "diagnostic_only; promote retrieval changes only if redundancy "
            "correlates with failures"
        ),
    }

_WORD_RE = _re_eval.compile(r"[\w가-힣]+", _re_eval.UNICODE)
_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "by",
    "at",
    "and",
    "or",
    "but",
    "how",
    "what",
    "why",
    "does",
    "do",
    "did",
    "will",
    "that",
    "this",
    "it",
    "as",
    "be",
    "he",
    "she",
    "they",
    "we",
    "you",
    "from",
    "which",
    "not",
}


def _signif_tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if len(t) >= 2 and t not in _STOP}


def _source_key(value: str) -> str:
    """Normalize source labels for provenance matching.

    The retrieval pipeline returns mixed source surfaces: collection names,
    source_type, full paths, canonical-relative paths, URLs, and titles. Stable
    eval source matching should measure whether the right provenance family is
    present, not whether punctuation/casing/storage layout stayed identical.
    """
    return "".join(_WORD_RE.findall((value or "").lower())).replace("_", "")


def _iter_source_strings(value: Any) -> set[str]:
    """Return string leaves from nested provenance values."""
    out: set[str] = set()
    if value is None:
        return out
    if isinstance(value, str):
        if value:
            out.add(value)
        return out
    if isinstance(value, int | float):
        out.add(str(value))
        return out
    if isinstance(value, dict):
        for item in value.values():
            out.update(_iter_source_strings(item))
        return out
    if isinstance(value, list | tuple | set):
        for item in value:
            out.update(_iter_source_strings(item))
    return out


def _frontmatter_from_content(content: str) -> dict:
    """Parse JSON frontmatter from canonical content when metadata was not threaded."""
    if not content.startswith("---json"):
        return {}
    end = content.find("\n---", 7)
    if end < 0:
        return {}
    try:
        payload = json.loads(content[7:end])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _result_source_forms(result: dict) -> set[str]:
    path = str(result.get("path") or result.get("source") or "")
    title = str(result.get("title") or "")
    collection = str(result.get("collection") or "")
    source_type = str(result.get("source_type") or "")
    result_id = str(result.get("id") or "")
    raw_forms = {path, title, collection, source_type, result_id}
    content = str(result.get("content") or "")

    for container in (
        result.get("metadata") if isinstance(result.get("metadata"), dict) else {},
        result.get("provenance") if isinstance(result.get("provenance"), dict) else {},
        _frontmatter_from_content(content),
    ):
        for key in (
            "id",
            "source_aliases",
            "sources",
            "previous_ids",
            "supersedes",
            "superseded_by",
            "relations",
            "provenance_repair",
        ):
            raw_forms.update(_iter_source_strings(container.get(key)))

    path_lower = path.lower()
    title_lower = title.lower()
    collection_lower = collection.lower()
    source_type_lower = source_type.lower()

    # `distilled/*` notes are produced from the canonical knowledge pipeline.
    # They often replace older canonical files in top-5 while preserving the
    # same semantic provenance family.
    if (
        collection_lower == "distilled"
        or source_type_lower == "distilled"
        or path_lower.startswith("distilled/")
        or "/knowledge/distilled/" in path_lower
    ):
        raw_forms.update({"distilled", "canonical", "knowledge"})

    if (
        collection_lower == "canonical"
        or source_type_lower == "canonical"
        or "/knowledge/canonical/" in path_lower
    ):
        raw_forms.add("canonical")

    if collection_lower == "knowledge" or source_type_lower == "knowledge":
        raw_forms.add("knowledge")

    # Profile facts moved into canonical/chris/_identity.md, but stable eval
    # still uses `_profile` as the expected source label.
    if "_identity.md" in path_lower or "identity" in title_lower:
        raw_forms.update({"_profile", "profile", "identity"})

    expanded = {form for form in raw_forms if form}
    for form in list(expanded):
        expanded.add(form.replace("_", "-"))
        expanded.add(form.replace("-", "_"))
    return expanded | {_source_key(form) for form in expanded if form}


def _source_matches(result: dict, expected_source: str) -> bool:
    expected = (expected_source or "").lower()
    if not expected:
        return True

    expected_key = _source_key(expected)
    forms = _result_source_forms(result)
    for form in forms:
        form_lower = form.lower()
        form_key = _source_key(form_lower)
        if (
            expected in form_lower
            or form_lower == expected
            or expected_key == form_key
            or (expected_key and len(expected_key) > 4 and expected_key in form_key)
            or (form_key and len(form_key) > 12 and form_key in expected_key)
        ):
            return True
    return False


def _query_supports_successor_source(result: dict, query: str) -> bool:
    """True when a current/distilled successor is clearly about the query.

    This is only used to classify source/provenance success for archived
    canonical expectations. It does not grant content credit. The goal is to
    avoid calling a case a complete retrieval miss when the system retrieved a
    current distilled successor that is visibly about the same query, while the
    expected phrase itself is stale or paraphrased.
    """
    q_tokens = _signif_tokens(query)
    if len(q_tokens) < 3:
        return False
    text = " ".join(
        str(result.get(k) or "")
        for k in (
            "title",
            "path",
            "source",
            "source_type",
            "collection",
            "content",
        )
    )
    meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    text += " " + " ".join(_iter_source_strings(meta))
    hit_count = len(q_tokens & _signif_tokens(text[:3000]))
    return hit_count >= min(3, len(q_tokens))


def _is_current_memory_successor_candidate(result: dict, expected_source: str) -> bool:
    """Return true when a current memory note can stand in for an archived source.

    Distilled and current canonical notes can replace older canonical files.
    The eval set still contains many archived paths, so exact source matching
    under-counts cases where retrieval returned the right current memory. This
    helper only identifies the candidate relationship; the caller must still
    require either expected-content overlap or query overlap before granting
    source credit.
    """
    expected = (expected_source or "").lower()
    if not expected:
        return False
    expected_is_canonical_file = (
        expected.endswith(".md")
        or expected.startswith("canonical/")
        or "/canonical/" in expected
        or "canonical/archived/" in expected
    )
    if not expected_is_canonical_file:
        return False

    path = str(result.get("path") or result.get("source") or "").lower()
    collection = str(result.get("collection") or "").lower()
    source_type = str(result.get("source_type") or "").lower()
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    provenance_collection = str(provenance.get("collection") or "").lower()
    provenance_tier = str(provenance.get("tier") or "").lower()
    return (
        collection in {"canonical", "distilled", "knowledge"}
        or source_type in {"canonical", "distilled", "knowledge"}
        or provenance_collection in {"canonical", "distilled", "knowledge"}
        or provenance_tier in {"canonical", "distilled"}
        or path.startswith("distilled/")
        or path.startswith("canonical/")
        or "/knowledge/canonical/" in path
        or "/knowledge/distilled/" in path
        or "/distilled/" in path
    )


def _expected_hit(
    results: list[dict],
    expected_source: str,
    expected_content: str,
    expected_alternates: list[str] | None = None,
    query: str = "",
) -> tuple[bool, bool, int, bool]:
    """Returns (hit_source_top5, hit_content_strict, hit_at_rank, hit_content_loose).

    - hit_content_strict: literal lowercased substring of expected_content OR any
      of expected_alternates appears in the retrieved content
    - hit_content_loose:  ≥75% of significant tokens from expected_content appear
      in the retrieved content (fallback for paraphrased/translated matches)

    M9.1: expected_alternates lets the eval dataset carry multiple equivalent
    phrasings of the same semantic answer. A hit on ANY alternate is a strict
    hit. This removes the ~8% false-negative ceiling from brittle exact-
    substring matching on paraphrased chunks.

    Empty expected_source/expected_content fields pass automatically.
    """
    rank = 0
    hit_source = not expected_source
    hit_content_strict = not expected_content
    hit_content_loose = not expected_content
    # Build list of all acceptable strict-match substrings
    exp_strict_forms: list[str] = []
    if expected_content:
        exp_strict_forms.append(expected_content.lower())
    if expected_alternates:
        for alt in expected_alternates:
            if alt and isinstance(alt, str):
                exp_strict_forms.append(alt.lower())

    exp_content_tokens = _signif_tokens(expected_content or "")
    threshold = max(1, int(len(exp_content_tokens) * 0.75))

    for i, r in enumerate(results[:5], 1):
        content = (r.get("content") or "").lower()

        # Strict hit if ANY acceptable form is a substring
        row_content_loose = False
        if exp_strict_forms and any(form in content for form in exp_strict_forms):
            row_content_loose = True
            hit_content_strict = True
            hit_content_loose = True
        elif exp_content_tokens:
            content_tokens = _signif_tokens(content[:2000])
            overlap = len(exp_content_tokens & content_tokens)
            if overlap >= threshold:
                row_content_loose = True
                hit_content_loose = True

        successor_candidate = _is_current_memory_successor_candidate(r, expected_source)
        if (
            _source_matches(r, expected_source)
            or (bool(expected_content) and row_content_loose and successor_candidate)
            or (successor_candidate and _query_supports_successor_source(r, query))
        ):
            if rank == 0:
                rank = i
            hit_source = True
    return hit_source, hit_content_strict, rank, hit_content_loose


def _forbidden_matches(results: list[dict], forbidden_content: list[str] | None) -> list[str]:
    """Return forbidden substrings found in top-5 retrieved text.

    This supports privacy/stale-negative eval rows. Positive expected-content
    hits are not enough if retrieval also surfaces a forbidden stale claim or
    sensitive raw-content marker.
    """

    forms = [str(item).strip().lower() for item in (forbidden_content or []) if str(item).strip()]
    if not forms:
        return []
    haystacks: list[str] = []
    for result in results[:5]:
        haystacks.append(
            "\n".join(
                str(result.get(key) or "")
                for key in ("content", "path", "source", "title", "source_type", "collection")
            ).lower()
        )
    matches: list[str] = []
    for form in forms:
        if any(form in haystack for haystack in haystacks):
            matches.append(form)
    return matches


def _ndcg_at_k(rank: int, k: int = 5) -> float:
    """Normalized DCG@k for a single binary-relevance item.

    rank is 1-indexed; rank=0 means not in top-k. DCG = 1/log2(rank+1) for the
    one relevant doc, 0 otherwise. IDCG = 1/log2(2) = 1 (relevant doc at rank 1).
    Returns 0 when not retrieved, 1 when at rank 1, ~0.63 at rank 2, etc.
    """
    import math

    if rank <= 0 or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def _reciprocal_rank(rank: int) -> float:
    """Reciprocal rank for a single binary-relevance item. 0 if not retrieved."""
    if rank <= 0:
        return 0.0
    return 1.0 / rank


def run_eval(
    use_v2: bool,
    hyde: bool,
    expand: bool,
    iterative: bool,
    token: str,
    cases: list[dict],
    n_results: int = 5,
    diversity_metrics: bool = False,
) -> dict:
    hits_source = 0
    hits_content_strict = 0
    hits_content_loose = 0
    ranks: list[int] = []
    rr_sum = 0.0
    ndcg_sum = 0.0
    latencies: list[float] = []
    forbidden_hit_count = 0
    per_test: list[dict] = []

    for case in cases:
        q = case["query"]
        expected_source = case.get("expected_source", "")
        expected_content = case.get("expected_content", "")
        forbidden_content = [str(item) for item in (case.get("forbidden_content") or []) if str(item)]

        if use_v2:
            params = {"q": q, "n": str(n_results)}
            if hyde:
                params["hyde"] = "true"
            if expand:
                params["expand"] = "true"
            if iterative:
                params["iterative"] = "true"
            path = "/recall/v2?" + urllib.parse.urlencode(params)
        else:
            path = "/recall?" + urllib.parse.urlencode({"q": q, "n": str(n_results)})

        t0 = time.time()
        payload = _get(path, token)
        dt = (time.time() - t0) * 1000
        latencies.append(dt)

        results = payload.get("results", [])
        expected_alternates = list(case.get("expected_alternates") or [])
        # M9.1 fix: also accept the pre-relabel original substring if present.
        # Sage rewrote expected_content to concept-level forms (e.g.
        # "Start: August 2024" → "August 2024 start date") but the ChromaDB
        # chunks still contain the verbatim original. Without this merge the
        # relabel caused a 58pt drop on extended content_hit — classic
        # "rewrote the test, not the codebase" mistake.
        origin = case.get("_relabel_origin")
        if origin and origin != expected_content:
            expected_alternates.append(origin)
        hs, hc_strict, rank, hc_loose = _expected_hit(
            results, expected_source, expected_content, expected_alternates, query=q
        )
        forbidden_matches = _forbidden_matches(results, forbidden_content)
        forbidden_hit = bool(forbidden_matches)
        if forbidden_hit:
            forbidden_hit_count += 1
        effective_hc_strict = hc_strict and not forbidden_hit
        effective_hc_loose = hc_loose and not forbidden_hit
        if hs:
            hits_source += 1
        if effective_hc_strict:
            hits_content_strict += 1
        if effective_hc_loose:
            hits_content_loose += 1
        if rank > 0:
            ranks.append(rank)

        # M8.1: rank-aware metrics. MRR rewards rank-1 hits 5x more than rank-5 hits.
        # NDCG@5 is normalized so a perfect retrieval (relevant at rank 1) = 1.0.
        # Both are computed on the "rank" signal which uses expected_source matching.
        rr = _reciprocal_rank(rank)
        ndcg = _ndcg_at_k(rank, k=5)
        rr_sum += rr
        ndcg_sum += ndcg

        diversity = _topk_diversity_metrics(results) if diversity_metrics else None
        per_test.append(
            {
                "query": q,
                "expected_source": expected_source,
                "expected_content": expected_content,
                "expected_alternates": expected_alternates,
                "forbidden_content": forbidden_content,
                "forbidden_hit": forbidden_hit,
                "forbidden_matches": forbidden_matches,
                "hit_source": hs,
                "hit_content": effective_hc_strict,
                "hit_content_loose": effective_hc_loose,
                "rank": rank,
                "rr": round(rr, 3),
                "ndcg5": round(ndcg, 3),
                "latency_ms": int(dt),
                **({"diversity": diversity} if diversity is not None else {}),
                "top_sources": [
                    r.get("path") or r.get("source") or r.get("title") or r.get("collection") or ""
                    for r in results[:5]
                ],
            }
        )

    total = len(cases)
    summary = {
        "total": total,
        "hit_source_pct": round(100 * hits_source / total, 1) if total else 0,
        "hit_content_pct": round(100 * hits_content_strict / total, 1) if total else 0,
        "hit_content_loose_pct": round(100 * hits_content_loose / total, 1) if total else 0,
        "forbidden_hit_count": forbidden_hit_count,
        "negative_pass_pct": round(100 * (total - forbidden_hit_count) / total, 1) if total else 100.0,
        "mean_rank": round(sum(ranks) / len(ranks), 2) if ranks else 0,
        "mrr": round(rr_sum / total, 3) if total else 0,
        "ndcg5": round(ndcg_sum / total, 3) if total else 0,
        "mean_latency_ms": round(sum(latencies) / len(latencies), 0) if latencies else 0,
        "per_test": per_test,
    }
    if diversity_metrics:
        summary["diversity"] = _aggregate_diversity(per_test)
    return summary


def _persist_report(report: dict, track: str, content_metric: str) -> None:
    from eval_gate import _persist_eval_report

    _persist_eval_report(report, track=track, content_metric=content_metric)


def _generate_rag_answer(question: str, contexts: list[str], *, timeout: int = 45) -> tuple[str, str]:
    """Generate an answer from retrieved contexts for true answer-level RAGAS.

    The legacy RAGAS path used top retrieved context as the "answer", which is
    useful for faithfulness but makes answer relevance mostly informational.
    This helper keeps retrieval unchanged but adds a cheap synthesis step so
    answer_relevance measures an actual generated RAG answer.
    """

    if not contexts:
        return "", "generated_empty_context"
    context_text = "\n\n".join(f"[{i + 1}] {chunk[:900]}" for i, chunk in enumerate(contexts[:5]))
    prompt = (
        "You are Chris's Brain RAG answer generator. Answer the question using ONLY the retrieved "
        "context. Directly answer the exact query first; do not answer adjacent questions. "
        "If the context is insufficient for a specific detail, say what is missing, then give the "
        "most concrete supported answer. Be concise.\n\n"
        f"Question: {question[:500]}\n\nRetrieved context:\n{context_text}\n\nAnswer:"
    )
    try:
        sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
        from cli_llm import dispatch

        result = dispatch(
            "jenna",
            prompt,
            thinking="low",
            timeout=timeout,
            openclaw_agent="jenna",
            backlog_kind="synthesis",
            backlog_payload={"source": "eval_compare:ragas_answer", "prompt": prompt},
        )
    except Exception:
        return contexts[0] if contexts else "", "generated_error_fallback_context"
    if result.ok and result.text and result.text.strip():
        return result.text.strip(), "generated"
    return contexts[0] if contexts else "", "generated_failed_fallback_context"


def _select_ragas_answer(
    question: str,
    contexts: list[str],
    payload: dict,
    *,
    answer_source: str,
    timeout: int = 45,
) -> tuple[str, str]:
    """Select the answer text sent to RAGAS plus an auditable source label."""

    if answer_source == "generated":
        return _generate_rag_answer(question, contexts, timeout=timeout)
    if answer_source == "hyde":
        hypothetical = str(payload.get("hypothetical") or "").strip()
        if hypothetical:
            return hypothetical, "hyde"
        return (contexts[0] if contexts else ""), "hyde_missing_fallback_context"
    return (contexts[0] if contexts else ""), "context"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare /recall vs /recall/v2")
    parser.add_argument("--hyde", action="store_true")
    parser.add_argument("--expand", action="store_true")
    parser.add_argument(
        "--iterative",
        action="store_true",
        help="Phase M9 CRAG iterative retrieval — pass ?iterative=true to /recall/v2",
    )
    parser.add_argument(
        "--ragas",
        action="store_true",
        help="M8.3: run RAGAS LLM-as-judge scoring (faithfulness/relevance) on each case "
        "via CLI-first dispatch (Codex gpt-5.5 primary, OpenClaw emergency fallback). "
        "Much slower (~5s/case). Use on --limit 50 subsets.",
    )
    parser.add_argument(
        "--ragas-answer-source",
        choices=["context", "hyde", "generated"],
        default="context",
        help=(
            "Answer text to judge in --ragas mode. 'context' preserves the legacy top-chunk surrogate; "
            "'hyde' uses recall/v2 hypothetical answer when present; 'generated' synthesizes an answer "
            "from retrieved context so answer_relevance is a real answer-level gate."
        ),
    )
    parser.add_argument("--limit", type=int, default=0, help="Only run first N cases (0 = all)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--diversity-metrics",
        action="store_true",
        help="Add diagnostic final-top-k e5 cosine redundancy metrics to eval output.",
    )
    parser.add_argument(
        "--include-per-test",
        action="store_true",
        help="Keep per_test case results in JSON output. Required by LoRA A/B gate "
        "for per-query worst-regression check (2026-04-16 fix).",
    )
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=DEFAULT_EVAL_SET,
        help="Path to eval_set.json (default: cli/eval_set.json)",
    )
    parser.add_argument(
        "--persist-track",
        choices=["default", "stable", "extended", "adversarial", "ragas", "holdout", "legacy"],
        default="",
        help="Persist this run to logs/eval-report[-track].json and eval-history[-track].jsonl.",
    )
    parser.add_argument(
        "--content-metric",
        choices=["strict", "loose"],
        default="strict",
        help="Content metric to persist when --persist-track is set.",
    )
    args = parser.parse_args()

    if not SECRET_FILE.exists():
        sys.stderr.write(f"no secret file at {SECRET_FILE}\n")
        return 2
    token = SECRET_FILE.read_text().strip()

    cases = json.loads(args.eval_set.read_text())
    if args.limit > 0:
        cases = cases[: args.limit]

    if not args.json:
        print(f"Running {len(cases)} eval cases against /recall and /recall/v2...")
    baseline = run_eval(
        use_v2=False,
        hyde=False,
        expand=False,
        iterative=False,
        token=token,
        cases=cases,
        diversity_metrics=args.diversity_metrics,
    )
    v2 = run_eval(
        use_v2=True,
        hyde=args.hyde,
        expand=args.expand,
        iterative=args.iterative,
        token=token,
        cases=cases,
        diversity_metrics=args.diversity_metrics,
    )

    # M8.3: RAGAS LLM-as-judge scoring (opt-in)
    ragas_agg = None
    if args.ragas:
        import sys as _sys

        _sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
        try:
            from ragas_judge import aggregate as _ragas_agg
            from ragas_judge import score_one as _ragas_score

            if not args.json:
                print(f"\nRunning RAGAS judge on {len(cases)} cases...")
            scores = []
            answer_sources: Counter[str] = Counter()
            ragas_cases = []
            for i, case in enumerate(cases):
                q = case["query"]
                expected = case.get("answer_rubric") or case.get("expected_content", "")
                # Re-query to capture contexts for the judge
                params = {"q": q, "n": "5"}
                if args.hyde:
                    params["hyde"] = "true"
                if args.expand:
                    params["expand"] = "true"
                if args.iterative:
                    params["iterative"] = "true"
                path = "/recall/v2?" + urllib.parse.urlencode(params)
                try:
                    payload = _get(path, token)
                    results = payload.get("results", [])[:5]
                    contexts = [r.get("content", "")[:800] for r in results if r.get("content")]
                    answer, selected_answer_source = _select_ragas_answer(
                        q,
                        contexts,
                        payload,
                        answer_source=args.ragas_answer_source,
                    )
                    answer_sources[selected_answer_source] += 1
                    s = _ragas_score(
                        q,
                        answer,
                        contexts,
                        expected=expected,
                        metrics=["faithfulness", "answer_relevance"],
                        timeout=30,
                    )
                    scores.append(s)
                    ragas_cases.append(
                        {
                            "query": q,
                            "answer_source": selected_answer_source,
                            "answer_preview": answer[:240],
                            "answer_rubric": str(case.get("answer_rubric") or "")[:240],
                            "score": s.to_dict(),
                        }
                    )
                    if not args.json and (i + 1) % 10 == 0:
                        print(f"  {i + 1}/{len(cases)} scored")
                except Exception as e:
                    sys.stderr.write(f"ragas_judge failed on {i}: {e}\n")
            ragas_agg = _ragas_agg(scores)
            ragas_agg["answer_source"] = args.ragas_answer_source
            ragas_agg["answer_source_counts"] = dict(answer_sources)
            ragas_agg["cases"] = ragas_cases
        except Exception as e:
            sys.stderr.write(f"ragas_judge wiring failed: {e}\n")

    mode_parts = []
    if args.hyde:
        mode_parts.append("hyde")
    if args.expand:
        mode_parts.append("expand")
    if args.iterative:
        mode_parts.append("iterative")
    mode = "+".join(mode_parts) if mode_parts else "basic"

    report = {
        "cases": len(cases),
        "v2_mode": mode,
        "baseline": {k: v for k, v in baseline.items() if k != "per_test" or args.include_per_test},
        "v2": {k: v for k, v in v2.items() if k != "per_test" or args.include_per_test},
    }
    if ragas_agg:
        report["ragas"] = ragas_agg

    if args.persist_track:
        track = "default" if args.persist_track == "legacy" else args.persist_track
        _persist_report(report, track=track, content_metric=args.content_metric)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("\n" + "=" * 60)
    print(f"Eval comparison — {mode} mode, {len(cases)} cases")
    print("=" * 60)
    for name, r in (("/recall (baseline)", baseline), (f"/recall/v2 ({mode})", v2)):
        print(f"\n{name}")
        print(f"  hit_source@5          : {r['hit_source_pct']:5.1f}%")
        print(f"  hit_content@5 strict  : {r['hit_content_pct']:5.1f}%")
        print(f"  hit_content@5 loose   : {r.get('hit_content_loose_pct', 0):5.1f}%  (≥75% token overlap)")
        print(f"  mean rank             : {r['mean_rank']}")
        print(f"  mean latency          : {r['mean_latency_ms']:5.0f} ms")
        if args.diversity_metrics:
            div = r.get("diversity", {})
            print(f"  top-k mean cosine     : {div.get('mean_pairwise_cosine', 0):.4f}")
            print(f"  top-k high-sim pairs  : {div.get('high_similarity_pair_count', 0)}")

    ds = v2["hit_source_pct"] - baseline["hit_source_pct"]
    dc = v2["hit_content_pct"] - baseline["hit_content_pct"]
    print("\nDelta (v2 - baseline)")
    print(f"  hit_source@5    : {ds:+.1f} pts")
    print(f"  hit_content@5   : {dc:+.1f} pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
