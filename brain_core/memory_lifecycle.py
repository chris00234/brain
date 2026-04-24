#!/opt/homebrew/bin/python3
"""Memory file lifecycle - archive old agent memory files, extract insights first.

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
import logging
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("brain.memory_lifecycle")

try:
    from config import INBOX_DIR, OPENCLAW_BIN
    from config import OPENCLAW_DIR as AGENTS_DIR

    EXTRACTION_FAILURE_LOG = AGENTS_DIR / "workspace-liz" / "logs" / "memory-extraction-failures.jsonl"
except ImportError:
    AGENTS_DIR = Path("/Users/chrischo/.openclaw")
    INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
    EXTRACTION_FAILURE_LOG = Path(
        "/Users/chrischo/.openclaw/workspace-liz/logs/memory-extraction-failures.jsonl"
    )
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
            f.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


def extract_via_liz(memory_file: Path) -> int:
    """Dispatch a memory file to Liz for canonical-worthy fact extraction.

    Each extracted fact becomes a schema-compliant raw record in raw/inbox/.
    Returns the number of facts written. Returns 0 on any failure (caller should
    proceed with archival regardless - extraction is best-effort).
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
        "OUTPUT FORMAT (return ONLY valid JSON, no markdown fences):\n"
        '{"facts": [{"text": "<short fact>", "kind": "preference|decision|fact|entity"}]}\n'
        "\n"
        'If nothing is worth promoting, return: {"facts": []}\n'
        "STRICT: return only the JSON object, no prose."
    )

    sys.path.insert(0, str(Path(__file__).parent))
    from cli_llm import dispatch as _dispatch

    result = _dispatch(
        agent=EXTRACTION_AGENT,
        message=prompt,
        thinking="off",
        timeout=LIZ_DISPATCH_TIMEOUT,
    )
    if not result.ok:
        log_extraction_failure(f"openclaw dispatch failed for {memory_file.name}: {result.error}")
        sys.stderr.write(
            f"DISPATCH_FAIL agent={EXTRACTION_AGENT} reason={result.error[:200]} file={memory_file.name}\n"
        )
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
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
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
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Skip pre-archival extraction (e.g. for testing or quota-sensitive runs)",
    )
    args = parser.parse_args()

    now = datetime.now()
    archive_cutoff = now - timedelta(days=args.archive_age)
    delete_cutoff = now - timedelta(days=args.delete_age)

    print(f"Memory Lifecycle - {now.strftime('%Y-%m-%d %H:%M')}")
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
            print(
                f"  ARCHIVE: {agent}/{f.name} ({file_date.strftime('%Y-%m-%d')})"
                + (" [+extract via Liz]" if not args.no_extract else "")
            )
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

    print(
        f"\nArchived: {archived_count} | Deleted: {deleted_count} | Facts extracted: {extracted_total} | Dedup removed: {dedup_removed}"
    )


def dedup_semantic_memory() -> int:
    """Remove content-duplicate entries from the semantic_memory collection."""
    from vector_store import get_vector_store

    store = get_vector_store()

    points = store.get(
        "semantic_memory",
        limit=10000,
        with_payload=False,
        with_documents=True,
    )
    if not points:
        return 0

    # Find content duplicates — keep the first occurrence.
    seen: dict[str, int] = {}
    dupe_ids: list[str] = []
    for i, p in enumerate(points):
        doc = p.document
        if doc is None:
            continue
        h = hashlib.md5(doc.encode()).hexdigest()
        if h in seen:
            dupe_ids.append(p.id)
        else:
            seen[h] = i

    if not dupe_ids:
        print(f"  semantic_memory: {len(points)} entries, no duplicates")
        return 0

    # Delete duplicates in batches.
    BATCH = 20
    for s in range(0, len(dupe_ids), BATCH):
        e = min(s + BATCH, len(dupe_ids))
        store.delete("semantic_memory", dupe_ids[s:e])
    print(f"  semantic_memory: removed {len(dupe_ids)} content duplicates")
    return len(dupe_ids)


