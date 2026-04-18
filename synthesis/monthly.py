#!/opt/homebrew/bin/python3
"""Monthly synthesis — Sage produces Chris's longitudinal arc.

Reads the 4 most recent weekly arcs + the previous monthly arc + the current
profile, dispatches to Sage, writes a canonical monthly arc that captures how
Chris's state, projects, and patterns have evolved.

Runs 1st of each month at 05:00 PST.

Usage:
  monthly_synthesis.py [--dry-run] [--month YYYY-MM]
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from cli_llm import dispatch_with_schema  # migrated 2026-04-17
from safe_state import atomic_write_text

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
WEEKLY_DIR = Path("/Users/chrischo/server/knowledge/canonical/chris/weekly")
MONTHLY_DIR = Path("/Users/chrischo/server/knowledge/canonical/chris/monthly")
IDENTITY = Path("/Users/chrischo/server/knowledge/canonical/chris/_identity.md")
STATE = Path("/Users/chrischo/server/knowledge/canonical/chris/_state.md")
FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-sage/logs/monthly-synthesis-failures.jsonl")
TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_ACCOUNT = "jenna-bot"

DISPATCH_TIMEOUT = 480
AGENT = "sage"

MONTHLY_SCHEMA = """{
  "title": "<one-sentence title>",
  "values_evolved": ["<0-3 values shifts>"],
  "goals_progressed": ["<0-3 goals>"],
  "goals_abandoned": ["<0-2 dropped>"],
  "relationships": ["<0-3 shifts>"],
  "mood_trend": "<rising|stable|declining|mixed>",
  "energy_trend": "<rising|stable|declining|mixed>",
  "chapter_advance": "<one sentence>",
  "longitudinal_patterns": ["<2-4 patterns>"],
  "hypotheses_for_next_month": ["<2-4 hypotheses>"],
  "narrative": "<2 paragraphs, ~10-12 sentences>"
}"""


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


def collect_month(target_month: str) -> dict:
    """target_month is YYYY-MM. Returns the 4 most recent weekly arcs + prev monthly + profile."""
    weekly_files = sorted(WEEKLY_DIR.glob("*.md"))[-4:] if WEEKLY_DIR.exists() else []
    weeklies = [{"name": f.name, "content": f.read_text()[:5000]} for f in weekly_files]

    prev_monthly = None
    if MONTHLY_DIR.exists():
        existing = sorted(MONTHLY_DIR.glob("*.md"))
        # Pick the previous month's file if present
        for f in reversed(existing):
            if f.stem != target_month:
                prev_monthly = {"name": f.name, "content": f.read_text()[:4000]}
                break

    profile_parts = []
    if IDENTITY.exists():
        profile_parts.append(IDENTITY.read_text())
    if STATE.exists():
        profile_parts.append(STATE.read_text())
    profile_text = "\n\n".join(profile_parts)
    return {
        "weeklies": weeklies,
        "prev_monthly": prev_monthly,
        "profile": profile_text[:4000],
    }


def build_prompt(month: str, data: dict) -> str:
    lines = []
    lines.append("You are Sage, Chris's longitudinal synthesis agent.")
    lines.append(f"Produce Chris's monthly arc for {month}.")
    lines.append("")
    lines.append("=" * 60)
    lines.append("RECENT WEEKLY ARCS (most recent 4):")
    lines.append("=" * 60)
    if not data["weeklies"]:
        lines.append("(no weekly arcs yet — first month)")
    for w in data["weeklies"]:
        lines.append(f"\n--- {w['name']} ---")
        lines.append(w["content"])
    lines.append("")
    lines.append("=" * 60)
    lines.append("PREVIOUS MONTHLY ARC:")
    lines.append("=" * 60)
    if data["prev_monthly"]:
        lines.append(f"--- {data['prev_monthly']['name']} ---")
        lines.append(data["prev_monthly"]["content"])
    else:
        lines.append("(no previous monthly arc)")
    lines.append("")
    lines.append("=" * 60)
    lines.append("CURRENT CHRIS PROFILE EXCERPT:")
    lines.append("=" * 60)
    lines.append(data["profile"])
    lines.append("")
    lines.append("=" * 60)
    lines.append("OUTPUT FORMAT (return ONLY valid JSON, no markdown fences):")
    lines.append("""{
  "title": "<one-sentence title for the month>",
  "values_evolved": ["<0-3 places Chris's values shifted, with before→after>"],
  "goals_progressed": ["<0-3 goals that moved forward>"],
  "goals_abandoned": ["<0-2 goals dropped or paused>"],
  "relationships": ["<0-3 relationships that shifted in strength or sentiment>"],
  "mood_trend": "<rising|stable|declining|mixed>",
  "energy_trend": "<rising|stable|declining|mixed>",
  "chapter_advance": "<one sentence: did Chris move into a new life chapter this month? What changed?>",
  "longitudinal_patterns": ["<2-4 patterns visible across the month that weren't visible weekly>"],
  "hypotheses_for_next_month": ["<2-4 hypotheses to watch>"],
  "narrative": "<2 paragraphs, ~10-12 sentences total. Direct, dry, no flattery. Focus on EVOLUTION — what changed, not just what happened.>"
}""")
    lines.append("")
    lines.append("STRICT RULES:")
    lines.append("- Return ONLY the JSON object. No prose before or after.")
    lines.append("- Empty lists are allowed; do not omit keys.")
    lines.append("- Match Chris's profile tone.")
    return "\n".join(lines)


def telegram_alert(month: str, out_path: Path) -> None:
    msg = f"🧠 Monthly arc ready: {month}\n\nNew canonical note: {out_path.name}\nReview when you have a moment."
    try:
        subprocess.run(
            [
                OPENCLAW_BIN,
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                TELEGRAM_CHAT_ID,
                "--account",
                TELEGRAM_ACCOUNT,
                "--message",
                msg,
            ],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        pass


def write_arc(month: str, data: dict, parsed: dict) -> Path:
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    out = MONTHLY_DIR / f"{month}.md"
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    record = {
        "id": f"chris_monthly_arc_{month.replace('-', '_')}",
        "type": "canonical",
        "domain": "chris",
        "subtype": "monthly_arc",
        "title": parsed.get("title", f"Chris monthly arc {month}"),
        "status": "active",
        "visibility": "private",
        "confidence": 0.85,
        "created_at": now,
        "updated_at": now,
        "last_reviewed_at": now,
        "owner": "chris",
        "scope": "global",
        "valid_from": now,
        "valid_to": None,
        "sources": [f"canonical/chris/weekly/{w['name']}" for w in data["weeklies"]],
        "provenance_summary": "Auto-generated by Sage from the month's weekly arcs + previous monthly arc + profile.",
        "entities": ["Chris", "Sage"],
        "relations": [{"type": "informs", "target": "chris_profile"}],
        "review_state": "proposed",
        "change_policy": "review_required",
        "supersedes": [],
        "superseded_by": None,
    }
    body = [
        "---json",
        json.dumps(record, indent=2),
        "---",
        "",
        f"# {record['title']}",
        "",
        "## Values evolved",
    ]
    body += [f"- {x}" for x in parsed.get("values_evolved", [])]
    body += ["", "## Goals progressed"] + [f"- {x}" for x in parsed.get("goals_progressed", [])]
    body += ["", "## Goals abandoned"] + [f"- {x}" for x in parsed.get("goals_abandoned", [])]
    body += ["", "## Relationships"] + [f"- {x}" for x in parsed.get("relationships", [])]
    body += ["", f"## Mood trend: {parsed.get('mood_trend', '?')}"]
    body += ["", f"## Energy trend: {parsed.get('energy_trend', '?')}"]
    body += ["", "## Chapter advance", parsed.get("chapter_advance", "(none)")]
    body += ["", "## Longitudinal patterns"] + [f"- {x}" for x in parsed.get("longitudinal_patterns", [])]
    body += ["", "## Hypotheses for next month"] + [
        f"- {x}" for x in parsed.get("hypotheses_for_next_month", [])
    ]
    body += ["", "## Narrative", "", parsed.get("narrative", "(no narrative)")]
    atomic_write_text(out, "\n".join(body))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Sage's monthly synthesis pass")
    parser.add_argument("--month", default=None, help="Month YYYY-MM (default: previous month)")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt without dispatching")
    parser.add_argument("--force", action="store_true", help="Re-run even if monthly arc already exists")
    args = parser.parse_args()

    # Default to previous month (we run on the 1st, summarizing last month)
    if args.month:
        target_month = args.month
    else:
        now = datetime.now()
        if now.month == 1:
            target_month = f"{now.year - 1}-12"
        else:
            target_month = f"{now.year}-{now.month - 1:02d}"

    print(f"Monthly synthesis for {target_month}")

    existing = MONTHLY_DIR / f"{target_month}.md"
    if existing.exists() and existing.stat().st_size > 100 and not args.force and not args.dry_run:
        print(f"  Already synthesized: {existing} (use --force to re-run)")
        return

    print("[1/4] Collecting weekly arcs + previous monthly + profile...")
    data = collect_month(target_month)
    print(f"  weeklies: {len(data['weeklies'])}, prev_monthly: {bool(data['prev_monthly'])}")

    print("[2/4] Building prompt...")
    prompt = build_prompt(target_month, data)
    print(f"  prompt size: {len(prompt)} chars")

    if args.dry_run:
        print("\n[DRY RUN] first 1500 chars of prompt:")
        print("=" * 60)
        print(prompt[:1500])
        return

    print("[3/4] Dispatching to Sage...")
    parsed = dispatch_with_schema(
        agent=AGENT,
        message=prompt,
        schema_description=MONTHLY_SCHEMA,
        thinking="low",
        timeout=DISPATCH_TIMEOUT,
        max_retries=1,
        backlog_kind="synthesis",
        backlog_payload={
            "agent": AGENT,
            "prompt": prompt,
            "thinking": "low",
            "timeout": DISPATCH_TIMEOUT,
            "source": "monthly",
        },
    )
    if parsed is None:
        sys.stderr.write("DISPATCH_FAIL agent=sage reason=dispatch_with_schema returned None\n")
        log_failure("dispatch_with_schema returned None")
        try:
            subprocess.run(
                [
                    OPENCLAW_BIN,
                    "agent",
                    "--agent",
                    "jenna",
                    "--message",
                    f"SYNTHESIS FAILED: {Path(__file__).stem} — dispatch_with_schema returned None",
                    "--thinking",
                    "off",
                    "--timeout",
                    "30",
                ],
                timeout=35,
                capture_output=True,
            )
        except Exception:
            pass
        sys.exit(1)

    if not isinstance(parsed.get("narrative"), str):
        sys.stderr.write("VALIDATION_FAIL: narrative is not a string\n")
        try:
            subprocess.run(
                [
                    OPENCLAW_BIN,
                    "agent",
                    "--agent",
                    "jenna",
                    "--message",
                    f"SYNTHESIS FAILED: {Path(__file__).stem} — narrative field missing or not a string",
                    "--thinking",
                    "off",
                    "--timeout",
                    "30",
                ],
                timeout=35,
                capture_output=True,
            )
        except Exception:
            pass
        sys.exit(1)
    if not isinstance(parsed.get("longitudinal_patterns"), list):
        sys.stderr.write("VALIDATION_FAIL: longitudinal_patterns is not a list\n")
        try:
            subprocess.run(
                [
                    OPENCLAW_BIN,
                    "agent",
                    "--agent",
                    "jenna",
                    "--message",
                    f"SYNTHESIS FAILED: {Path(__file__).stem} — longitudinal_patterns field missing or not a list",
                    "--thinking",
                    "off",
                    "--timeout",
                    "30",
                ],
                timeout=35,
                capture_output=True,
            )
        except Exception:
            pass
        sys.exit(1)

    print("[4/4] Writing monthly arc...")
    out = write_arc(target_month, data, parsed)
    print(f"  Wrote: {out}")
    telegram_alert(target_month, out)
    print("Done.")


if __name__ == "__main__":
    main()
