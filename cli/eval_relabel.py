#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_relabel.py — rewrite brittle expected_content in extended eval set.

M9.1: the extended eval's `expected_content` is hardcoded short substrings
(avg 32 chars, 74 queries under 20 chars). This creates an ~8% false-negative
ceiling where retrieval returns the right chunk but the strict substring
check fails because the chunk paraphrases or varies in capitalization/spacing.

This script batches the eval cases in groups of 20, sends each batch to Sage
via openclaw_dispatch, and asks for semantic-equivalent rewrites of the
expected_content that are:
  - Still specific enough to uniquely identify the right chunk
  - Robust to paraphrase (key entities + concepts, not exact phrasing)
  - Match SEVERAL reasonable phrasings a retrieved chunk might use

Output schema per item: {id, original_expected, rewritten_expected,
alternate_forms[]}. The script writes a backup of the original eval set
and produces eval_set_extended_v2.json with the rewrites merged in.

Cost: ~$0.002 per batch x 30 batches = ~$0.06 for 606 queries. Much cheaper
than a human label pass.

Usage:
  cli/eval_relabel.py --limit 20   # dry run on first 20
  cli/eval_relabel.py              # full run
  cli/eval_relabel.py --apply      # actually write eval_set_extended_v2.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from openclaw_dispatch import dispatch

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
SRC = BRAIN_ROOT / "cli" / "eval_set_extended.json"
DST = BRAIN_ROOT / "cli" / "eval_set_extended_v2.json"
BACKUP_DIR = BRAIN_ROOT / "cli" / "eval_backups"

BATCH_SIZE = 10
SAGE_TIMEOUT = 60


_PROMPT = """You are relabeling a RAG evaluation dataset. Each item has a query and an `expected_content` field that's used to verify whether retrieval found the right chunk via strict substring match. The current substrings are too literal — they fail when the retrieved chunk paraphrases or varies wording.

For each item below, rewrite `expected_content` to be concept-level semantic: a short phrase (15-40 chars) that captures the core fact or decision the correct chunk MUST contain, regardless of exact wording. Also provide 2-3 `alternate_forms` that are equally valid substring matches.

Rules:
- Keep it specific enough to uniquely identify the correct chunk
- Remove quoted punctuation, dates with exact formatting, and rare-phrase matches
- If the original is already generic enough (e.g. "React + Vite + TypeScript"), keep it as-is and put empty alternate_forms
- Output MUST be valid JSON matching the schema

Input items:
{items_json}

Output ONLY a JSON object with this schema:
{{
  "items": [
    {{
      "index": <0-based index into input>,
      "expected": "<rewritten expected substring, 15-40 chars>",
      "alternates": ["<alt 1>", "<alt 2>", "<alt 3>"]
    }},
    ...
  ]
}}

No prose. No markdown fences. Just the JSON."""


_JSON_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _parse_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _relabel_batch(cases: list[dict]) -> list[dict] | None:
    items_json = json.dumps(
        [
            {
                "index": i,
                "query": c.get("query", ""),
                "current_expected": c.get("expected_content", ""),
            }
            for i, c in enumerate(cases)
        ],
        ensure_ascii=False,
        indent=2,
    )
    prompt = _PROMPT.format(items_json=items_json)

    result = dispatch("sage", prompt, thinking="off", timeout=SAGE_TIMEOUT)
    if not result.ok or not result.text:
        return None

    parsed = _parse_json(result.text)
    if not parsed or not isinstance(parsed.get("items"), list):
        return None

    out: list[dict] = []
    for ritem in parsed["items"]:
        idx = ritem.get("index")
        exp = ritem.get("expected", "").strip()
        alts = [a.strip() for a in (ritem.get("alternates") or []) if isinstance(a, str)]
        if not isinstance(idx, int) or not (0 <= idx < len(cases)):
            continue
        if not exp:
            continue
        out.append({"index": idx, "expected": exp[:80], "alternates": alts[:3]})
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=0, help="Only process first N cases (0 = all)")
    p.add_argument("--apply", action="store_true", help="Write eval_set_extended_v2.json")
    p.add_argument("--resume", action="store_true", help="Skip already-relabeled cases if DST exists")
    args = p.parse_args()

    cases = json.loads(SRC.read_text())
    n_total = len(cases)
    if args.limit > 0:
        cases = cases[: args.limit]

    print(f"[eval_relabel] loaded {len(cases)} cases (total set size {n_total})", file=sys.stderr)

    # Backup the original
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"eval_set_extended.{_now().replace(':', '-')}.json"
    backup_path.write_text(SRC.read_text())
    print(f"[eval_relabel] backup written: {backup_path}", file=sys.stderr)

    # Existing rewrites (for resume)
    existing_rewrites: dict[int, dict] = {}
    if args.resume and DST.exists():
        try:
            existing = json.loads(DST.read_text())
            for i, c in enumerate(existing):
                if c.get("expected_content") != cases[i].get("expected_content") if i < len(cases) else False:
                    existing_rewrites[i] = c
        except Exception as _exc:
            print(f"[eval_relabel] resume-load failed: {_exc}", file=sys.stderr)
    print(f"[eval_relabel] resuming from {len(existing_rewrites)} existing rewrites", file=sys.stderr)

    rewrites: dict[int, dict] = dict(existing_rewrites)
    dispatched_batches = 0
    total_rewritten = 0

    for batch_start in range(0, len(cases), BATCH_SIZE):
        # Skip whole batch if all cases are already rewritten
        if all(i in rewrites for i in range(batch_start, min(batch_start + BATCH_SIZE, len(cases)))):
            continue
        batch = cases[batch_start : batch_start + BATCH_SIZE]
        print(
            f"[eval_relabel] batch {dispatched_batches + 1} "
            f"({batch_start}-{batch_start + len(batch) - 1})...",
            file=sys.stderr,
        )
        t0 = time.time()
        rewrite_items = _relabel_batch(batch)
        dispatched_batches += 1
        if not rewrite_items:
            print(f"  batch failed (no output), skipping", file=sys.stderr)  # noqa: F541
            continue
        for item in rewrite_items:
            global_idx = batch_start + item["index"]
            orig = cases[global_idx]
            rewritten = dict(orig)
            rewritten["expected_content"] = item["expected"]
            if item["alternates"]:
                rewritten["expected_alternates"] = item["alternates"]
            rewritten["_relabel_origin"] = orig.get("expected_content", "")
            rewrites[global_idx] = rewritten
            total_rewritten += 1
        dt = time.time() - t0
        print(
            f"  rewrote {len(rewrite_items)} items in {dt:.1f}s (running total: {total_rewritten})",
            file=sys.stderr,
        )

    # Build the full v2 list
    v2 = []
    for i in range(len(cases)):
        v2.append(rewrites.get(i, cases[i]))

    summary = {
        "total": len(v2),
        "rewritten": total_rewritten,
        "unchanged": len(v2) - total_rewritten,
        "batches_dispatched": dispatched_batches,
    }

    if args.apply:
        DST.write_text(json.dumps(v2, indent=2, ensure_ascii=False))
        print(f"[eval_relabel] wrote {DST}", file=sys.stderr)
    else:
        print(f"[eval_relabel] DRY RUN — pass --apply to write {DST}", file=sys.stderr)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