def dedup_semantic_near_duplicates() -> dict:
    """Retroactive near-duplicate scan of semantic_memory using embedding similarity.

    Finds pairs with cosine distance < 0.08 AND Jaccard token overlap > 0.5
    (same thresholds as learn.py inline dedup). Keeps the longer/newer entry,
    deletes the shorter/older one. Logs decisions to audit trail.
    """
    import re

    from vector_store import get_vector_store

    store = get_vector_store()

    # Get docs with embeddings — capped at 2000 now that this runs daily.
    # 2000^2 / 2 = 2M pairwise comparisons; with length + cosine prefilters
    # the hot loop finishes in ~30-40s on the M4 Max. Previously capped at
    # 300 (weekly) which let near-duplicates accumulate past the window.
    try:
        points = store.get(
            "semantic_memory",
            limit=2000,
            with_payload=True,
            with_documents=True,
            with_vectors=True,
        )
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    ids = [p.id for p in points]
    docs = [p.document or "" for p in points]
    embs = [p.vector or [] for p in points]
    metas = [p.payload or {} for p in points]

    if len(ids) < 2:
        return {"status": "ok", "checked": len(ids), "removed": 0}

    TOKEN_RE = re.compile(r"[a-z0-9_\-]{2,}")

    def tokenize(text):
        return set(TOKEN_RE.findall((text or "").lower()))

    def cosine_dist(a, b):
        dot = sum(x * y for x, y in zip(a, b, strict=False))
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

            # Quick length check - skip if very different lengths
            len_ratio = min(len(docs[i]), len(docs[j])) / max(len(docs[i]), len(docs[j]), 1)
            if len_ratio < 0.3:
                continue

            dist = cosine_dist(embs[i], embs[j])
            # Raised 2026-04-23 from 0.08 to 0.10 to close the gap with the
            # contradiction detector (fires at distance < 0.10). Previously
            # atoms in [0.08, 0.10] got flagged as contradictions but never
            # auto-resolved — they lived in attention_queue indefinitely.
            if dist >= 0.10:
                continue

            tokens_j = tokenize(docs[j])
            jac = jaccard(tokens_i, tokens_j)
            if jac < 0.5:
                continue

            # Near-duplicate found - keep the longer/newer one
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
        store.delete("semantic_memory", delete_list[s:e])

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
            reason=f"Daily retroactive scan: cosine<0.10 + Jaccard>0.5, {merge_count} pairs found",
        )
    except Exception:
        pass

    return {"status": "ok", "checked": len(ids), "removed": len(delete_list), "pairs": merge_count}


def _get_vote_consensus(contra_id: str) -> str | None:
    """Read contradiction_votes; return consensus action if >=3 votes and >=2 agree.

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
    0. Agent vote consensus: >=3 votes with >=2 agreeing -> majority action
    1. If confidence gap > 0.2 - keep the higher-confidence entry
    2. If contradiction is > 14 days old and unreviewed - keep the newer entry
    3. If one side is already deleted - dismiss the contradiction
    """
    from datetime import datetime, timedelta

    from vector_store import get_vector_store

    store = get_vector_store()

    # Fetch pending contradictions
    try:
        pending = store.get(
            "semantic_contradictions",
            filter={"review_state": "pending"},
            limit=500,
            with_payload=True,
            with_documents=False,
        )
    except Exception as e:
        return {"resolved": 0, "error": str(e)}

    resolved_count = 0
    kept_count = 0
    cutoff_date = datetime.now(UTC) - timedelta(days=14)

    for p in pending:
        meta = p.payload or {}
        contra_id = p.id
        old_id = meta.get("old_id")
        new_id = meta.get("new_id")
        created_at = meta.get("created_at", "")

        if not old_id or not new_id:
            continue

        # Fetch both memories
        try:
            both = store.get(
                "semantic_memory",
                ids=[old_id, new_id],
                with_payload=True,
                with_documents=False,
            )
        except Exception:
            continue

        returned_ids = [m.id for m in both]
        id_to_meta = {m.id: (m.payload or {}) for m in both}

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
                # Case 2b: Near-duplicate rephrasing - distance<0.05 + token
                # overlap>=0.70. Matches the ingest-time gate in learn.py so
                # any near-duplicate that slipped past (e.g. added before the
                # gate shipped) still gets cleaned up.
                try:
                    dist_val = float(meta.get("distance", 1.0))
                    overlap_val = float(meta.get("token_overlap", 0.0))
                except (ValueError, TypeError):
                    dist_val = 1.0
                    overlap_val = 0.0
                if dist_val < 0.05 and overlap_val >= 0.70:
                    action = "keep_new"
                else:
                    # Case 3: Age-based - contradictions older than 14 days, keep newer
                    try:
                        contra_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        if contra_dt.tzinfo is None:
                            contra_dt = contra_dt.replace(tzinfo=UTC)
                        if contra_dt < cutoff_date:
                            action = "keep_new"
                    except Exception:
                        pass

        if action is None:
            kept_count += 1
            continue

        # "merge" has no deterministic implementation here - the votes express
        # an intent that needs human curation. Leave the contradiction pending
        # for manual review rather than silently dropping both sides.
        if action == "merge":
            kept_count += 1
            continue

        # Apply resolution
        try:
            if action == "keep_new" and old_id in returned_ids:
                store.delete("semantic_memory", [old_id])
            elif action == "keep_old" and new_id in returned_ids:
                store.delete("semantic_memory", [new_id])
            # "dismiss" and the keep_* branches both clear the contradiction
            # record after (optionally) deleting one side.
            store.delete("semantic_contradictions", [contra_id])
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

    return {"resolved": resolved_count, "kept_for_review": kept_count, "total": len(pending)}


