#!/opt/homebrew/bin/python3
"""Automated canonical pipeline — ingest agent learnings, distill, propose, auto-promote.

Usage:
  pipeline_auto.py [--dry-run] [--auto-promote-threshold 75] [--reject-threshold 42]
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Code now lives at /server/brain/pipeline/; data is at /server/knowledge/.
SCRIPTS_DIR = Path(__file__).parent  # /server/brain/pipeline
sys.path.insert(0, str(SCRIPTS_DIR))
from common import (  # noqa: E402
    find_similar_canonical,
    parse_markdown_frontmatter,
    slugify,
    utc_now,
    write_markdown_frontmatter,
)

ROOT = Path("/Users/chrischo/server/knowledge")  # data tree
REVIEW_QUEUE_DIR = ROOT / "review_queue"
AGENTS_DIR = Path("/Users/chrischo/.openclaw")
STATE_FILE = ROOT / ".pipeline_state.json"  # state tracks data, so lives with data
DIGEST_FILE = ROOT / "reports" / "weekly-digest.md"
try:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from config import PYTHON
except ImportError:
    PYTHON = "/Users/chrischo/server/brain/.venv/bin/python"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"scanned_files": {}, "last_run": None}


def save_state(state):
    state["last_run"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def scan_new_content(state):
    new_entries = []
    scanned = state.get("scanned_files", {})
    cutoff = datetime.now() - timedelta(days=7)

    for agent_dir in (AGENTS_DIR / "agents").iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        ws = AGENTS_DIR / f"workspace-{agent}"
        if not ws.exists():
            continue

        learnings_dir = ws / ".learnings"
        if learnings_dir.exists():
            for f in learnings_dir.glob("*.md"):
                fkey = str(f)
                current_size = f.stat().st_size
                if scanned.get(fkey) == current_size:
                    continue
                content = f.read_text()
                if len(content.strip()) < 100:
                    continue
                entries = re.split(r"\n(?=\d{4}-\d{2}-\d{2}|#{1,3}\s+)", content)
                for entry in entries:
                    entry = entry.strip()
                    if len(entry) > 50:
                        new_entries.append(
                            {
                                "content": entry[:2000],
                                "source_file": fkey,
                                "agent": agent,
                                "source_type": "agent_learning",
                            }
                        )
                scanned[fkey] = current_size

        mem_dir = ws / "memory"
        if mem_dir.exists():
            for f in sorted(mem_dir.glob("*.md"), reverse=True):
                if f.name.startswith("archive"):
                    continue
                try:
                    file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                    if file_date < cutoff:
                        break
                except ValueError:
                    continue
                fkey = str(f)
                current_size = f.stat().st_size
                if scanned.get(fkey) == current_size:
                    continue
                content = f.read_text()
                if len(content.strip()) < 100:
                    continue
                new_entries.append(
                    {
                        "content": content[:2000],
                        "source_file": fkey,
                        "agent": agent,
                        "source_type": "session_memory",
                    }
                )
                scanned[fkey] = current_size

    state["scanned_files"] = scanned
    return new_entries


def run_script(script_name, args_list, dry_run=False):
    cmd = [PYTHON, str(SCRIPTS_DIR / script_name)] + args_list
    if dry_run:
        print(f"  [DRY RUN] Would run: {' '.join(cmd)}")
        return {"status": "dry_run"}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(SCRIPTS_DIR))
        if result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"status": "ok", "raw": result.stdout[:500]}
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def ingest_entries(entries, dry_run=False):
    ingested = 0
    for entry in entries:
        result = run_script(
            "ingest.py",
            [
                "--content",
                entry["content"],
                "--source-type",
                entry["source_type"],
                "--source-ref",
                f"{entry['source_type']}:{entry['agent']}:{entry['source_file']}",
                "--actor",
                entry["agent"],
            ],
            dry_run=dry_run,
        )
        if result.get("status") != "error":
            ingested += 1
    return ingested


def main():
    parser = argparse.ArgumentParser(description="Automated Canonical Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done")
    parser.add_argument("--auto-promote-threshold", type=int, default=75)
    parser.add_argument("--reject-threshold", type=int, default=42)
    args = parser.parse_args()

    print("=" * 60)
    print(f"Canonical Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if args.dry_run:
        print("[DRY RUN MODE]")
    print("=" * 60)

    state = load_state()
    canonical_before = len(list((ROOT / "canonical").rglob("*.md")))

    print("\n[1/6] Scanning agent learnings and memory files...")
    new_entries = scan_new_content(state)
    print(f"  Found {len(new_entries)} new entries")

    # Always proceed to batch_distill even with 0 scanner entries —
    # other sources (brain_reflect, ingest adapters) write directly to raw/inbox.
    ingested = 0
    if new_entries:
        print("\n[2/6] Ingesting to raw/inbox/...")
        ingested = ingest_entries(new_entries, args.dry_run)
        print(f"  Ingested {ingested} entries")
    else:
        print("\n[2/6] No new scanner entries — checking raw/inbox for external records...")

    print("\n[3/6] Batch distilling...")
    distill_result = run_script("batch_distill.py", [], args.dry_run)
    if distill_result.get("status") == "error":
        print(f"  ERROR: batch_distill.py failed: {distill_result.get('error', 'unknown')}")
        return  # don't save state — next run will re-scan
    distill_created = distill_result.get("created_count", 0)
    print(f"  Created {distill_created} distilled notes")

    # Always proceed to propose/score/promote — they're idempotent.
    # Prior early-exit here dropped pending distilled notes that had no proposals yet.

    print("\n[4/6] Batch proposing...")
    propose_result = run_script("batch_propose.py", [], args.dry_run)
    if propose_result.get("status") == "error":
        print(f"  ERROR: batch_propose.py failed: {propose_result.get('error', 'unknown')}")
        return  # don't save state — next run will re-scan
    propose_created = propose_result.get("created_count", 0)
    print(f"  Created {propose_created} proposals")

    print("\n[5/6] Scoring proposals...")
    score_result = run_script("score_proposals.py", [], args.dry_run)
    if score_result.get("status") == "error":
        print(f"  WARNING: score_proposals.py failed: {score_result.get('error', 'unknown')}")
    items = score_result.get("items", [])

    promoted = []
    rejected = []
    held = []

    review_queue = ROOT / "reports" / "review-queue"
    rejected_dir = review_queue / "rejected"

    for item in items:
        score = item.get("score", 0)
        proposal_path = ROOT / item.get("path", "")
        if not item.get("path") or not proposal_path.exists():
            continue

        if score >= args.auto_promote_threshold:
            if not args.dry_run and proposal_path.exists():
                metadata, body = parse_markdown_frontmatter(proposal_path)
                metadata["review_state"] = "confirmed"
                metadata["status"] = "active"
                metadata["id"] = metadata["id"].replace("proposal_", "", 1)
                file_name = slugify(metadata.get("title", metadata["id"])) + ".md"
                target = ROOT / "canonical" / metadata.get("domain", "projects") / file_name
                target.parent.mkdir(parents=True, exist_ok=True)
                # Dedup: merge into existing canonical if similar
                existing = find_similar_canonical(metadata.get("title", ""), body)
                if existing and existing != target:
                    ex_meta, ex_body = parse_markdown_frontmatter(existing)
                    ex_meta["sources"] = list(set(ex_meta.get("sources", []) + metadata.get("sources", [])))
                    ex_meta["updated_at"] = utc_now()
                    if len(body) > len(ex_body):
                        ex_body = body
                    write_markdown_frontmatter(existing, ex_meta, ex_body)
                    item["merged_into"] = existing.name
                else:
                    write_markdown_frontmatter(target, metadata, body)
                if target.exists() or (existing and existing.exists()):
                    proposal_path.unlink()
                promoted.append(item)
            else:
                promoted.append(item)

        elif score < args.reject_threshold:
            if not args.dry_run and proposal_path.exists():
                rejected_dir.mkdir(parents=True, exist_ok=True)
                proposal_path.rename(rejected_dir / proposal_path.name)
                rejected.append(item)
            else:
                rejected.append(item)
        else:
            # Held for human review — write to review_queue/ with pending status
            if not args.dry_run and proposal_path.exists():
                REVIEW_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                metadata, body = parse_markdown_frontmatter(proposal_path)
                metadata["review_status"] = "pending"
                metadata["scored_at"] = utc_now()
                metadata["pipeline_score"] = score
                file_name = proposal_path.name
                target = REVIEW_QUEUE_DIR / file_name
                write_markdown_frontmatter(target, metadata, body)
                proposal_path.unlink()
            held.append(item)

    # Count total pending in review queue (includes prior runs)
    pending_review = 0
    if REVIEW_QUEUE_DIR.exists():
        for rq_file in REVIEW_QUEUE_DIR.glob("*.md"):
            try:
                rq_meta, _ = parse_markdown_frontmatter(rq_file)
                if rq_meta.get("review_status") == "pending":
                    pending_review += 1
            except Exception:
                continue

    print(f"  Promoted: {len(promoted)} | Held for review: {len(held)} | Rejected: {len(rejected)}")
    print(f"  Review queue: {pending_review} pending")

    print("\n[6/6] Writing digest...")
    canonical_after = len(list((ROOT / "canonical").rglob("*.md")))

    digest_lines = [
        f"# Weekly Pipeline Digest — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**Scanned:** {len(new_entries)} new entries from agent learnings/memory",
        f"**Ingested:** {ingested} | **Distilled:** {distill_created} | **Proposed:** {propose_created}",
        f"**Canonical notes:** {canonical_before} -> {canonical_after}",
        f"**Review queue:** {pending_review} pending",
        "",
        "## Promoted to Canonical",
    ]
    for item in promoted:
        digest_lines.append(
            f"- [{item.get('domain', '?')}] {item.get('title', '?')} (score: {item.get('score', 0)})"
        )
    if not promoted:
        digest_lines.append("- None")

    digest_lines.append("")
    digest_lines.append("## Held for Review")
    for item in held:
        digest_lines.append(
            f"- [{item.get('domain', '?')}] {item.get('title', '?')} (score: {item.get('score', 0)})"
        )
    if not held:
        digest_lines.append("- None")

    digest_lines.append("")
    digest_lines.append("## Auto-Rejected")
    for item in rejected:
        reasons = ", ".join(item.get("reasons", []))
        digest_lines.append(
            f"- [{item.get('domain', '?')}] {item.get('title', '?')} (score: {item.get('score', 0)}, reasons: {reasons})"
        )
    if not rejected:
        digest_lines.append("- None")

    if not args.dry_run:
        DIGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        DIGEST_FILE.write_text("\n".join(digest_lines) + "\n")
        print(f"  Digest: {DIGEST_FILE}")

    # Move old raw/inbox files (>30 days) to raw/orphaned/ for quarantine
    # instead of hard-deleting. Matches maintenance.py prune_raw_inbox behavior
    # so daily pipeline + weekly maintenance can't produce divergent outcomes.
    if not args.dry_run:
        inbox = ROOT / "raw" / "inbox"
        orphaned = ROOT / "raw" / "orphaned"
        orphaned.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        cleaned = 0
        for f in inbox.glob("*.json"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, tz=UTC) < cutoff:
                    dest = orphaned / f.name
                    # If a file of the same name already exists in orphaned,
                    # append an epoch suffix so we don't overwrite prior quarantines.
                    if dest.exists():
                        dest = orphaned / f"{f.stem}.{int(f.stat().st_mtime)}.json"
                    f.rename(dest)
                    cleaned += 1
            except Exception:
                pass
        if cleaned:
            print(f"  Quarantined {cleaned} old inbox files (>30d) to raw/orphaned/")

    # Write pipeline trace log (append-only JSONL for end-to-end observability)
    if not args.dry_run:
        trace = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "scanned": len(new_entries),
            "ingested": ingested,
            "distilled": distill_created,
            "proposed": propose_created,
            "promoted": len(promoted),
            "held": len(held),
            "rejected": len(rejected),
            "pending_review": pending_review,
            "canonical_before": canonical_before,
            "canonical_after": canonical_after,
            "promoted_items": [
                {"domain": i.get("domain"), "title": i.get("title"), "score": i.get("score")}
                for i in promoted
            ],
        }
        trace_log = ROOT / "reports" / "pipeline-trace.jsonl"
        trace_log.parent.mkdir(parents=True, exist_ok=True)
        with trace_log.open("a") as f:
            f.write(json.dumps(trace) + "\n")

    save_state(state)

    # Chain reindex if anything changed so new canonical notes become searchable
    # immediately (without waiting for the next scheduled reindex).
    if not args.dry_run and (distill_created or promoted):
        secret_file = Path.home() / ".openclaw/credentials/.personal_webhook_secret"
        if secret_file.exists():
            try:
                import urllib.request

                req = urllib.request.Request(
                    "http://127.0.0.1:8791/jobs/reindex",
                    method="POST",
                    headers={"Authorization": f"Bearer {secret_file.read_text().strip()}"},
                )
                urllib.request.urlopen(req, timeout=5).read()
                print("  Triggered reindex (async).")
            except Exception as e:
                print(f"  WARN: reindex trigger failed: {e}")

    print("\n" + "=" * 60)
    print("Pipeline complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
