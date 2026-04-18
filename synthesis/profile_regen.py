#!/opt/homebrew/bin/python3
"""Regenerate canonical/chris/_state.md from current canonical knowledge.

Dispatches to Sage with all canonical chris/* notes + key decisions,
asks for a structured STATE snapshot (projects, tools, focus) — NOT identity.
Identity lives in _identity.md and is immutable; this job only touches _state.md.

Usage:
  profile_regen.py [--dry-run]
"""

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
try:
    from config import BRAIN_LOGS_DIR, KNOWLEDGE_DIR, OPENCLAW_BIN
except ImportError:
    KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

from cli_llm import dispatch  # migrated 2026-04-17
from common import parse_note, render_note
from safe_state import atomic_write_text

STATE_PATH = KNOWLEDGE_DIR / "canonical" / "chris" / "_state.md"
CANONICAL_DIR = KNOWLEDGE_DIR / "canonical"
DISPATCH_TIMEOUT = 300

PROMPT_TEMPLATE = """You are Sage. Chris's canonical knowledge base has been updated.
Regenerate Chris's STATE snapshot based on the canonical notes below.

This is the mutable state document only — current projects, tools, focus, recent signals.
DO NOT emit identity, hard rules, or values — those live in _identity.md and are immutable.

OUTPUT FORMAT: Return ONLY the markdown body (no JSON frontmatter, no fences).
Use the EXACT section structure:
## Tools and stack
## Workflow preferences
## Active projects
## Agent ecosystem & routing
## Hard infrastructure facts
## Patterns observed
## Standing operational notes
## Current state

Keep each section concise. Lead with facts, not prose. Use bullet points.
Preserve existing content that is still accurate. Update anything contradicted by newer notes.

CURRENT STATE:
{current_state}

CANONICAL NOTES ({note_count} total):
{notes}

Return the updated state body now:"""


def collect_canonical_notes():
    notes = []
    skip_names = {"_profile.md", "_identity.md", "_state.md"}
    for md in CANONICAL_DIR.rglob("*.md"):
        if md.name in skip_names:
            continue
        try:
            meta, body = parse_note(md)
            domain = meta.get("domain", "")
            title = meta.get("title", md.stem)
            notes.append(
                {
                    "text": f"[{domain}] {title}\n{body[:300]}",
                    "updated_at": meta.get("updated_at", ""),
                }
            )
        except Exception:
            continue
    return notes


def dispatch_to_sage(prompt):
    result = dispatch(
        agent="sage",
        message=prompt,
        thinking="medium",
        timeout=DISPATCH_TIMEOUT,
        backlog_kind="synthesis",
        backlog_payload={
            "agent": "sage",
            "prompt": prompt,
            "thinking": "medium",
            "timeout": DISPATCH_TIMEOUT,
            "source": "profile_regen",
        },
    )
    if not result.ok:
        print(f"  ERROR: Sage dispatch failed: {result.error[:200]}", file=sys.stderr)
        return None
    text = result.text.strip()
    text = re.sub(r"^```(?:markdown)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"[profile_regen] {datetime.now().isoformat(timespec='seconds')}")

    current_state = ""
    current_meta = {}
    if STATE_PATH.exists():
        try:
            current_meta, current_state = parse_note(STATE_PATH)
        except Exception:
            current_state = STATE_PATH.read_text()

    notes = collect_canonical_notes()
    print(f"  Collected {len(notes)} canonical notes")

    notes.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    notes_text = "\n\n---\n\n".join(n["text"] for n in notes[:50])
    prompt = PROMPT_TEMPLATE.format(
        current_state=current_state[:3000],
        note_count=len(notes),
        notes=notes_text[:8000],
    )

    if args.dry_run:
        print(f"  [DRY RUN] Would dispatch {len(prompt)} chars to Sage")
        return

    print("  Dispatching to Sage...")
    new_body = dispatch_to_sage(prompt)
    if not new_body or len(new_body) < 200:
        print("  ERROR: Sage returned empty/short state, aborting", file=sys.stderr)
        try:
            subprocess.run(
                [
                    OPENCLAW_BIN,
                    "agent",
                    "--agent",
                    "jenna",
                    "--message",
                    f"SYNTHESIS FAILED: {Path(__file__).stem} — Sage returned empty/short state",
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

    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    current_meta.update(
        {
            "updated_at": now,
            "last_reviewed_at": now,
            "provenance_summary": f"Auto-regenerated by Sage from {len(notes)} canonical notes on {now}",
        }
    )

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        import shutil

        shutil.copy2(STATE_PATH, STATE_PATH.with_suffix(".bak"))
    atomic_write_text(STATE_PATH, render_note(current_meta, new_body))
    print(f"  Written {len(new_body)} chars to {STATE_PATH}")


if __name__ == "__main__":
    main()