def cleanup_supersession_chains() -> dict:
    """Find and fix orphaned supersession chains.

    When the head of a supersession chain is deleted, all predecessors
    are stuck as 'superseded' pointing to a dead ID. This function:
    1. Finds all memories with a non-empty superseded_by field
    2. Checks if the superseded_by target still exists
    3. If the target is missing, clears superseded_by (resurrects the memory)
    4. If the target itself is superseded, follows the chain to the live head
    """
    from vector_store import get_vector_store

    store = get_vector_store()

    # Single-call full scan — QdrantStore.get walks the native cursor
    # internally. The previous `while True: offset+=PAGE` pattern silently
    # re-fetched the first page on every iteration under Qdrant 1.17.
    all_ids: set[str] = set()
    superseded: list[tuple[str, dict]] = []

    try:
        points = store.get(
            "semantic_memory",
            limit=1_000_000,
            with_payload=True,
            with_documents=False,
        )
    except Exception as e:
        return {"checked": 0, "orphaned": 0, "fixed": 0, "error": f"fetch failed: {e}"}

    for p in points:
        all_ids.add(p.id)
        meta = p.payload or {}
        target = (meta.get("superseded_by") or "").strip()
        if target:
            superseded.append((p.id, dict(meta)))

    # Build lookup for O(1) chain walking
    superseded_map: dict[str, dict] = dict(superseded)

    # Find orphans: superseded_by points to a non-existent ID
    orphaned_ids: list[str] = []
    orphaned_metas: list[dict] = []
    for mid, meta in superseded:
        target = meta["superseded_by"].strip()
        # Follow chain: if target exists but is itself superseded, walk forward
        visited: set[str] = {mid}
        current = target
        while current in all_ids:
            # Target exists - check if it's also superseded
            target_meta = superseded_map.get(current)
            if target_meta is None:
                break  # current is live (not superseded) - chain is healthy
            next_target = (target_meta.get("superseded_by") or "").strip()
            if not next_target or next_target in visited:
                break  # chain ends here or is circular
            visited.add(next_target)
            current = next_target

        if current not in all_ids:
            # Chain head is dead - resurrect this memory
            meta.pop("superseded_by", None)
            meta.pop("valid_until", None)
            orphaned_ids.append(mid)
            orphaned_metas.append(meta)

    # Batch update orphans. Under the VectorStore abstraction each
    # orphan gets an individual patch (superseded_by / valid_until
    # cleared) so we issue one update_payload per id. Total orphan
    # count is small on normal runs; batch=50 is kept to preserve
    # error-logging semantics.
    fixed = 0
    BATCH = 50
    for i in range(0, len(orphaned_ids), BATCH):
        batch_ids = orphaned_ids[i : i + BATCH]
        try:
            for mid in batch_ids:
                store.update_payload(
                    "semantic_memory",
                    ids=[mid],
                    patch={"superseded_by": "", "valid_until": ""},
                )
            fixed += len(batch_ids)
        except Exception as e:
            return {
                "checked": len(superseded),
                "orphaned": len(orphaned_ids),
                "fixed": fixed,
                "error": f"update failed at batch {i}: {e}",
            }

    return {"checked": len(superseded), "orphaned": len(orphaned_ids), "fixed": fixed}


