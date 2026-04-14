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
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_EVAL_SET = Path("/Users/chrischo/server/brain/cli/eval_set.json")
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BASE = "http://127.0.0.1:8791"


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        BASE + path,
        # M9.4: x-agent=eval so action_audit attributes eval runs correctly
        # instead of lumping them into the generic "unknown" bucket. The
        # brain's /brain/usage endpoint now shows eval load separately.
        headers={"Authorization": f"Bearer {token}", "x-agent": "eval"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "results": []}


import re as _re_eval

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


def _expected_hit(
    results: list[dict],
    expected_source: str,
    expected_content: str,
    expected_alternates: list[str] | None = None,
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
    exp_source_lower = (expected_source or "").lower()

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
        path = (r.get("path") or r.get("source") or "").lower()
        title = (r.get("title") or "").lower()
        content = (r.get("content") or "").lower()
        collection = (r.get("collection") or "").lower()
        source_type = (r.get("source_type") or "").lower()
        if exp_source_lower and (
            exp_source_lower in path
            or exp_source_lower in title
            or exp_source_lower == collection
            or exp_source_lower == source_type
        ):
            if rank == 0:
                rank = i
            hit_source = True
        # Strict hit if ANY acceptable form is a substring
        if exp_strict_forms and any(form in content for form in exp_strict_forms):
            hit_content_strict = True
            hit_content_loose = True
        elif exp_content_tokens and not hit_content_loose:
            content_tokens = _signif_tokens(content[:2000])
            overlap = len(exp_content_tokens & content_tokens)
            if overlap >= threshold:
                hit_content_loose = True
    return hit_source, hit_content_strict, rank, hit_content_loose


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
    use_v2: bool, hyde: bool, expand: bool, iterative: bool, token: str, cases: list[dict], n_results: int = 5
) -> dict:
    hits_source = 0
    hits_content_strict = 0
    hits_content_loose = 0
    ranks: list[int] = []
    rr_sum = 0.0
    ndcg_sum = 0.0
    latencies: list[float] = []
    per_test: list[dict] = []

    for case in cases:
        q = case["query"]
        expected_source = case.get("expected_source", "")
        expected_content = case.get("expected_content", "")

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
            results, expected_source, expected_content, expected_alternates
        )
        if hs:
            hits_source += 1
        if hc_strict:
            hits_content_strict += 1
        if hc_loose:
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

        per_test.append(
            {
                "query": q,
                "hit_source": hs,
                "hit_content": hc_strict,
                "hit_content_loose": hc_loose,
                "rank": rank,
                "rr": round(rr, 3),
                "ndcg5": round(ndcg, 3),
                "latency_ms": int(dt),
            }
        )

    total = len(cases)
    return {
        "total": total,
        "hit_source_pct": round(100 * hits_source / total, 1) if total else 0,
        "hit_content_pct": round(100 * hits_content_strict / total, 1) if total else 0,
        "hit_content_loose_pct": round(100 * hits_content_loose / total, 1) if total else 0,
        "mean_rank": round(sum(ranks) / len(ranks), 2) if ranks else 0,
        "mrr": round(rr_sum / total, 3) if total else 0,
        "ndcg5": round(ndcg_sum / total, 3) if total else 0,
        "mean_latency_ms": round(sum(latencies) / len(latencies), 0) if latencies else 0,
        "per_test": per_test,
    }


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
        "via openclaw_dispatch to Sage. Much slower (~5s/case) and costs "
        "~$0.0005/case. Use on --limit 50 subsets.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only run first N cases (0 = all)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=DEFAULT_EVAL_SET,
        help="Path to eval_set.json (default: cli/eval_set.json)",
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
    baseline = run_eval(use_v2=False, hyde=False, expand=False, iterative=False, token=token, cases=cases)
    v2 = run_eval(
        use_v2=True, hyde=args.hyde, expand=args.expand, iterative=args.iterative, token=token, cases=cases
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
            for i, case in enumerate(cases):
                q = case["query"]
                expected = case.get("expected_content", "")
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
                    # The "answer" for RAGAS is the top-1 result's content — mirrors
                    # how a naive RAG answer would be constructed from retrieval alone
                    answer = contexts[0] if contexts else ""
                    s = _ragas_score(
                        q,
                        answer,
                        contexts,
                        expected=expected,
                        metrics=["faithfulness", "answer_relevance"],
                        timeout=30,
                    )
                    scores.append(s)
                    if not args.json and (i + 1) % 10 == 0:
                        print(f"  {i + 1}/{len(cases)} scored")
                except Exception as e:
                    sys.stderr.write(f"ragas_judge failed on {i}: {e}\n")
            ragas_agg = _ragas_agg(scores)
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
        "baseline": {k: v for k, v in baseline.items() if k != "per_test"},
        "v2": {k: v for k, v in v2.items() if k != "per_test"},
    }
    if ragas_agg:
        report["ragas"] = ragas_agg

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

    ds = v2["hit_source_pct"] - baseline["hit_source_pct"]
    dc = v2["hit_content_pct"] - baseline["hit_content_pct"]
    print("\nDelta (v2 − baseline)")
    print(f"  hit_source@5    : {ds:+.1f} pts")
    print(f"  hit_content@5   : {dc:+.1f} pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
