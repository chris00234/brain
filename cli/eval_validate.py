#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_validate.py — Validate mined eval candidates against the live brain.

Reads JSON-lines candidates from eval_mine_canonical.py output, tests each by
hitting /recall/v2?n=10 to confirm the expected_source is actually retrievable,
then writes the surviving candidates to an output file ready for merge.

Also does dedup, length filter, and sensitivity filter.

Usage:
  eval_validate.py [--input PATH] [--output PATH] [--private PATH]
                   [--n-results 10] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BASE_URL = "http://127.0.0.1:8791"

DEFAULT_INPUT = Path("/tmp/brain_eval_mine_canonical.jsonl")
DEFAULT_OUTPUT = Path("/tmp/brain_eval_validated.jsonl")
DEFAULT_PRIVATE = Path("/tmp/brain_eval_validated.private.jsonl")

SENSITIVE_PATTERN = re.compile(
    r"(password|secret|api[_\- ]?key|token|credentials|wheogus|비밀번호|비번)",
    re.IGNORECASE,
)


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(BASE_URL + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "results": []}


def _hit_in_topk(results: list, expected_source: str, expected_content: str) -> bool:
    """True if the expected_source or expected_content appears anywhere in the top-k results."""
    exp_src = (expected_source or "").lower()
    exp_con = (expected_content or "").lower()
    for r in results:
        path = (r.get("path") or r.get("source") or "").lower()
        title = (r.get("title") or "").lower()
        content = (r.get("content") or "").lower()
        coll = (r.get("collection") or "").lower()
        stype = (r.get("source_type") or "").lower()
        if exp_src and (
            exp_src in path
            or exp_src in title
            or exp_src == coll
            or exp_src == stype
        ):
            return True
        if exp_con and exp_con in content:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate mined eval candidates")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--private", type=Path, default=DEFAULT_PRIVATE,
                        help="Path for candidates flagged sensitive")
    parser.add_argument("--n-results", type=int, default=10,
                        help="Top-N to check (widened from eval's default 5)")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N (0 = all)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"FATAL: input not found at {args.input}", file=sys.stderr)
        return 2
    if not SECRET_FILE.exists():
        print(f"FATAL: secret file missing at {SECRET_FILE}", file=sys.stderr)
        return 2
    token = SECRET_FILE.read_text().strip()

    # Load candidates
    candidates = []
    for line in args.input.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if args.limit > 0:
        candidates = candidates[:args.limit]

    print(f"loaded {len(candidates)} candidates from {args.input}")

    # Dedup on lowercased query
    seen_queries: set[str] = set()
    deduped: list[dict] = []
    for c in candidates:
        q = (c.get("query") or "").strip().lower()
        if not q or q in seen_queries:
            continue
        seen_queries.add(q)
        deduped.append(c)
    print(f"after dedup: {len(deduped)}")

    # Length filter
    filtered = [c for c in deduped if 5 <= len(c.get("query") or "") <= 200]
    print(f"after length filter: {len(filtered)}")

    # Sensitivity filter: split public / private
    public: list[dict] = []
    private: list[dict] = []
    for c in filtered:
        text_blob = f"{c.get('query', '')} {c.get('expected_source', '')} {c.get('expected_content', '')}"
        if SENSITIVE_PATTERN.search(text_blob):
            private.append(c)
        else:
            public.append(c)
    print(f"sensitivity split: public={len(public)} private={len(private)}")

    # Validation: hit /recall/v2?n=10, keep only if expected content or source is in top-N
    print(f"\nvalidating {len(public)} public candidates against /recall/v2?n={args.n_results}...")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    out_f = args.output.open("w")
    private_f = args.private.open("w")
    kept_public = 0
    dropped_unsolvable = 0

    t_start = time.time()
    for i, c in enumerate(public, 1):
        query = c["query"]
        path = "/recall/v2?" + urllib.parse.urlencode({"q": query, "n": str(args.n_results)})
        payload = _get(path, token)
        results = payload.get("results", [])
        if _hit_in_topk(results, c.get("expected_source", ""), c.get("expected_content", "")):
            out_f.write(json.dumps(c, ensure_ascii=False) + "\n")
            kept_public += 1
        else:
            dropped_unsolvable += 1
        if i % 25 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i}/{len(public)}] kept={kept_public} dropped={dropped_unsolvable} ({elapsed:.0f}s)")

    out_f.close()

    # Write private set (not validated — just filtered, kept local)
    for c in private:
        private_f.write(json.dumps(c, ensure_ascii=False) + "\n")
    private_f.close()

    print(f"\nDONE — kept_public={kept_public} dropped_unsolvable={dropped_unsolvable}")
    print(f"  public: {args.output}")
    print(f"  private: {args.private}")
    print(f"total validation time: {time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