def recompute_trust_scores() -> dict:
    """Round 9 B3: weekly refresh of trust_score from current corroboration counts.

    Walks semantic_memory in pages, recomputes the cross-source corroboration
    score for each entry, and updates the metadata in place. Memories that
    gain or lose corroborating sources naturally drift in trust over time.
    """
    from vector_store import get_vector_store

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from learn import _count_corroborating_trust
    except Exception as e:
        return {"status": "error", "reason": f"learn import failed: {e}"}

    store = get_vector_store()

    total = 0
    updated = 0
    drift_up = 0
    drift_down = 0
    try:
        points = store.get(
            "semantic_memory",
            limit=1_000_000,
            with_payload=True,
            with_documents=True,
        )
    except Exception as e:
        return {"status": "error", "reason": f"fetch failed: {e}"}

    for p in points:
        total += 1
        meta = p.payload or {}
        try:
            old = float(meta.get("trust_score", "0.5"))
        except Exception:
            old = 0.5
        new = _count_corroborating_trust(p.document or "")
        if abs(new - old) < 0.05:
            continue
        if new > old:
            drift_up += 1
        else:
            drift_down += 1
        try:
            store.update_payload(
                "semantic_memory",
                ids=[p.id],
                patch={"trust_score": float(new)},
            )
            updated += 1
        except Exception as e:
            # Continue — partial updates are fine for a weekly job.
            print(f"  trust_recompute update failed for {p.id}: {e}")

    return {
        "status": "ok",
        "scanned": total,
        "updated": updated,
        "drift_up": drift_up,
        "drift_down": drift_down,
    }


def reinforce_on_access(memory_ids: list[str], boost: float = 0.02) -> dict:
    """Round 10 C1 (MemoryBank): bump access_count + trust_score on retrieval.

    Called as a fire-and-forget BackgroundTask from /recall when semantic_memory
    entries appear in the top-N results. Implements MemoryBank's reinforcement
    on access - memories you actually use ratchet up trust over time, becoming
    more salient for future queries.

    Best-effort: any failure logs and returns the partial count rather than
    blocking the recall response.
    """
    if not memory_ids:
        return {"reinforced": 0}
    from vector_store import get_vector_store

    store = get_vector_store()

    # Fetch current metadata for the affected ids
    try:
        points = store.get(
            "semantic_memory",
            ids=memory_ids[:20],
            with_payload=True,
            with_documents=False,
        )
    except Exception as e:
        return {"reinforced": 0, "reason": f"fetch failed: {e}"}
    if not points:
        return {"reinforced": 0}

    # Z-suffix matches the convention used by entity_graph._now() and
    # learn._now_iso() — keeps lexicographic comparison consistent across
    # all writers (the prune job sorts by these timestamps).
    now_iso = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    reinforced = 0
    for p in points:
        meta = dict(p.payload or {})
        try:
            count = int(meta.get("access_count", 0))
        except (ValueError, TypeError):
            count = 0
        try:
            trust = float(meta.get("trust_score", 0.5))
        except (ValueError, TypeError):
            trust = 0.5

        # Decayed access_score: weights recent access more heavily.
        # Existing score decays at 5% per day, then +1 for current access.
        last_accessed_raw = meta.get("last_accessed_at", "")
        existing_score = float(meta.get("access_score", str(count)))
        if last_accessed_raw:
            try:
                last_ts = last_accessed_raw.replace("Z", "+00:00")
                last_dt = datetime.fromisoformat(last_ts)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                days_since = max(0, (now_dt - last_dt).total_seconds() / 86400)
                decay_factor = 0.95**days_since
                existing_score = existing_score * decay_factor
            except (ValueError, TypeError):
                pass

        # Phase A4: typed numerics for Qdrant payload range filters.
        patch = {
            "access_count": count + 1,
            "access_score": round(existing_score + 1.0, 2),
            "last_accessed_at": now_iso,
            "trust_score": round(min(1.0, trust + boost), 3),
        }
        try:
            store.update_payload("semantic_memory", ids=[p.id], patch=patch)
            reinforced += 1
        except Exception as e:
            # Partial reinforcement is fine — this runs as a fire-and-forget
            # BackgroundTask from /recall.
            print(f"  reinforce_on_access update failed for {p.id}: {e}")

    if reinforced == 0:
        return {"reinforced": 0}
    update_ids = [p.id for p in points]

    # v3 Layer B - bump Neo4j MemoryAccess.utility_score so the graph's
    # MemRL ranking actually reflects usage. Uses _neo4j_only path to avoid
    # the double-boost bug where reinforce_memory's full path re-reads and
    # re-writes Chroma trust_score we just updated above (would add +0.04
    # instead of +0.02 per access). The Neo4j-only path only touches
    # MemoryAccess, not Chroma.
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from entity_graph import reinforce_memory_neo4j_only

        for mid in update_ids:
            try:
                reinforce_memory_neo4j_only(mid, success=True)
            except Exception:
                continue
    except ImportError:
        pass

    return {"reinforced": len(update_ids)}


