#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_mine_canonical.py — Reverse-mine canonical notes into retrieval eval questions.

For each markdown file in ~/server/knowledge/canonical/, dispatch to Jenna
(OpenClaw) with a prompt that generates 2-3 natural-language questions the note
should answer. Writes JSON-lines to /tmp/brain_eval_mine_canonical.jsonl for
the validate/merge pipeline to consume.

Usage:
  eval_mine_canonical.py [--limit N] [--output PATH] [--agent jenna]

Output format (one JSON object per line):
  {"query": "...", "expected_source": "<rel-path>", "expected_content": "...", "collection": "all"}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from openclaw_dispatch import dispatch_with_schema  # noqa: E402

CANONICAL_ROOT = Path("/Users/chrischo/server/knowledge/canonical")
DEFAULT_OUTPUT = Path("/tmp/brain_eval_mine_canonical.jsonl")

PROMPT_TEMPLATE = """Generate 2-3 natural-language retrieval questions that a user would plausibly ask that SHOULD return this canonical note as a top-5 result.

Rules:
- Mix literal keyword-matching questions with paraphrased/semantic questions.
- One question in natural Korean if the topic is personal, project-level, or involves Chris directly.
- Length 3-15 words per question.
- Do NOT paraphrase the note title verbatim.
- The expected_content field must be a 3-6 word substring that literally appears in the note body.

Note relative path: {rel_path}
Note title: {title}
Note body:
{body}

Return ONLY this JSON shape (no prose, no fences):
"""

SCHEMA = """{
  "questions": [
    {
      "query": "<natural language question, 3-15 words>",
      "expected_source": "<the relative path provided above, verbatim>",
      "expected_content": "<3-6 word substring that literally appears in the note body>",
      "collection": "all"
    }
  ]
}"""


def extract_frontmatter_title_and_body(text: str) -> tuple[str, str]:
    """Return (title, body_without_frontmatter). Handles both --- YAML and ---json blocks."""
    stripped = text.lstrip()
    title = ""
    body = stripped

    if stripped.startswith("---"):
        import re as _re
        m = _re.match(r"^---(?:json)?\s*\n(.*?)\n---\s*\n", stripped, _re.DOTALL)
        if m:
            fm_raw = m.group(1)
            body = stripped[m.end():]
            # Extract title from either YAML or JSON frontmatter
            title_match = _re.search(r'"?title"?\s*[:=]\s*"([^"]+)"', fm_raw)
            if title_match:
                title = title_match.group(1)
            else:
                yaml_match = _re.search(r"^title:\s*(.+)$", fm_raw, _re.MULTILINE)
                if yaml_match:
                    title = yaml_match.group(1).strip().strip('"').strip("'")

    # Fallback: first H1/H2 heading in body
    if not title:
        for line in body.splitlines()[:30]:
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
            if s.startswith("## "):
                title = s[3:].strip()
                break

    return title, body


def mine_note(md_path: Path, rel_path: str, agent: str) -> list[dict]:
    """Run reverse-mining on a single note. Returns list of question dicts, possibly empty."""
    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"  SKIP {rel_path}: read failed: {e}", file=sys.stderr)
        return []

    title, body = extract_frontmatter_title_and_body(text)
    if len(body.strip()) < 100:
        return []  # too short to mine meaningfully

    prompt = PROMPT_TEMPLATE.format(
        rel_path=rel_path,
        title=title or "(no title)",
        body=body[:2500],
    )

    try:
        result = dispatch_with_schema(
            agent=agent,
            message=prompt,
            schema_description=SCHEMA,
            thinking="low",
            timeout=90,
            max_retries=1,
        )
    except Exception as e:
        print(f"  DISPATCH_FAIL {rel_path}: {e}", file=sys.stderr)
        return []

    if not result or "questions" not in result:
        return []

    questions = result.get("questions") or []
    out = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        query = (q.get("query") or "").strip()
        expected_source = (q.get("expected_source") or rel_path).strip()
        expected_content = (q.get("expected_content") or "").strip()
        if not query or len(query) < 5 or len(query) > 200:
            continue
        out.append({
            "query": query,
            "expected_source": expected_source,
            "expected_content": expected_content,
            "collection": q.get("collection") or "all",
        })
    return out


def _load_checkpoint(output_path: Path) -> set[str]:
    """Return the set of rel_paths already mined (present in expected_source of
    the existing output file). Used for resume-on-restart so a crash doesn't
    strand the remaining notes.
    """
    if not output_path.exists():
        return set()
    seen: set[str] = set()
    try:
        for line in output_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                src = entry.get("expected_source", "").strip()
                if src:
                    seen.add(src)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description="Reverse-mine canonical notes into eval questions")
    parser.add_argument("--limit", type=int, default=0, help="Only mine first N files (0 = all)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--agent", default="jenna", help="OpenClaw agent to dispatch to")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore existing output file; re-mine from scratch")
    args = parser.parse_args()

    if not CANONICAL_ROOT.exists():
        print(f"FATAL: canonical root not found at {CANONICAL_ROOT}", file=sys.stderr)
        return 2

    md_files = sorted(CANONICAL_ROOT.rglob("*.md"))
    if args.limit > 0:
        md_files = md_files[:args.limit]

    # Checkpoint: skip notes already present in the output file unless --fresh
    already_mined = set()
    if not args.fresh:
        already_mined = _load_checkpoint(args.output)
        if already_mined:
            print(f"checkpoint: {len(already_mined)} notes already mined, will skip and append new ones")

    print(f"mining {len(md_files)} canonical notes via {args.agent}"
          f" ({len(already_mined)} already done, ~{len(md_files) - len(already_mined)} to go)...")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Append mode when checkpoint exists, write mode when --fresh
    mode = "a" if already_mined and not args.fresh else "w"
    out_f = args.output.open(mode)
    total_out = 0
    skipped = 0
    t_start = time.time()

    for i, md_path in enumerate(md_files, 1):
        rel = str(md_path.relative_to(CANONICAL_ROOT.parent))  # e.g. "canonical/foo.md"
        if rel in already_mined:
            skipped += 1
            continue
        t_note = time.time()
        questions = mine_note(md_path, rel, args.agent)
        for q in questions:
            out_f.write(json.dumps(q, ensure_ascii=False) + "\n")
            total_out += 1
        out_f.flush()
        dt = time.time() - t_note
        elapsed = time.time() - t_start
        print(f"  [{i}/{len(md_files)}] {rel[:50]} → {len(questions)} qs ({dt:.1f}s, total {total_out}, elapsed {elapsed:.0f}s)")

    out_f.close()
    print(f"\nDONE — wrote {total_out} new questions, skipped {skipped} already-mined")
    print(f"Output: {args.output}")
    print(f"Total time: {time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
