#!/Users/chrischo/server/brain/.venv/bin/python
"""Drift taxonomy for trend-track eval failures (Contract 10).

Classifies failing per-test rows from `eval_compare.py --json
--include-per-test` output into durable drift classes by checking the
expected source/phrase against the live knowledge tree:

  zero_results            v2 returned nothing (strict governance surface)
  loose_only_paraphrase   right source, loose content hit — fixture phrase
                          brittle; candidate for expected_alternates
  phrase_only_in_archive  expected phrase survives only in canonical/archived
                          or knowledge/obsolete — truth superseded, fixture debt
  stale_expected_phrase   expected phrase exists nowhere in live knowledge —
                          fixture expects vanished truth
  phrase_live_ranking_miss expected phrase IS live in canonical/distilled but
                          retrieval missed top-5 — genuine retrieval gap

  flags: vanished_expected_source (path-shaped expected_source with no file
  on disk), archived_expected_source (file only under archived/obsolete).

READ-ONLY reporting: never edits eval sets; emits JSON (and --markdown) so
fixture corrections stay explicit, reviewed changes with evidence.

Usage:
  eval_drift_taxonomy.py REPORT.json --track default [--markdown OUT.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

KNOWLEDGE_ROOT = Path("/Users/chrischo/server/knowledge")
LIVE_DIRS = ("canonical", "distilled")
ARCHIVE_MARKERS = ("canonical/archived", "obsolete")


@lru_cache(maxsize=4)
def _corpus(knowledge_root: Path) -> list[tuple[str, str]]:
    """(relative_path, lowercased_text) for every md file under live dirs +
    archives. Loaded once per root; ~50MB worst case, fine for a CLI run."""
    docs: list[tuple[str, str]] = []
    for sub in (*LIVE_DIRS, "obsolete"):
        base = knowledge_root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            try:
                docs.append((str(p.relative_to(knowledge_root)), p.read_text(errors="ignore").lower()))
            except OSError:
                continue
    return docs


def _is_archived(rel_path: str) -> bool:
    return any(marker in rel_path for marker in ARCHIVE_MARKERS)


def _phrase_locations(phrase: str, knowledge_root: Path) -> tuple[list[str], list[str]]:
    """Live and archived file paths containing the lowercased phrase."""
    needle = phrase.lower().strip()
    if not needle:
        return [], []
    live, archived = [], []
    for rel, text in _corpus(knowledge_root):
        if needle in text:
            (archived if _is_archived(rel) else live).append(rel)
    return live, archived


def _looks_like_path(expected_source: str) -> bool:
    return "/" in expected_source or expected_source.endswith(".md")


def _source_on_disk(expected_source: str, knowledge_root: Path) -> tuple[bool, bool]:
    """(exists_anywhere, only_in_archive) for a path-shaped expected_source."""
    rel = expected_source.strip().lstrip("/")
    if (knowledge_root / rel).exists():
        return True, _is_archived(rel)
    basename = Path(rel).name.lower()
    hits = [doc_rel for doc_rel, _ in _corpus(knowledge_root) if Path(doc_rel).name.lower() == basename]
    if not hits:
        return False, False
    return True, all(_is_archived(h) for h in hits)


def classify_case(case: dict, *, knowledge_root: Path = KNOWLEDGE_ROOT) -> dict:
    """Primary drift class + flags + evidence for one failing per-test row."""
    flags: list[str] = []
    evidence: list[str] = []

    expected_source = str(case.get("expected_source") or "")
    if _looks_like_path(expected_source):
        exists, only_archived = _source_on_disk(expected_source, knowledge_root)
        if not exists:
            flags.append("vanished_expected_source")
        elif only_archived:
            flags.append("archived_expected_source")

    if not case.get("top_sources"):
        primary = "zero_results"
    elif case.get("hit_source") and case.get("hit_content_loose") and not case.get("hit_content"):
        primary = "loose_only_paraphrase"
    elif not str(case.get("expected_content") or "").strip():
        # Content auto-passes when the fixture has no expected phrase, so the
        # case can only have failed on source retrieval.
        primary = "source_only_miss"
    else:
        phrase = str(case.get("expected_content") or "")
        live, archived = _phrase_locations(phrase, knowledge_root)
        if live:
            primary = "phrase_live_ranking_miss"
            evidence.extend(f"live:{p}" for p in live[:3])
        elif archived:
            primary = "phrase_only_in_archive"
            evidence.extend(f"archived:{p}" for p in archived[:3])
        else:
            primary = "stale_expected_phrase"

    return {
        "query": case.get("query"),
        "expected_source": expected_source,
        "expected_content": case.get("expected_content"),
        "primary_class": primary,
        "flags": flags,
        "evidence": evidence,
    }


def classify_report(report_path: Path | str, *, track: str, knowledge_root: Path = KNOWLEDGE_ROOT) -> dict:
    report = json.loads(Path(report_path).read_text())
    per_test = report.get("v2", {}).get("per_test") or []
    failures = [t for t in per_test if not (t.get("hit_source") and t.get("hit_content"))]
    classified = [classify_case(t, knowledge_root=knowledge_root) for t in failures]
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "track": track,
        "total": len(per_test),
        "failures": len(failures),
        "classes": dict(Counter(c["primary_class"] for c in classified)),
        "flag_counts": dict(Counter(f for c in classified for f in c["flags"])),
        "cases": classified,
    }


def _markdown(out: dict) -> str:
    lines = [
        f"# Eval drift taxonomy — track `{out['track']}` ({out['generated_at']})",
        "",
        f"{out['failures']} failing of {out['total']} cases.",
        "",
        "| class | count |",
        "|---|---|",
    ]
    lines += [f"| {cls} | {n} |" for cls, n in sorted(out["classes"].items(), key=lambda kv: -kv[1])]
    if out["flag_counts"]:
        lines += ["", "| flag | count |", "|---|---|"]
        lines += [f"| {f} | {n} |" for f, n in sorted(out["flag_counts"].items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", help="eval_compare --json --include-per-test output file")
    parser.add_argument("--track", required=True, help="Track label (default/extended/train/holdout)")
    parser.add_argument("--knowledge-root", default=str(KNOWLEDGE_ROOT))
    parser.add_argument("--markdown", help="Also write a markdown summary here")
    parser.add_argument("--json-out", help="Write the full JSON report here (default: stdout)")
    args = parser.parse_args()

    out = classify_report(args.report, track=args.track, knowledge_root=Path(args.knowledge_root))
    rendered = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(rendered)
        print(f"wrote {args.json_out}: {out['failures']} failures -> {out['classes']}")
    else:
        print(rendered)
    if args.markdown:
        Path(args.markdown).write_text(_markdown(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