def reinforce_all_collections(results: list[dict], limit: int = 10) -> dict:
    """Super-human reconsolidation: reinforce EVERY atom-backed retrieval hit,
    not just semantic_memory. Matches biological reconsolidation - every
    retrieval rewrites the trace. UP-only: bumps reinforcement_count and
    last_reviewed_at via atoms_store.reinforce. Never decrements, never
    deletes. Fire-and-forget from /recall BackgroundTask.
    """
    if not results:
        return {"reinforced": 0}
    sem_ids: list[str] = []
    atom_chroma_ids: list[str] = []
    for r in results[:limit]:
        if not isinstance(r, dict):
            continue
        col = (r.get("collection") or "").lower()
        rid = r.get("id") or (r.get("metadata") or {}).get("id")
        if not rid:
            continue
        if col == "semantic_memory" or "semantic" in col:
            sem_ids.append(rid)
        else:
            atom_chroma_ids.append(rid)
    out = {"reinforced": 0, "semantic": 0, "atoms": 0}
    if sem_ids:
        try:
            r1 = reinforce_on_access(sem_ids)
            out["semantic"] = int(r1.get("reinforced", 0))
        except Exception as _exc:
            log.debug("sem reinforce skipped: %s", _exc)
    if atom_chroma_ids:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from atoms_store import reinforce as _atom_reinforce

            for cid in atom_chroma_ids:
                try:
                    _atom_reinforce(cid, success=True)
                    out["atoms"] += 1
                except Exception:
                    continue
        except ImportError:
            pass
    out["reinforced"] = out["semantic"] + out["atoms"]
    return out


