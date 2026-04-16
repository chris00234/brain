#!/opt/homebrew/bin/python3
"""Canonical quality filter — archive audit-log / session-log noise.

Round 2 Step 5. Distinguishes real knowledge notes from session/audit logs
that shouldn't live in the main canonical layer. Uses heuristic scoring on
frontmatter + title + body, reports candidates in dry-run, and optionally
archives them to `canonical/archived/<domain>/<slug>.md` with
`status: archived`.

Heuristic signals (each contributes 1 point):
  S1: provenance_summary starts with "Distilled from openclaw_session"
      / "Distilled from browser" / "Distilled from shell" / "Distilled from synthesis"
  S2: title matches "OpenClaw <agent> session" / "Browser visit" /
      "From: <email>" / "Shell session " / "Claude Code session"
  S3: body length < 200 chars
  S4: subtype in ("audit-log", "session-log", "audit_log", "session_log")
  S5: provenance has no meaningful rich content ("Review this proposed
      canonical note" sentinel)

Archive threshold: score ≥ 2 fires archival candidacy.

Usage:
  canonical_quality_filter.py [--dry-run] [--apply] [--threshold 2]

Default mode is dry-run. --apply physically moves files into
canonical/archived/<original-domain>/ with updated frontmatter (adds
archived_at, archived_reason, sets status: archived).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from common import ROOT, parse_note, render_note, iter_note_paths  # noqa: E402

CANONICAL_DIR = ROOT / "canonical"
ARCHIVE_DIR = CANONICAL_DIR / "archived"
REPORT_DIR = ROOT / "reports" / "canonical_quality"

SKIP_NAMES = {"index.md", "_index.md", "_identity.md", "_state.md", "_profile.md"}
SKIP_DIRS = {"entities", "archived"}  # never archive entity pages or re-archive
DEFAULT_THRESHOLD = 2
BODY_MIN_CHARS = 200

TITLE_PATTERNS = [
    re.compile(r"^openclaw\s+\w+\s+session", re.I),
    re.compile(r"^browser\s+visit", re.I),
    re.compile(r"^from:", re.I),
    re.compile(r"^shell\s+session", re.I),
    re.compile(r"^claude\s+code\s+session", re.I),
    re.compile(r"^.*_received_at.*_event.*", re.I),
    re.compile(r'^\{"author":', re.I),
    re.compile(r'^\{"_received_at', re.I),
    re.compile(r'^\{"generated_by', re.I),
    re.compile(r'^\{"event":', re.I),
    re.compile(r'^\s*\{.*"memory_count"', re.I),
]
PROV_AUDIT_PREFIXES = (
    "distilled from openclaw_session",
    "distilled from browser",
    "distilled from shell",
    "distilled from synthesis",
    "distilled from git",
)
AUDIT_SUBTYPES = {"audit-log", "session-log", "audit_log", "session_log"}
REVIEW_SENTINEL = "review this proposed canonical note"


def _score_note(meta: dict, body: str) -> tuple[int, list[str]]:
    signals: list[str] = []

    prov = (meta.get("provenance_summary") or "").strip().lower()
    if prov and any(prov.startswith(p) for p in PROV_AUDIT_PREFIXES):
        signals.append("S1_prov_distilled_from_session")

    title = (meta.get("title") or "").strip()
    if any(pat.search(title) for pat in TITLE_PATTERNS):
        signals.append("S2_title_matches_audit_pattern")

    if len(body.strip()) < BODY_MIN_CHARS:
        signals.append("S3_body_too_short")

    if (meta.get("subtype") or "").strip().lower() in AUDIT_SUBTYPES:
        signals.append("S4_subtype_audit")

    if REVIEW_SENTINEL in prov or REVIEW_SENTINEL in body.lower()[:400]:
        signals.append("S5_review_sentinel_boilerplate")

    return len(signals), signals


def _load_candidates() -> list[dict]:
    out = []
    for path in iter_note_paths(CANONICAL_DIR):
        if path.name in SKIP_NAMES or path.name.endswith(".bak"):
            continue
        parts = path.relative_to(CANONICAL_DIR).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        try:
            meta, body = parse_note(path)
        except Exception:
            continue
        if meta.get("type") != "canonical":
            continue
        if meta.get("status") != "active":
            continue
        score, signals = _score_note(meta, body)
        out.append({
            "path": path,
            "rel_path": str(path.relative_to(ROOT)),
            "id": meta.get("id") or path.stem,
            "title": (meta.get("title") or path.stem)[:120],
            "domain": meta.get("domain") or "other",
            "subtype": meta.get("subtype") or "",
            "score": score,
            "signals": signals,
            "body_chars": len(body.strip()),
        })
    return out


def _write_report(candidates: list[dict], threshold: int, all_count: int) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    json_path = REPORT_DIR / f"{date}.json"
    md_path = REPORT_DIR / f"{date}.md"

    flagged = [c for c in candidates if c["score"] >= threshold]
    keep = [c for c in candidates if c["score"] < threshold]

    by_domain: dict[str, list[dict]] = {}
    for c in flagged:
        by_domain.setdefault(c["domain"], []).append(c)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_scanned": all_count,
        "threshold": threshold,
        "flagged_count": len(flagged),
        "keep_count": len(keep),
        "by_domain": {d: len(items) for d, items in by_domain.items()},
        "flagged": [
            {k: v for k, v in c.items() if k != "path"} for c in flagged
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    lines = [
        f"# Canonical Quality Filter — {date}",
        "",
        f"_Generated {payload['generated_at']}_",
        "",
        f"- Total active canonical notes scanned: **{all_count}**",
        f"- Flagged for archival (score ≥ {threshold}): **{len(flagged)}**",
        f"- Kept as knowledge: **{len(keep)}**",
        "",
        "## Flagged by domain",
        "",
    ]
    for d in sorted(by_domain):
        lines.append(f"### {d} ({len(by_domain[d])})")
        lines.append("")
        for c in by_domain[d][:50]:
            sigs = ", ".join(c["signals"])
            lines.append(f"- `{c['id']}` — **{c['title']}** — score {c['score']} — {sigs} — `{c['rel_path']}`")
        if len(by_domain[d]) > 50:
            lines.append(f"- _… {len(by_domain[d]) - 50} more_")
        lines.append("")

    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def _archive_note(candidate: dict) -> dict:
    """Move a note to canonical/archived/<original-domain>/<basename>.md with status=archived."""
    src = candidate["path"]
    meta, body = parse_note(src)
    meta["status"] = "archived"
    meta["archived_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    meta["archived_reason"] = "quality_filter:" + ",".join(candidate["signals"])
    original_domain = candidate["domain"]
    dest_dir = ARCHIVE_DIR / original_domain
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    # Preserve frontmatter updates
    dest.write_text(render_note(meta, body))
    # Delete original only after successful write
    src.unlink()
    return {"from": str(src.relative_to(ROOT)), "to": str(dest.relative_to(ROOT))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="(default) scan + report only")
    parser.add_argument("--apply", action="store_true", help="physically archive flagged notes")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    if args.apply and args.dry_run:
        print("cannot pass both --apply and --dry-run", file=sys.stderr)
        return 2

    candidates = _load_candidates()
    json_path, md_path = _write_report(candidates, args.threshold, len(candidates))
    flagged = [c for c in candidates if c["score"] >= args.threshold]

    if not args.apply:
        print(json.dumps({
            "status": "dry-run",
            "scanned": len(candidates),
            "flagged": len(flagged),
            "report_md": str(md_path.relative_to(ROOT)),
            "report_json": str(json_path.relative_to(ROOT)),
        }))
        return 0

    # Apply mode — move each flagged note
    moved: list[dict] = []
    errors = 0
    for c in flagged:
        try:
            moved.append(_archive_note(c))
        except Exception as e:
            errors += 1
            print(f"  archive error on {c['rel_path']}: {e}", file=sys.stderr)

    print(json.dumps({
        "status": "applied",
        "scanned": len(candidates),
        "moved": len(moved),
        "errors": errors,
        "report_md": str(md_path.relative_to(ROOT)),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
