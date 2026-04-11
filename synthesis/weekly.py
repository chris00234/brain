#!/opt/homebrew/bin/python3
"""Weekly synthesis — Sage produces Chris's weekly arc.

Reads the 7 most recent daily syntheses + last week's canonical decisions,
dispatches to Sage via openclaw, writes the result as a canonical weekly arc.
Runs Sunday 04:00 PST.

Usage:
  weekly_synthesis.py [--dry-run] [--week YYYY-Www]
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from safe_state import atomic_write_text  # noqa: E402
from openclaw_dispatch import dispatch_with_schema  # noqa: E402

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
DISTILLED_DAILY = Path("/Users/chrischo/server/knowledge/distilled/daily")
WEEKLY_OUT = Path("/Users/chrischo/server/knowledge/canonical/chris/weekly")
CANONICAL_DECISIONS = Path("/Users/chrischo/server/knowledge/canonical/decisions")
IDENTITY = Path("/Users/chrischo/server/knowledge/canonical/chris/_identity.md")
STATE = Path("/Users/chrischo/server/knowledge/canonical/chris/_state.md")
FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-sage/logs/weekly-synthesis-failures.jsonl")

DISPATCH_TIMEOUT = 360
AGENT = "sage"

WEEKLY_SCHEMA = """{
  "title": "<one-sentence title>",
  "did": ["<3-7 things>"],
  "decided": ["<0-5 decisions>"],
  "struggled": ["<0-3 struggles>"],
  "patterns": ["<0-4 patterns>"],
  "contradictions": ["<0-3 contradictions>"],
  "hypotheses": ["<1-3 hypotheses>"],
  "open_questions": ["<1-3 questions>"],
  "narrative": "<paragraph, ~6-8 sentences>"
}"""


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


def collect_week(target_week: str) -> dict:
    """Collect last 7 days of daily syntheses + recent decisions for the prompt."""
    # target_week is like "2026-W14"; resolve to its Monday
    year, w = target_week.split("-W")
    monday = datetime.strptime(f"{year}-W{w}-1", "%G-W%V-%u")
    days = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    daily_notes = []
    for d in days:
        f = DISTILLED_DAILY / f"{d}.md"
        if f.exists():
            daily_notes.append({"date": d, "content": f.read_text()[:3000]})

    recent_decisions = []
    if CANONICAL_DECISIONS.exists():
        cutoff = monday - timedelta(days=14)
        for f in sorted(CANONICAL_DECISIONS.glob("*.md")):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
                    recent_decisions.append({"file": f.name, "content": f.read_text()[:1500]})
            except Exception:
                continue

    profile_parts = []
    if IDENTITY.exists():
        profile_parts.append(IDENTITY.read_text())
    if STATE.exists():
        profile_parts.append(STATE.read_text())
    profile_text = "\n\n".join(profile_parts)
    return {
        "days": days,
        "daily_notes": daily_notes,
        "recent_decisions": recent_decisions,
        "profile_excerpt": profile_text[:3000],
    }


def build_prompt(week: str, data: dict) -> str:
    lines = []
    lines.append(f"You are Sage, Chris's research and synthesis agent.")
    lines.append(f"Produce Chris's weekly arc for {week} ({data['days'][0]} to {data['days'][-1]}).")
    lines.append("")
    lines.append("=" * 60)
    lines.append("DAILY SYNTHESES (Jenna's nightly outputs):")
    lines.append("=" * 60)
    if not data["daily_notes"]:
        lines.append("(no daily notes for this week)")
    else:
        for d in data["daily_notes"]:
            lines.append(f"\n--- {d['date']} ---")
            lines.append(d["content"])
    lines.append("")
    lines.append("=" * 60)
    lines.append("RECENT CANONICAL DECISIONS (last 14 days):")
    lines.append("=" * 60)
    for d in data["recent_decisions"][:10]:
        lines.append(f"\n--- {d['file']} ---")
        lines.append(d["content"])
    lines.append("")
    lines.append("=" * 60)
    lines.append("CHRIS PROFILE EXCERPT:")
    lines.append("=" * 60)
    lines.append(data["profile_excerpt"])
    lines.append("")
    lines.append("=" * 60)
    lines.append("OUTPUT FORMAT (return ONLY valid JSON, no markdown fences):")
    lines.append("""{
  "title": "<one-sentence title for the week>",
  "did": ["<3-7 most concrete things Chris did this week>"],
  "decided": ["<0-5 decisions Chris made this week>"],
  "struggled": ["<0-3 things Chris struggled with or kept revisiting>"],
  "patterns": ["<0-4 productive or anti-patterns visible in the week>"],
  "contradictions": ["<0-3 places this week's data contradicts the profile, with reasoning>"],
  "hypotheses": ["<1-3 hypotheses about Chris's state, goals, or trajectory>"],
  "open_questions": ["<1-3 questions worth surfacing to Chris next week>"],
  "narrative": "<one paragraph weaving the above into a coherent arc, ~6-8 sentences, in Chris's tone — direct, no flattery>"
}""")
    lines.append("")
    lines.append("STRICT RULES:")
    lines.append("- Return ONLY the JSON object. No prose before or after.")
    lines.append("- Empty lists are allowed. Do not omit keys.")
    lines.append("- Match Chris's profile tone: direct, dry, no emoji, no sycophancy.")
    return "\n".join(lines)


def write_arc(week: str, data: dict, parsed: dict) -> Path:
    WEEKLY_OUT.mkdir(parents=True, exist_ok=True)
    out = WEEKLY_OUT / f"{week}.md"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record = {
        "id": f"chris_weekly_arc_{week.replace('-', '_').lower()}",
        "type": "canonical",
        "domain": "chris",
        "subtype": "weekly_arc",
        "title": parsed.get("title", f"Chris weekly arc {week}"),
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
        "sources": [f"distilled/daily/{d['date']}.md" for d in data["daily_notes"]],
        "provenance_summary": "Auto-generated by Sage from the week's daily syntheses + recent canonical decisions.",
        "entities": ["Chris", "Sage"],
        "relations": [{"type": "informs", "target": "chris_profile"}],
        "review_state": "proposed",
        "change_policy": "review_required",
        "supersedes": [],
        "superseded_by": None,
    }
    body = ["---json", json.dumps(record, indent=2), "---", "",
            f"# {record['title']}", "",
            "## Did this week"]
    body += [f"- {x}" for x in parsed.get("did", [])]
    body += ["", "## Decided"] + [f"- {x}" for x in parsed.get("decided", [])]
    body += ["", "## Struggled with"] + [f"- {x}" for x in parsed.get("struggled", [])]
    body += ["", "## Patterns observed"] + [f"- {x}" for x in parsed.get("patterns", [])]
    if parsed.get("contradictions"):
        body += ["", "## Contradictions with profile"] + [f"- {x}" for x in parsed["contradictions"]]
    body += ["", "## Hypotheses"] + [f"- {x}" for x in parsed.get("hypotheses", [])]
    body += ["", "## Open questions for next week"] + [f"- {x}" for x in parsed.get("open_questions", [])]
    body += ["", "## Narrative", "", parsed.get("narrative", "(no narrative)")]
    atomic_write_text(out, "\n".join(body))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Sage's weekly synthesis pass")
    parser.add_argument("--week", default=None, help="ISO week like 2026-W14 (default: this week)")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt without dispatching")
    parser.add_argument("--force", action="store_true", help="Re-run even if weekly arc already exists")
    args = parser.parse_args()

    target_week = args.week or datetime.now().strftime("%G-W%V")
    print(f"Weekly synthesis for {target_week}")

    existing = WEEKLY_OUT / f"{target_week}.md"
    if existing.exists() and existing.stat().st_size > 100 and not args.force and not args.dry_run:
        print(f"  Already synthesized: {existing} (use --force to re-run)")
        return

    print("[1/4] Collecting week's daily notes + recent decisions...")
    data = collect_week(target_week)
    print(f"  daily notes: {len(data['daily_notes'])}, recent decisions: {len(data['recent_decisions'])}")

    print("[2/4] Building prompt...")
    prompt = build_prompt(target_week, data)
    print(f"  prompt size: {len(prompt)} chars")

    if args.dry_run:
        print("\n[DRY RUN] would dispatch to Sage. First 1500 chars:")
        print("=" * 60)
        print(prompt[:1500])
        return

    print("[3/4] Dispatching to Sage...")
    parsed = dispatch_with_schema(
        agent=AGENT,
        message=prompt,
        schema_description=WEEKLY_SCHEMA,
        thinking="low",
        timeout=DISPATCH_TIMEOUT,
        max_retries=1,
    )
    if parsed is None:
        sys.stderr.write("DISPATCH_FAIL agent=sage reason=dispatch_with_schema returned None\n")
        log_failure("dispatch_with_schema returned None")
        try:
            subprocess.run([
                OPENCLAW_BIN, "agent",
                "--agent", "jenna",
                "--message", f"SYNTHESIS FAILED: {Path(__file__).stem} — dispatch_with_schema returned None",
                "--thinking", "off", "--timeout", "30",
            ], timeout=35, capture_output=True)
        except Exception:
            pass
        sys.exit(1)

    if not isinstance(parsed.get("narrative"), str):
        sys.stderr.write("VALIDATION_FAIL: narrative is not a string\n")
        try:
            subprocess.run([
                OPENCLAW_BIN, "agent",
                "--agent", "jenna",
                "--message", f"SYNTHESIS FAILED: {Path(__file__).stem} — narrative field missing or not a string",
                "--thinking", "off", "--timeout", "30",
            ], timeout=35, capture_output=True)
        except Exception:
            pass
        sys.exit(1)
    if not isinstance(parsed.get("did"), list):
        sys.stderr.write("VALIDATION_FAIL: did is not a list\n")
        try:
            subprocess.run([
                OPENCLAW_BIN, "agent",
                "--agent", "jenna",
                "--message", f"SYNTHESIS FAILED: {Path(__file__).stem} — did field missing or not a list",
                "--thinking", "off", "--timeout", "30",
            ], timeout=35, capture_output=True)
        except Exception:
            pass
        sys.exit(1)

    print("[4/4] Writing weekly arc...")
    out = write_arc(target_week, data, parsed)
    print(f"  Wrote: {out}")
    print("Done.")


if __name__ == "__main__":
    main()