def prune_atrophied_memories(
    dry_run: bool = True, max_age_days: int = 120, compress_with_gist: bool = False
) -> dict:
    """Round 10 C1 (MemoryBank): synaptic pruning of unused obsolete memories.

    Walks semantic_memory and identifies entries matching ALL of:
      - tier == "obsolete"
      - access_count == 0
      - last_accessed_at older than max_age_days (or never accessed AND
        created_at older than max_age_days)
      - trust_score < 0.5
      - not referenced from any canonical/distilled note (provenance check)

    Default is dry_run=True - logs what WOULD be pruned without deleting.
    Set dry_run=False after reviewing the candidates.

    Compression: if compress_with_gist=True AND there are candidates, dispatch
    a single Jenna call (batched) to extract one-line gists per memory before
    deletion. The gists are stored as new tier="gist" memories with
    derived_from=[old_id], so the information isn't lost - only the verbose
    original is removed.

    Every deletion is logged to audit_log for recovery if needed.

    Safety floor: never deletes more than 100 memories per run, never deletes
    if the candidate set is more than 5% of the collection.
    """
    from vector_store import get_vector_store

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from audit_log import log_event
    except Exception:

        def log_event(*args, **kwargs):
            return ""

    store = get_vector_store()

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    # Normalize to Z-suffix so lexicographic comparison works against memories
    # whose timestamps were written by entity_graph._now() (Z-suffix) rather
    # than reinforce_on_access (+00:00 suffix). "+" sorts after "Z" in ASCII,
    # so a +00:00 cutoff string was making Z-suffix memories appear OLDER
    # than the cutoff and bypassing the age filter.
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

    # Round 11 fix: real provenance check - preload the set of memory IDs
    # mentioned in any canonical/distilled note. The previous implementation
    # checked metadata fields that no writer ever populates, making the
    # provenance gate a no-op.
    canonical_refs: set[str] = set()
    try:
        canonical_refs_path = Path("/Users/chrischo/server/knowledge")
        for sub in ("canonical", "distilled"):
            base = canonical_refs_path / sub
            if not base.exists():
                continue
            for f in base.rglob("*.md"):
                try:
                    txt = f.read_text(errors="replace")
                    # Extract semantic_memory:<hex> patterns
                    import re as _re

                    for m in _re.finditer(r"semantic_memory:[a-f0-9]{8,40}", txt):
                        canonical_refs.add(m.group(0))
                except Exception:
                    continue
    except Exception:
        pass

    # Single-call full scan via cursor.
    candidates: list[dict] = []
    total_scanned = 0
    try:
        points = store.get(
            "semantic_memory",
            limit=1_000_000,
            with_payload=True,
            with_documents=True,
        )
    except Exception as e:
        return {"status": "error", "reason": f"fetch failed: {e}"}

    for p in points:
        total_scanned += 1
        mid = p.id
        doc = p.document or ""
        meta = p.payload or {}
        tier = (meta.get("memory_class") or "").lower()
        if tier != "obsolete":
            continue
        try:
            count = int(meta.get("access_count", 0))
        except (ValueError, TypeError):
            count = 0
        if count > 0:
            continue
        try:
            trust = float(meta.get("trust_score", 0.5))
        except (ValueError, TypeError):
            trust = 0.5
        if trust >= 0.5:
            continue
        last_accessed = (
            meta.get("last_accessed_at") or meta.get("updated_at") or meta.get("created_at") or ""
        )
        if not last_accessed:
            continue
        # Normalize Z/+00:00 suffix for lexicographic comparison (round 11 fix)
        last_accessed_norm = last_accessed.replace("+00:00", "Z")
        if last_accessed_norm >= cutoff_iso:
            continue
        # Provenance check: don't delete if any canonical/distilled note
        # references this memory id (real check, not the no-op metadata
        # field that round 10 originally used).
        if mid in canonical_refs:
            continue

        candidates.append(
            {
                "id": mid,
                "content": (doc or "")[:300],
                "trust": trust,
                "last_accessed": last_accessed,
                "tier": tier,
            }
        )

    n_candidates = len(candidates)
    # Take the SMALLER of the two limits - never delete >100/run AND never
    # delete >5% of the collection. Was using max() which inverted the intent.
    safety_cap = min(100, max(1, int(total_scanned * 0.05)))
    if n_candidates > safety_cap:
        return {
            "status": "safety_abort",
            "scanned": total_scanned,
            "candidates": n_candidates,
            "safety_cap": safety_cap,
            "reason": f"candidate set ({n_candidates}) exceeds 5% / 100 floor - refusing to prune",
        }

    if dry_run or n_candidates == 0:
        # Log the dry-run report so it's reviewable
        sample = candidates[:10]
        return {
            "status": "dry_run" if dry_run else "ok",
            "scanned": total_scanned,
            "candidates": n_candidates,
            "would_prune": [
                {"id": c["id"], "trust": c["trust"], "last_accessed": c["last_accessed"]} for c in sample
            ],
            "actually_deleted": 0,
        }

    # Real deletion path - only reached when dry_run=False
    candidate_ids = [c["id"] for c in candidates]

    # Optional gist compression before delete
    gist_count = 0
    if compress_with_gist and candidates:
        try:
            from cli_llm import dispatch

            BATCH = 50
            # 2026-04-17 token-spike fix: cap per-item content at 400 chars.
            # Previously unbounded concat of 50 memories could hit 760K tokens/call
            # (observed 3-day OpenAI spike). 400 chars x 50 = 20KB prompt ≈ 5K tokens,
            # still enough context for a 1-line gist of each memory.
            for i in range(0, len(candidates), BATCH):
                batch = candidates[i : i + BATCH]
                prompt = (
                    "Compress each of these memories to a single 1-line gist. "
                    'Return strict JSON: {"gists": [{"id": "...", "gist": "..."}, ...]}\n\n'
                    + "\n".join(f"[{c['id']}] {(c['content'] or '')[:400]}" for c in batch)
                )
                result = dispatch(agent="jenna", message=prompt, thinking="off", timeout=60)
                if result.ok:
                    try:
                        text = result.text.strip()
                        if text.startswith("```"):
                            text = text.split("```", 2)[1]
                            if text.startswith("json"):
                                text = text[4:]
                            if "```" in text:
                                text = text.split("```", 1)[0]
                        parsed = json.loads(text)
                        for g in parsed.get("gists", []):
                            old_id = g.get("id", "")
                            gist_text = (g.get("gist") or "").strip()
                            if not gist_text or len(gist_text) < 10:
                                continue
                            # Store the gist as a new memory with tier=gist
                            try:
                                from indexer import get_embedding

                                emb = get_embedding(gist_text, prefix="passage")
                                if emb:
                                    new_id = f"gist_{old_id[:24]}"
                                    store.upsert(
                                        "semantic_memory",
                                        ids=[new_id],
                                        vectors=[emb],
                                        documents=[gist_text],
                                        payloads=[
                                            {
                                                "memory_class": "gist",
                                                "derived_from": old_id,
                                                "created_at": datetime.now(UTC).isoformat(),
                                                "trust_score": 0.4,  # Phase A4: typed float
                                            }
                                        ],
                                    )
                                    gist_count += 1
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception as e:
            print(f"  gist compression failed: {e}")

    # Delete in batches of 50 - record audit log for each
    deleted = 0
    BATCH = 50
    for i in range(0, len(candidate_ids), BATCH):
        batch = candidate_ids[i : i + BATCH]
        try:
            store.delete("semantic_memory", batch)
            deleted += len(batch)
            for mid in batch:
                try:
                    log_event(
                        event_type="prune",
                        entity_a=mid,
                        resolution="atrophied_obsolete",
                        reason=f"obsolete + access_count=0 + trust<0.5 + last_accessed > {max_age_days}d",
                    )
                except Exception:
                    pass
        except Exception as e:
            return {
                "status": "partial",
                "scanned": total_scanned,
                "candidates": n_candidates,
                "actually_deleted": deleted,
                "gist_count": gist_count,
                "reason": f"delete failed at batch {i}: {e}",
            }

    return {
        "status": "ok",
        "scanned": total_scanned,
        "candidates": n_candidates,
        "actually_deleted": deleted,
        "gist_count": gist_count,
    }


