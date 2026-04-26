#!/Users/chrischo/server/brain/.venv/bin/python3
"""cli/canonical_quality_triage.py — LLM triage for score=2 quality-flagged canonicals.

canonical_quality_filter.py flags notes with heuristic score ≥ 2. Score=3
items are high-confidence noise (session logs, commit JSON) and are
auto-archived. Score=2 items are borderline: may be legitimate preference
notes OR boilerplate masquerading as knowledge. This script uses CLI LLM
(codex, subscription) to make that call per item.

Verdicts:
- archive: clearly session/audit noise, safe to move to canonical/archived/
- keep: real knowledge (preference, decision, fact), keep in canonical
- uncertain: leave alone, surface for human review

Safety:
- Dry-run by default. --apply invokes canonical_quality_filter.py on a
  per-file basis for `archive` verdicts (reversible — moves to archived/).
- Only processes `archive_candidates` from the latest quality report with
  score exactly 2 (score ≥ 3 is handled by the underlying filter).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

try:
    from cli_llm import cli_dispatch_with_schema

    from config import BRAIN_DIR, KNOWLEDGE_DIR
except ImportError:
    BRAIN_DIR = Path("/Users/chrischo/server/brain")
    KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")
    cli_dispatch_with_schema = None  # type: ignore[assignment]

REPORTS_DIR = KNOWLEDGE_DIR / "reports" / "canonical_quality"
AUTO_CONFIDENCE_THRESHOLD = 0.8
MAX_ITEMS_PER_RUN = 40

SCHEMA = (
    '{"verdict": string (one of "archive", "keep", "uncertain"), '
    '"confidence": number (0.0-1.0), '
    '"reason": string (max 200 chars)}'
)

PROMPT_TEMPLATE = """You are triaging a canonical knowledge note in Chris Cho's personal brain system. The note was flagged by a heuristic quality filter with score=2 (borderline — could be real knowledge or could be session/audit noise). Decide.

Canonical notes SHOULD contain:
- Durable preferences ("Chris prefers TypeScript strict mode")
- Architectural decisions ("Adopted two-track eval on 2026-04-13 because...")
- Infrastructure facts ("ChromaDB port is 8000")
- Identity / role definitions
- Learned lessons that generalize

Canonical notes SHOULD NOT contain:
- Raw session transcripts ("OpenClaw jenna session 2026-04-01")
- Browser history rows ("Browser visit (chrome) URL: ...")
- Git commit metadata dumps ("author Chris Cho body ...")
- One-off audit trails with no reusable knowledge
- Summaries with sentinel boilerplate "Review this proposed canonical note"

Note to triage:
  id: {note_id}
  title: {title}
  domain: {domain}
  flagged signals: {signals}
  body preview (first 800 chars): {preview}

Verdict:
- `archive`: this is session/audit noise, move it out of canonical
- `keep`: this captures durable generalizable knowledge, keep in canonical
- `uncertain`: genuinely ambiguous — leave for human review

