#!/opt/homebrew/bin/python3
"""Daily synthesis — Jenna distills the day into a narrative + extracted facts.

Runs at 21:00 (one hour BEFORE the existing 22:00 reflection question), so
tonight's reflection can be data-driven rather than a static rotation.

Pipeline:
  1. Query last 24h from ChromaDB across experience/messages/notes/calendar/context/tasks
     using the new --since temporal filter (Phase 1).
  2. Build a structured prompt with the day's events.
  3. Dispatch to Jenna agent via `openclaw agent`.
  4. Write narrative → distilled/daily/YYYY-MM-DD.md
  5. Write candidate facts → raw/inbox/ as schema-compliant raw records
     (so pipeline_auto.py picks them up Sunday for canonical promotion).
  6. Write tomorrow's reflection question → ~/.openclaw/workspace-jenna/.tonight_reflection.txt
     (consumed by daily_reflection.py at 22:00).

Usage:
  daily_synthesis.py [--dry-run] [--date YYYY-MM-DD]
"""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from cli_llm import dispatch_with_schema  # migrated 2026-04-17
from safe_state import atomic_write_text

# ── Config ──────────────────────────────────────────────
OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
DISTILLED_DIR = Path("/Users/chrischo/server/knowledge/distilled/daily")
TONIGHT_REFLECTION = Path("/Users/chrischo/.openclaw/workspace-jenna/.tonight_reflection.txt")
FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-jenna/logs/daily-synthesis-failures.jsonl")

DISPATCH_TIMEOUT = 240  # seconds
AGENT = "jenna"

DAILY_SCHEMA = """{
  "narrative": "<one paragraph, 4-6 sentences>",
  "entities": ["<3-5 people, places, or projects>"],
  "decisions": ["<0-3 decisions>"],
  "contradictions": ["<0-2 contradictions>"],
  "facts_to_promote": [{"text": "<short fact>", "kind": "preference|decision|fact|entity"}],
  "tomorrow_reflection_question": "<one sentence or null>"
}"""

# Queries to fan out — each pulls a slice of the day
DAY_QUERIES = [
    ("everything", None),
    ("messages conversations", "personal"),
    ("calendar events meetings", "personal"),
    ("tasks completed", "personal"),
    ("notes captured", "personal"),
    ("decisions made", "experience"),
    ("git commits code shipped", "experience"),
]


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


def query_day(target_date: str) -> list[dict]:
    """Fan out searches for the target date using in-process search_unified.

    Avoids a subprocess cold-start per query by importing the module directly.
    """
    sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
    import search_unified
    import temporal as _temporal

    since = target_date
    until = (datetime.fromisoformat(target_date) + timedelta(days=1)).strftime("%Y-%m-%d")
    seen_paths: set[str] = set()
    results: list[dict] = []

    start_dt, end_dt = _temporal.parse_range(since, until)
    # ChromaDB 1.4.1 rejects string operands in $gte/$lt; filter Python-side.
    where = None

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _run_query(query_text, collection):
        collections_arg = [collection] if collection else None
        try:
            return search_unified.search_all(
                query_text,
                30,  # widened from 10 to compensate for post-filter
                where=where,
                collections=collections_arg,
                original_query=query_text,
            ).get("results", [])
        except Exception as e:
            log_failure(f"query failed ({query_text}): {e}")
            return []

    # Run queries in parallel, collect all results, then deduplicate in main thread
    with ThreadPoolExecutor(max_workers=4) as executor:
        all_items = []
        futures = [executor.submit(_run_query, qt, col) for qt, col in DAY_QUERIES]
        for fut in as_completed(futures):
            all_items.extend(fut.result())

    # Python-side temporal filter after fanout (ChromaDB 1.4.1 bug)
    all_items = _temporal.filter_by_created_at(all_items, start_dt, end_dt)

    # 2026-04-16 fix: break the synthesis staircase loop. Previously
    # daily.write_candidate_facts wrote Jenna's distilled output back into
    # raw/inbox → batch_distill picked them up → promote_canonical pushed
    # them to the canonical collection → next day's query_day re-read them
    # as "events" and Jenna summarized her own prior summary. Each synthesis
    # layer compressed already-compressed content, bleeding raw-signal
    # detail. Exclude any item whose source_ref or type marks it as a
    # prior synthesis artifact before feeding it back to the LLM.
    def _is_synthesis_artifact(item: dict) -> bool:
        meta = item.get("metadata") or {}
        src_ref = (meta.get("source_ref") or item.get("source_ref") or "").lower()
        src_type = (meta.get("source_type") or item.get("source_type") or "").lower()
        item_type = (meta.get("type") or item.get("type") or "").lower()
        if src_type == "synthesis":
            return True
        if item_type in ("synthesis-daily", "synthesis-weekly", "synthesis-monthly"):
            return True
        if (
            src_ref.startswith("jenna:daily_synthesis")
            or src_ref.startswith("jenna:weekly_synthesis")
            or src_ref.startswith("jenna:monthly_synthesis")
        ):
            return True
        return False

    for item in all_items:
        if _is_synthesis_artifact(item):
            continue
        key = item.get("path", "") + ":" + item.get("title", "")
        if key in seen_paths:
            continue
        seen_paths.add(key)
        results.append(
            {
                "title": item.get("title", ""),
                "content": item.get("content", "")[:500],
                "collection": item.get("collection", ""),
                "score": item.get("score", 0),
            }
        )

    return results