def cleanup_stale_superseded() -> dict:
    """Delete old superseded memories whose replacement exists and were never accessed since.

    Targets memories where:
      - superseded_by is set AND the target ID exists (chain is valid)
      - valid_until is set and > 30 days ago (superseded for 30+ days)
      - access_count == 0 since being superseded

    Safety: max 100 deletions per run, max 5% of collection.
    """
    from vector_store import get_vector_store

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from audit_log import log_event
    except Exception:

        def log_event(*args, **kwargs):
            return ""

    store = get_vector_store()

    cutoff = datetime.now(UTC) - timedelta(days=30)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

    # Single-call full scan via cursor.
    all_ids: set[str] = set()
    superseded: list[tuple[str, dict]] = []
    total_scanned = 0

    try:
        points = store.get(
            "semantic_memory",
            limit=1_000_000,
            with_payload=True,
            with_documents=False,
        )
    except Exception as e:
        return {"cleaned": 0, "checked": 0, "error": f"fetch failed: {e}"}

    for p in points:
        total_scanned += 1
        all_ids.add(p.id)
        meta = p.payload or {}
        target = (meta.get("superseded_by") or "").strip()
        if target:
            superseded.append((p.id, dict(meta)))

    # Filter to deletion candidates
    candidates: list[str] = []
    for mid, meta in superseded:
        target = meta["superseded_by"].strip()
        # Target must exist (chain is valid - not an orphan)
        if target not in all_ids:
            continue
        # valid_until must be set and older than 30 days
        valid_until = (meta.get("valid_until") or "").strip()
        if not valid_until:
            continue
        valid_until_norm = valid_until.replace("+00:00", "Z")
        if valid_until_norm >= cutoff_iso:
            continue  # superseded too recently
        # access_count must be 0
        try:
            count = int(meta.get("access_count", 0))
        except (ValueError, TypeError):
            count = 0
        if count > 0:
            continue
        candidates.append(mid)

    checked = len(superseded)

    # Safety floor: max 100, max 5% of collection
    safety_cap = min(100, max(1, int(total_scanned * 0.05)))
    if len(candidates) > safety_cap:
        return {
            "cleaned": 0,
            "checked": checked,
            "candidates": len(candidates),
            "safety_cap": safety_cap,
            "reason": f"candidate set ({len(candidates)}) exceeds safety cap - refusing",
        }

    if not candidates:
        return {"cleaned": 0, "checked": checked}

    # Delete in batches
    deleted = 0
    BATCH = 50
    for i in range(0, len(candidates), BATCH):
        batch = candidates[i : i + BATCH]
        try:
            store.delete("semantic_memory", batch)
            deleted += len(batch)
            for mid in batch:
                try:
                    log_event(
                        event_type="prune",
                        entity_a=mid,
                        resolution="stale_superseded",
                        reason="superseded >30d + target exists + access_count=0",
                    )
                except Exception:
                    pass
        except Exception as e:
            return {"cleaned": deleted, "checked": checked, "error": f"delete failed: {e}"}

    return {"cleaned": deleted, "checked": checked}


