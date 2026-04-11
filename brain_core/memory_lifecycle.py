#!/opt/homebrew/bin/python3
"""Memory file lifecycle — archive old agent memory files, extract insights first.

Phase 4d: before archiving a memory file, dispatch it to Liz via openclaw agent
for canonical-worthy fact extraction. Each extracted fact becomes a schema-compliant
raw record in raw/inbox/, picked up by pipeline_auto.py for promotion to canonical.
This stops the 180-day archive cliff from being a brain-damage event.

Usage:
  memory_lifecycle.py [--archive-age 90] [--delete-age 180] [--dry-run] [--no-extract]
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


try:
    from config import OPENCLAW_DIR as AGENTS_DIR, INBOX_DIR, OPENCLAW_BIN
    EXTRACTION_FAILURE_LOG = AGENTS_DIR / "workspace-liz" / "logs" / "memory-extraction-failures.jsonl"
except ImportError:
    AGENTS_DIR = Path("/Users/chrischo/.openclaw")
    INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
    EXTRACTION_FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-liz/logs/memory-extraction-failures.jsonl")
LIZ_DISPATCH_TIMEOUT = 180
EXTRACTION_AGENT = "liz"


def get_memory_files():
    files = []
    for agent_dir in (AGENTS_DIR / "agents").iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        mem_dir = AGENTS_DIR / f"workspace-{agent}" / "memory"
        if not mem_dir.exists():
            continue
        for f in mem_dir.glob("*.md"):
            if f.name == "working-buffer.md" or f.parent.name == "archive":
                continue
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                files.append((f, agent, file_date))
            except ValueError:
                continue
    return files


def get_archived_files():
    files = []
    for agent_dir in (AGENTS_DIR / "agents").iterdir():
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        archive_dir = AGENTS_DIR / f"workspace-{agent}" / "memory" / "archive"
        if not archive_dir.exists():
            continue
        for f in archive_dir.glob("*.md"):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                files.append((f, agent, file_date))
            except ValueError:
                continue
    return files


def log_extraction_failure(reason: str) -> None:
    try:
        EXTRACTION_FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EXTRACTION_FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


def extract_via_liz(memory_file: Path) -> int:
    """Dispatch a memory file to Liz for canonical-worthy fact extraction.

    Each extracted fact becomes a schema-compliant raw record in raw/inbox/.
    Returns the number of facts written. Returns 0 on any failure (caller should
    proceed with archival regardless — extraction is best-effort).
    """
    try:
        content = memory_file.read_text()[:8000]  # cap to avoid huge prompts
    except Exception as e:
        log_extraction_failure(f"could not read {memory_file}: {e}")
        return 0

    if len(content.strip()) < 100:
        return 0  # too small to bother

    prompt = (
        f"You are Liz, Chris's principal staff engineer.\n\n"
        f"Below is an old memory file from agent workspace that's about to be archived.\n"
        f"Extract any DURABLE facts, decisions, or preferences worth promoting to canonical.\n"
        f"Skip ephemeral session details, completed task notes, and trivial chat.\n\n"
        f"FILE: {memory_file.name}\n"
        f"=" * 60 + "\n"
        f"{content}\n"
        f"=" * 60 + "\n\n"
        f"OUTPUT FORMAT (return ONLY valid JSON, no markdown fences):\n"
        f'{{"facts": [{{"text": "<short fact>", "kind": "preference|decision|fact|entity"}}]}}\n'
        f"\n"
        f"If nothing is worth promoting, return: {{\"facts\": []}}\n"
        f"STRICT: return only the JSON object, no prose."
    )

    sys.path.insert(0, str(Path(__file__).parent))
    from openclaw_dispatch import dispatch as _dispatch  # noqa: E402

    result = _dispatch(
        agent=EXTRACTION_AGENT,
        message=prompt,
        thinking="off",
        timeout=LIZ_DISPATCH_TIMEOUT,
    )
    if not result.ok:
        log_extraction_failure(f"openclaw dispatch failed for {memory_file.name}: {result.error}")
        sys.stderr.write(f"DISPATCH_FAIL agent={EXTRACTION_AGENT} reason={result.error[:200]} file={memory_file.name}\n")
        return 0

    try:
        text = result.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
    except (json.JSONDecodeError, KeyError) as e:
        log_extraction_failure(f"failed to parse Liz reply for {memory_file.name}: {e}")
        return 0

    facts = parsed.get("facts", [])
    if not facts:
        return 0

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    date_part = now_iso[:10].replace("-", "_")
    for fact in facts:
        text = fact.get("text", "").strip()
        if not text:
            continue
        digest = hashlib.sha256(f"{memory_file.name}:{text}".encode()).hexdigest()
        rec_id = f"raw_extraction_{date_part}_{digest[:8]}"
        record = {
            "id": rec_id,
            "timestamp": now_iso,
            "source_type": "extraction",
            "source_ref": f"liz:pre_archive:{memory_file.name}",
            "actor": "liz",
            "visibility": "private",
            "scrub_status": "scrubbed",
            "content": text,
            "attachments": [],
            "entities": ["Chris", fact.get("kind", "fact")],
            "hash": f"sha256:{digest}",
        }
        out = INBOX_DIR / f"{rec_id}.json"
        if not out.exists():
            out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            written += 1
    return written


def main():
    parser = argparse.ArgumentParser(description="Memory File Lifecycle Manager")
    parser.add_argument("--archive-age", type=int, default=90, help="Days before archiving")
    parser.add_argument("--delete-age", type=int, default=180, help="Days before deleting archived files")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-extract", action="store_true",
                        help="Skip pre-archival extraction (e.g. for testing or quota-sensitive runs)")
    args = parser.parse_args()

    now = datetime.now()
    archive_cutoff = now - timedelta(days=args.archive_age)
    delete_cutoff = now - timedelta(days=args.delete_age)

    print(f"Memory Lifecycle — {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"Archive: files older than {args.archive_age} days ({archive_cutoff.strftime('%Y-%m-%d')})")
    print(f"Delete: archived files older than {args.delete_age} days ({delete_cutoff.strftime('%Y-%m-%d')})")
    if args.dry_run:
        print("[DRY RUN]")
    print("=" * 50)

    archived_count = 0
    extracted_total = 0
    for f, agent, file_date in get_memory_files():
        if file_date >= archive_cutoff:
            continue
        archive_dir = f.parent / "archive"
        if args.dry_run:
            print(f"  ARCHIVE: {agent}/{f.name} ({file_date.strftime('%Y-%m-%d')})"
                  + (" [+extract via Liz]" if not args.no_extract else ""))
        else:
            # Pre-archival extraction (best-effort, never blocks the archive)
            if not args.no_extract:
                count = extract_via_liz(f)
                if count > 0:
                    print(f"  EXTRACTED {count} facts from {agent}/{f.name}")
                    extracted_total += count
            archive_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(archive_dir / f.name))
        archived_count += 1

    deleted_count = 0
    for f, agent, file_date in get_archived_files():
        if file_date >= delete_cutoff:
            continue
        if args.dry_run:
            print(f"  DELETE: {agent}/archive/{f.name} ({file_date.strftime('%Y-%m-%d')})")
        else:
            f.unlink()
        deleted_count += 1

    # ── Semantic memory dedup ────────────────────────────
    # Content-hash dedup on the semantic_memory ChromaDB collection.
    # Catches duplicates that slip past learn.py's similarity gate.
    dedup_removed = 0
    if not args.dry_run:
        try:
            dedup_removed = dedup_semantic_memory()
        except Exception as e:
            print(f"  WARNING: semantic_memory dedup failed: {e}")
    else:
        print("  [DRY RUN] Would run semantic_memory dedup")

    print(f"\nArchived: {archived_count} | Deleted: {deleted_count} | Facts extracted: {extracted_total} | Dedup removed: {dedup_removed}")


def dedup_semantic_memory() -> int:
    """Remove content-duplicate entries from the semantic_memory ChromaDB collection."""
    import urllib.request
    CHROMA = "http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"

    # Find collection ID
    resp = urllib.request.urlopen(CHROMA, timeout=10)
    cols = json.loads(resp.read())
    col_id = None
    for c in cols:
        if c.get("name") == "semantic_memory":
            col_id = c["id"]
            break
    if not col_id:
        return 0

    # Get all docs
    req = urllib.request.Request(
        f"{CHROMA}/{col_id}/get",
        data=json.dumps({"limit": 10000, "include": ["documents"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    ids = data.get("ids", [])
    docs = data.get("documents", [])

    # Find content duplicates — keep the first occurrence
    seen = {}
    dupe_ids = []
    for i, doc in enumerate(docs):
        if doc is None:
            continue
        h = hashlib.md5(doc.encode()).hexdigest()
        if h in seen:
            dupe_ids.append(ids[i])
        else:
            seen[h] = i

    if not dupe_ids:
        print(f"  semantic_memory: {len(ids)} entries, no duplicates")
        return 0

    # Delete duplicates in batches
    BATCH = 20
    for s in range(0, len(dupe_ids), BATCH):
        e = min(s + BATCH, len(dupe_ids))
        req = urllib.request.Request(
            f"{CHROMA}/{col_id}/delete",
            data=json.dumps({"ids": dupe_ids[s:e]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
    print(f"  semantic_memory: removed {len(dupe_ids)} content duplicates")
    return len(dupe_ids)


def dedup_semantic_near_duplicates() -> dict:
    """Retroactive near-duplicate scan of semantic_memory using embedding similarity.

    Finds pairs with cosine distance < 0.08 AND Jaccard token overlap > 0.5
    (same thresholds as learn.py inline dedup). Keeps the longer/newer entry,
    deletes the shorter/older one. Logs decisions to audit trail.
    """
    import urllib.request
    import re
    CHROMA = "http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"

    # Find collection ID
    try:
        resp = urllib.request.urlopen(CHROMA, timeout=10)
        cols = json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    col_id = None
    for c in cols:
        if c.get("name") == "semantic_memory":
            col_id = c["id"]
            break
    if not col_id:
        return {"status": "skip", "reason": "collection not found"}

    # Get all docs with embeddings
    req = urllib.request.Request(
        f"{CHROMA}/{col_id}/get",
        data=json.dumps({"limit": 10000, "include": ["documents", "embeddings", "metadatas"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    ids = data.get("ids", [])
    docs = data.get("documents", [])
    embs = data.get("embeddings", [])
    metas = data.get("metadatas", [])

    if len(ids) < 2:
        return {"status": "ok", "checked": len(ids), "removed": 0}

    TOKEN_RE = re.compile(r'[a-z0-9_\-]{2,}')

    def tokenize(text):
        return set(TOKEN_RE.findall((text or "").lower()))

    def cosine_dist(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 2.0
        return 1.0 - (dot / (na * nb))

    def jaccard(s1, s2):
        if not s1 or not s2:
            return 0.0
        return len(s1 & s2) / max(len(s1 | s2), 1)

    # Find near-duplicate pairs
    to_delete = set()
    merge_count = 0

    for i in range(len(ids)):
        if ids[i] in to_delete or not docs[i] or not embs[i]:
            continue
        tokens_i = tokenize(docs[i])

        for j in range(i + 1, len(ids)):
            if ids[j] in to_delete or not docs[j] or not embs[j]:
                continue

            # Quick length check — skip if very different lengths
            len_ratio = min(len(docs[i]), len(docs[j])) / max(len(docs[i]), len(docs[j]), 1)
            if len_ratio < 0.3:
                continue

            dist = cosine_dist(embs[i], embs[j])
            if dist >= 0.08:
                continue

            tokens_j = tokenize(docs[j])
            jac = jaccard(tokens_i, tokens_j)
            if jac < 0.5:
                continue

            # Near-duplicate found — keep the longer/newer one
            time_i = (metas[i] or {}).get("created_at", "")
            time_j = (metas[j] or {}).get("created_at", "")

            if len(docs[i]) > len(docs[j]) or (len(docs[i]) == len(docs[j]) and time_i >= time_j):
                to_delete.add(ids[j])
            else:
                to_delete.add(ids[i])
            merge_count += 1

    if not to_delete:
        print(f"  semantic_memory near-dedup: {len(ids)} entries, no near-duplicates found")
        return {"status": "ok", "checked": len(ids), "removed": 0}

    # Delete in batches
    delete_list = list(to_delete)
    BATCH = 20
    for s in range(0, len(delete_list), BATCH):
        e = min(s + BATCH, len(delete_list))
        req = urllib.request.Request(
            f"{CHROMA}/{col_id}/delete",
            data=json.dumps({"ids": delete_list[s:e]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)

    print(f"  semantic_memory near-dedup: removed {len(delete_list)} near-duplicates from {len(ids)} entries")

    # Audit log
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from audit_log import log_event
        log_event(
            event_type="dedup",
            entity_a="semantic_memory",
            entity_b=f"{len(delete_list)} entries",
            resolution="near_duplicate_cleanup",
            reason=f"Weekly retroactive scan: cosine<0.08 + Jaccard>0.5, {merge_count} pairs found",
        )
    except Exception:
        pass

    return {"status": "ok", "checked": len(ids), "removed": len(delete_list), "pairs": merge_count}


def _get_vote_consensus(contra_id: str) -> str | None:
    """Read contradiction_votes; return consensus action if ≥3 votes and ≥2 agree.

    Returns one of keep_new / keep_old / merge / dismiss, or None if no consensus.
    """
    try:
        import sqlite3
        db = Path("/Users/chrischo/server/brain/logs/autonomy.db")
        if not db.exists():
            return None
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT vote, COUNT(*) FROM contradiction_votes WHERE contradiction_id=? GROUP BY vote",
                (contra_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None
    tally = {vote: count for vote, count in rows}
    total = sum(tally.values())
    if total < 3:
        return None
    top_vote, top_count = max(tally.items(), key=lambda kv: kv[1])
    if top_count < 2:
        return None
    return top_vote


def auto_resolve_stale_contradictions():
    """Auto-resolve contradictions where one side is clearly superior.

    Rules (checked in order):
    0. Agent vote consensus: ≥3 votes with ≥2 agreeing → majority action
    1. If confidence gap > 0.2 — keep the higher-confidence entry
    2. If contradiction is > 14 days old and unreviewed — keep the newer entry
    3. If one side is already deleted — dismiss the contradiction
    """
    from http_pool import http_json
    from search import get_collections
    from datetime import datetime, timezone, timedelta

    cols = get_collections()
    contra_col = cols.get("semantic_contradictions")
    sem_col = cols.get("semantic_memory")
    if not contra_col or not sem_col:
        return {"resolved": 0, "error": "collections unavailable"}

    # Fetch pending contradictions
    try:
        resp = http_json("POST",
            f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{contra_col}/get",
            {"where": {"review_state": "pending"}, "limit": 500, "include": ["metadatas"]}
        )
    except Exception as e:
        return {"resolved": 0, "error": str(e)}

    ids = resp.get("ids", [])
    metas = resp.get("metadatas", [])
    resolved_count = 0
    kept_count = 0
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=14)

    for contra_id, meta in zip(ids, metas or [{}] * len(ids)):
        meta = meta or {}
        old_id = meta.get("old_id")
        new_id = meta.get("new_id")
        created_at = meta.get("created_at", "")

        if not old_id or not new_id:
            continue

        # Fetch both memories
        try:
            mem_resp = http_json("POST",
                f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/get",
                {"ids": [old_id, new_id], "include": ["metadatas"]}
            )
        except Exception:
            continue

        returned_ids = mem_resp.get("ids", [])
        returned_metas = mem_resp.get("metadatas", []) or []
        id_to_meta = {i: m for i, m in zip(returned_ids, returned_metas) if m}

        action = None

        # Case 0: Agent vote consensus overrides heuristics
        vote_action = _get_vote_consensus(contra_id)
        if vote_action in ("keep_new", "keep_old", "merge", "dismiss"):
            action = vote_action
        elif len(returned_ids) < 2:
            # Case 1: One side already deleted
            action = "dismiss"
        else:
            old_meta = id_to_meta.get(old_id, {})
            new_meta = id_to_meta.get(new_id, {})
            try:
                old_conf = float(old_meta.get("confidence", 0.5))
                new_conf = float(new_meta.get("confidence", 0.5))
            except (ValueError, TypeError):
                old_conf = new_conf = 0.5

            # Case 2: Confidence gap > 0.2
            if new_conf - old_conf > 0.2:
                action = "keep_new"
            elif old_conf - new_conf > 0.2:
                action = "keep_old"
            else:
                # Case 3: Age-based — contradictions older than 14 days, keep newer
                try:
                    contra_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if contra_dt.tzinfo is None:
                        contra_dt = contra_dt.replace(tzinfo=timezone.utc)
                    if contra_dt < cutoff_date:
                        action = "keep_new"
                except Exception:
                    pass

        if action is None:
            kept_count += 1
            continue

        # "merge" has no deterministic implementation here — the votes express
        # an intent that needs human curation. Leave the contradiction pending
        # for manual review rather than silently dropping both sides.
        if action == "merge":
            kept_count += 1
            continue

        # Apply resolution
        try:
            if action == "keep_new" and old_id in returned_ids:
                http_json("POST",
                    f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/delete",
                    {"ids": [old_id]}
                )
            elif action == "keep_old" and new_id in returned_ids:
                http_json("POST",
                    f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/delete",
                    {"ids": [new_id]}
                )
            # "dismiss" and the keep_* branches both clear the contradiction
            # record after (optionally) deleting one side.
            http_json("POST",
                f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{contra_col}/delete",
                {"ids": [contra_id]}
            )
            # Clean up any orphan votes for this contradiction so the
            # contradiction_votes table doesn't grow unbounded and so future
            # votes can't match a deleted contradiction_id.
            try:
                import sqlite3
                db = Path("/Users/chrischo/server/brain/logs/autonomy.db")
                if db.exists():
                    conn = sqlite3.connect(str(db))
                    try:
                        conn.execute("DELETE FROM contradiction_votes WHERE contradiction_id=?", (contra_id,))
                        conn.commit()
                    finally:
                        conn.close()
            except Exception:
                pass
            resolved_count += 1
        except Exception:
            continue

    return {"resolved": resolved_count, "kept_for_review": kept_count, "total": len(ids)}


if __name__ == '__main__':
    main()