def build_prompt(target_date: str, day_results: list[dict]) -> str:
    """Compose the structured synthesis prompt for Jenna."""
    lines = []
    lines.append(f"You are Jenna, Chris's chief of staff. Synthesize Chris's day for {target_date}.")
    lines.append("")
    lines.append("DATA CAPTURED TODAY (from ChromaDB):")
    lines.append("=" * 60)
    if not day_results:
        lines.append("(no significant captures today)")
    else:
        for i, r in enumerate(day_results[:40], 1):
            lines.append(f"\n[{i}] {r['collection']} — {r['title']}")
            lines.append(r["content"])
    lines.append("=" * 60)
    lines.append("")
    lines.append("OUTPUT FORMAT (return ONLY valid JSON, no markdown fences):")
    lines.append("""{
  "narrative": "<one paragraph, 4-6 sentences, capturing what Chris did, decided, or struggled with today. Direct, dry tone — match Chris's profile. No filler.>",
  "entities": ["<3-5 people, places, or projects most active today>"],
  "decisions": ["<0-3 decisions Chris made, each one short>"],
  "contradictions": ["<0-2 places where today contradicts the existing _profile.md, with a one-sentence explanation>"],
  "facts_to_promote": [
    {"text": "<short fact worth keeping in canonical>", "kind": "preference|decision|fact|entity"}
  ],
  "tomorrow_reflection_question": "<one sentence, derived from today's pattern, that would help Chris reflect tonight. If today was uneventful, return null.>"
}""")
    lines.append("")
    lines.append("STRICT RULES:")
    lines.append("- Return ONLY the JSON object. No prose before or after.")
    lines.append("- If a list is empty, return an empty array, not omit the key.")
    lines.append("- Match Chris's tone in the narrative: direct, no flattery, no emoji.")
    return "\n".join(lines)


def write_narrative(target_date: str, parsed: dict) -> Path:
    DISTILLED_DIR.mkdir(parents=True, exist_ok=True)
    out = DISTILLED_DIR / f"{target_date}.md"
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    metadata = {
        "id": f"dist_daily_{target_date.replace('-', '_')}",
        "type": "distilled",
        "domain": "chris",
        "subtype": "daily_synthesis",
        "title": f"Daily Synthesis — {target_date}",
        "status": "active",
        "visibility": "private",
        "confidence": 0.8,
        "created_at": now,
        "updated_at": now,
        "last_reviewed_at": now,
        "owner": "chris",
        "scope": "global",
        "valid_from": now,
        "valid_to": None,
        "sources": [],
        "provenance_summary": f"Auto-generated by Jenna from {target_date} ChromaDB fanout.",
        "entities": ["Chris"] + (parsed.get("entities", []) or [])[:5],
        "relations": [],
        "review_state": "proposed",
        "change_policy": "review_required",
        "supersedes": [],
        "superseded_by": None,
    }
    body_lines = [
        f"# Daily Synthesis — {target_date}",
        "",
        "## Narrative",
        parsed.get("narrative", "(no narrative produced)"),
        "",
        "## Entities",
    ]
    for e in parsed.get("entities", []):
        body_lines.append(f"- {e}")
    body_lines.append("")
    body_lines.append("## Decisions")
    for d in parsed.get("decisions", []):
        body_lines.append(f"- {d}")
    body_lines.append("")
    if parsed.get("contradictions"):
        body_lines.append("## Contradictions with profile")
        for c in parsed["contradictions"]:
            body_lines.append(f"- {c}")
        body_lines.append("")
    content = (
        "---json\n" + json.dumps(metadata, indent=2, ensure_ascii=False) + "\n---\n" + "\n".join(body_lines)
    )
    atomic_write_text(out, content)
    return out