def memory_health_report() -> dict:
    """Weekly health snapshot of the semantic_memory tier system.

    Aggregates: tier distribution, category breakdown, stuck memories,
    average age/access per tier, superseded count. Writes JSON to
    logs/memory_health.json. No vector writes, no LLM calls.
    """
    from vector_store import get_vector_store

    store = get_vector_store()

    # Single-call full scan via cursor.
    try:
        points = store.get(
            "semantic_memory",
            limit=1_000_000,
            with_payload=True,
            with_documents=False,
        )
    except Exception as e:
        return {"status": "error", "reason": f"fetch failed: {e}"}
    all_metas: list[dict] = [(p.payload or {}) for p in points]

    now = datetime.now(UTC)
    now_iso = now.isoformat().replace("+00:00", "Z")
    total = len(all_metas)

    # Accumulators
    tier_counts: dict[str, int] = {}
    tier_age_sum: dict[str, float] = {}
    tier_access_sum: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    superseded_count = 0
    stuck_count = 0
    STUCK_AGE_DAYS = 90

    for meta in all_metas:
        tier = (meta.get("memory_class") or "episodic").lower()
        cat = (meta.get("category") or "other").lower()

        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        category_counts[cat] = category_counts.get(cat, 0) + 1

        # Age
        created_raw = meta.get("created_at", "")
        age_days = 0.0
        if created_raw:
            try:
                dt = datetime.fromisoformat(created_raw.rstrip("Z").replace("+00:00", ""))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                age_days = max(0.0, (now - dt).total_seconds() / 86400)
            except Exception:
                pass
        tier_age_sum[tier] = tier_age_sum.get(tier, 0.0) + age_days

        # Access count
        try:
            ac = int(meta.get("access_count", 0))
        except (ValueError, TypeError):
            ac = 0
        tier_access_sum[tier] = tier_access_sum.get(tier, 0) + ac

        # Superseded
        if (meta.get("superseded_by") or "").strip():
            superseded_count += 1

        # Stuck: episodic, old, never accessed
        if tier == "episodic" and age_days >= STUCK_AGE_DAYS and ac == 0:
            stuck_count += 1

    # Compute averages
    tier_avg_age = {t: round(tier_age_sum.get(t, 0) / max(c, 1), 1) for t, c in tier_counts.items()}
    tier_avg_access = {t: round(tier_access_sum.get(t, 0) / max(c, 1), 2) for t, c in tier_counts.items()}

    report = {
        "status": "ok",
        "timestamp": now_iso,
        "total_memories": total,
        "by_tier": dict(sorted(tier_counts.items())),
        "by_category": dict(sorted(category_counts.items())),
        "stuck_episodic": stuck_count,
        "superseded": superseded_count,
        "avg_age_days_by_tier": tier_avg_age,
        "avg_access_count_by_tier": tier_avg_access,
    }

    # Write to logs
    log_path = Path("/Users/chrischo/server/brain/logs/memory_health.json")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(report, indent=2))
    except Exception as e:
        report["write_error"] = str(e)

    return report


if __name__ == "__main__":
    main()