Confidence reflects how certain you are of your verdict."""


def _parse_latest_report() -> list[dict]:
    """Read newest canonical_quality/*.md and pull score=2 entries."""
    if not REPORTS_DIR.exists():
        return []
    reports = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
    if not reports:
        return []
    text = reports[0].read_text()
    pattern = re.compile(r"- `([^`]+)` — \*\*([^*]+)\*\* — score (\d+) — ([^—]+) — `(canonical/[^`]+)`")
    items: list[dict] = []
    for m in pattern.finditer(text):
        score = int(m.group(3))
        if score != 2:
            continue
        items.append(
            {
                "id": m.group(1),
                "title": m.group(2).strip(),
                "score": score,
                "signals": m.group(4).strip(),
                "path": m.group(5),
            }
        )
    return items


def _load_note_preview(rel_path: str, limit: int = 800) -> tuple[str, str]:
    """Return (domain, body_preview) from canonical/<rel>. Strips frontmatter."""
    path = KNOWLEDGE_DIR / rel_path
    if not path.exists():
        return "", ""
    text = path.read_text()
    # Strip ---json ... --- frontmatter
    body = re.sub(r"^---json\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    body = re.sub(r"^---\n.*?\n---\n", "", body, count=1, flags=re.DOTALL)
    body = body.strip()[:limit]
    # Parse domain from path: canonical/<domain>/<slug>.md
    parts = Path(rel_path).parts
    domain = parts[1] if len(parts) >= 2 else "unknown"
    return domain, body


def _triage_one(item: dict) -> dict:
    domain, preview = _load_note_preview(item["path"])
    if cli_dispatch_with_schema is None:
        return {"verdict": "uncertain", "confidence": 0.0, "reason": "cli_llm unavailable"}
    prompt = PROMPT_TEMPLATE.format(
        note_id=item["id"],
        title=item["title"],
        domain=domain,
        signals=item["signals"],
        preview=preview or "(empty body)",
    )
    result = cli_dispatch_with_schema(prompt, schema_description=SCHEMA, timeout=30)
    if result is None:
        return {"verdict": "uncertain", "confidence": 0.0, "reason": "LLM error"}
    return result


def _archive_one(rel_path: str) -> bool:
    """Move canonical/<domain>/<slug>.md → canonical/archived/<domain>/<slug>.md,
    rewriting status: active → archived in frontmatter."""
    src = KNOWLEDGE_DIR / rel_path
    if not src.exists():
        return False
    parts = Path(rel_path).parts
    if len(parts) < 3 or parts[0] != "canonical":
        return False
    dst = KNOWLEDGE_DIR / "canonical" / "archived" / parts[1] / parts[-1]
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text()
    text = re.sub(r'"status":\s*"active"', '"status": "archived"', text, count=1)
    # Add archived_at before closing } of frontmatter
    now = datetime.now(UTC).isoformat(timespec="seconds")
    text = re.sub(
        r"(\n)(\}\n---)",
        rf'\1  ,"archived_at": "{now}", "archived_reason": "llm_triage_score_2"\n\2',
        text,
        count=1,
    )
    dst.write_text(text)
    src.unlink()
    return True


def run(apply_changes: bool = False, limit: int = MAX_ITEMS_PER_RUN) -> dict:
    items = _parse_latest_report()
    if not items:
        return {"status": "ok", "found": 0, "note": "no score=2 items in latest report"}
    items = items[:limit]
    archived: list[dict] = []
    kept: list[dict] = []
    uncertain: list[dict] = []
    for item in items:
        verdict = _triage_one(item)
        conf = float(verdict.get("confidence") or 0)
        entry = {
            "id": item["id"],
            "path": item["path"],
            "verdict": verdict.get("verdict", "uncertain"),
            "confidence": round(conf, 3),
            "reason": (verdict.get("reason") or "")[:200],
        }
        if entry["verdict"] == "archive" and conf >= AUTO_CONFIDENCE_THRESHOLD:
            if apply_changes:
                _archive_one(item["path"])
            archived.append(entry)
        elif entry["verdict"] == "keep" and conf >= AUTO_CONFIDENCE_THRESHOLD:
            kept.append(entry)
        else:
            uncertain.append(entry)
    return {
        "status": "ok",
        "scanned": len(items),
        "archived": len(archived),
        "kept": len(kept),
        "uncertain": len(uncertain),
        "auto_threshold": AUTO_CONFIDENCE_THRESHOLD,
        "dry_run": not apply_changes,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "details": {
            "archived": archived[:10],
            "kept": kept[:10],
            "uncertain": uncertain[:10],
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=MAX_ITEMS_PER_RUN)
    args = p.parse_args()
    print(json.dumps(run(apply_changes=args.apply, limit=args.limit), indent=2, ensure_ascii=False))