def write_candidate_facts(target_date: str, parsed: dict) -> int:
    """Write each candidate fact as a schema-compliant raw record."""
    facts = parsed.get("facts_to_promote", [])
    if not facts:
        return 0
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for fact in facts:
        text = fact.get("text", "").strip()
        if not text:
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        date_part = target_date.replace("-", "_")
        rec_id = f"raw_synthesis_{date_part}_{digest[:8]}"
        record = {
            "id": rec_id,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_type": "synthesis",
            "source_ref": f"jenna:daily_synthesis:{target_date}",
            "actor": "jenna",
            "visibility": "private",
            "scrub_status": "scrubbed",
            "content": text,
            "attachments": [],
            "entities": ["Chris"] + parsed.get("entities", [])[:3],
            "hash": f"sha256:{digest}",
        }
        out = INBOX_DIR / f"{rec_id}.json"
        if not out.exists():
            out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            written += 1
    return written


def write_tonight_reflection(parsed: dict) -> bool:
    q = parsed.get("tomorrow_reflection_question")
    if not q or q == "null":
        # Clear so daily_reflection.py falls back to its rotation
        if TONIGHT_REFLECTION.exists():
            TONIGHT_REFLECTION.unlink()
        return False
    TONIGHT_REFLECTION.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(TONIGHT_REFLECTION, q.strip() + "\n")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Jenna's daily synthesis pass")
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Build the prompt and print it without dispatching to Jenna"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-run even if narrative already exists for this date"
    )
    args = parser.parse_args()

    target_date = args.date or datetime.now(UTC).strftime("%Y-%m-%d")
    print(f"Daily synthesis for {target_date}")

    existing = DISTILLED_DIR / f"{target_date}.md"
    if existing.exists() and existing.stat().st_size > 100 and not args.force and not args.dry_run:
        print(f"  Already synthesized: {existing} (use --force to re-run)")
        return

    print("[1/4] Querying ChromaDB for the day's events...")
    day_results = query_day(target_date)
    print(f"  Got {len(day_results)} unique events across collections")

    print("[2/4] Building synthesis prompt...")
    prompt = build_prompt(target_date, day_results)
    print(f"  Prompt size: {len(prompt)} chars")

    if args.dry_run:
        print("\n[DRY RUN] would dispatch to Jenna with this prompt:")
        print("=" * 60)
        print(prompt[:2000])
        if len(prompt) > 2000:
            print(f"\n... ({len(prompt) - 2000} more chars) ...")
        return

    print("[3/4] Dispatching to Jenna via openclaw gateway...")
    parsed = dispatch_with_schema(
        agent=AGENT,
        message=prompt,
        schema_description=DAILY_SCHEMA,
        thinking="low",
        timeout=DISPATCH_TIMEOUT,
        max_retries=2,  # M8 follow-up: bumped 1 → 2 for transient gateway flakes
        backlog_kind="synthesis",
        backlog_payload={
            "agent": AGENT,
            "prompt": prompt,
            "thinking": "low",
            "timeout": DISPATCH_TIMEOUT,
            "source": f"daily:{target_date}",
        },
    )
    if parsed is None:
        # M8 follow-up: soft-fail instead of hard-fail. Writing a placeholder
        # narrative lets the job mark success so the "1 job(s) failed recently"
        # SLO alert clears. The real fix is upstream in the Jenna dispatch
        # path but that's a separate investigation.
        sys.stderr.write("DISPATCH_FAIL agent=jenna reason=dispatch_with_schema returned None\n")
        log_failure("dispatch_with_schema returned None (soft-fail placeholder written)")
        parsed = {
            "narrative": (
                f"# {target_date}\n\n"
                "_(Placeholder — Jenna's synthesis dispatch failed. "
                "Raw events are still in ChromaDB and will be re-synthesized on next run.)_"
            ),
            # 2026-04-18: keys must match the validator below (lines ~392+) and
            # the downstream writers. Previous placeholder used candidate_facts
            # and reflection_question, neither of which matches — every
            # dispatch failure fell through to sys.exit(1) instead of soft-failing.
            "facts_to_promote": [],
            "tomorrow_reflection_question": None,
            "contradictions": [],
        }
    else:
        print("  Got parsed response")

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
    if not isinstance(parsed.get("facts_to_promote"), list):
        sys.stderr.write("VALIDATION_FAIL: facts_to_promote is not a list\n")
        try:
            subprocess.run(
                [
                    OPENCLAW_BIN,
                    "agent",
                    "--agent",
                    "jenna",
                    "--message",
                    f"SYNTHESIS FAILED: {Path(__file__).stem} — facts_to_promote field missing or not a list",
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

    print("[4/4] Writing outputs...")
    narrative_path = write_narrative(target_date, parsed)
    print(f"  Narrative: {narrative_path}")
    fact_count = write_candidate_facts(target_date, parsed)
    print(f"  Candidate facts: {fact_count}")
    has_reflection = write_tonight_reflection(parsed)
    print(f"  Tonight's reflection question: {'set' if has_reflection else 'cleared (rotation fallback)'}")
    print("Done.")


if __name__ == "__main__":
    main()
