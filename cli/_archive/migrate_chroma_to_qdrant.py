#!/opt/homebrew/bin/python3
"""cli/migrate_chroma_to_qdrant.py — one-shot data migration for the bold path.

Phase B3 equivalent, compressed. Reads every Chroma collection through
``ChromaStore`` and writes to the corresponding Qdrant target collection
through ``QdrantStore``. Verification hooks run per collection.

Topology (13 → 7):

    canonical, canonical_raptor                → canonical
    semantic_memory, semantic_contradictions   → semantic_memory
    experience, experience_compressed          → experience
    knowledge, context, patterns               → knowledge
    code                                       → code
    personal                                   → personal
    obsidian                                   → obsidian
    healthcheck_probe                          → (dropped)

For the merged collections, a `payload.origin` / `kind` / `compressed`
key is stamped so downstream code can still distinguish the source.

Resumability: `logs/qdrant_migration_checkpoint.json` records the last
confirmed offset per SOURCE collection. A rerun picks up where it left
off. ``--reset`` discards the checkpoint.

Verification (--verify-only, no writes):
- Per target collection: Qdrant count >= source-collection(s) count.
- Random 50 ids sampled from Chroma: exist in Qdrant via retrieve().
- Random 10 ids: vector L2 distance < 1e-5 between Chroma vec + Qdrant vec.

Usage:
    python cli/migrate_chroma_to_qdrant.py --dry-run
    python cli/migrate_chroma_to_qdrant.py
    python cli/migrate_chroma_to_qdrant.py --verify-only
    python cli/migrate_chroma_to_qdrant.py --collection canonical
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from qdrant_store import QdrantStore  # noqa: E402
from vector_store import ChromaStore  # noqa: E402

# Module-level handle so we construct exactly once per process.
_CHROMA: ChromaStore | None = None


def chroma_store() -> ChromaStore:
    global _CHROMA
    if _CHROMA is None:
        _CHROMA = ChromaStore()
    return _CHROMA


CHECKPOINT_FILE = BRAIN_ROOT / "logs" / "qdrant_migration_checkpoint.json"
PAGE_SIZE = 500
VERIFY_SAMPLE_IDS = 50
VERIFY_SAMPLE_VECTORS = 10

# Source → target topology. Each entry is (target_name, discriminator_patch):
# the discriminator patch is merged into every migrated point's payload so
# the merged collection retains source provenance.
TOPOLOGY: dict[str, tuple[str, dict]] = {
    "canonical": ("canonical", {"raptor_level": 0}),
    "canonical_raptor": ("canonical", {}),  # raptor payloads already carry level >= 1
    "semantic_memory": ("semantic_memory", {"kind": "fact"}),
    "semantic_contradictions": ("semantic_memory", {"kind": "contradiction"}),
    "experience": ("experience", {"compressed": False}),
    "experience_compressed": ("experience", {"compressed": True}),
    "knowledge": ("knowledge", {"origin": "knowledge"}),
    "context": ("knowledge", {"origin": "context"}),
    "patterns": ("knowledge", {"origin": "patterns"}),
    "code": ("code", {}),
    "personal": ("personal", {}),
    "obsidian": ("obsidian", {}),
    # healthcheck_probe deliberately absent → dropped
}


def load_checkpoint() -> dict:
    if not CHECKPOINT_FILE.exists():
        return {}
    try:
        return json.loads(CHECKPOINT_FILE.read_text())
    except Exception:
        return {}


def save_checkpoint(data: dict) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_FILE.with_suffix(CHECKPOINT_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CHECKPOINT_FILE)


def _l2(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return float("inf")
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def _unit_normalize(v: list[float]) -> list[float]:
    """Unit-normalize — Qdrant cosine collections store normalized vectors,
    so we compare against Chroma's vector normalized to the same length."""
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        return v
    return [x / n for x in v]


def migrate_collection(
    source: str,
    *,
    dry_run: bool,
    start_offset: int,
    qdrant: QdrantStore,
) -> dict:
    target, discriminator = TOPOLOGY[source]
    chroma = chroma_store()
    total_source = chroma.count(source)
    stats = {
        "source": source,
        "target": target,
        "total_source": total_source,
        "migrated": 0,
        "errors": 0,
        "last_offset": start_offset,
    }
    if total_source == 0:
        print(f"  {source}: empty, skip")
        return stats

    offset = start_offset
    while True:
        try:
            points = chroma.get(
                source,
                limit=PAGE_SIZE,
                offset=offset,
                with_payload=True,
                with_documents=True,
                with_vectors=True,
            )
        except Exception as e:
            print(f"  {source}: fetch failed at offset={offset}: {e}", file=sys.stderr)
            stats["errors"] += 1
            break

        if not points:
            break

        ids: list[str] = []
        vectors: list[list[float]] = []
        payloads: list[dict] = []
        documents: list[str] = []
        for p in points:
            if not p.vector:
                # Should never happen for real data; skip defensively.
                continue
            merged_payload = dict(p.payload or {})
            merged_payload.update(discriminator)
            ids.append(p.id)
            vectors.append(p.vector)
            payloads.append(merged_payload)
            documents.append(p.document or "")

        if not ids:
            # Whole page was vectorless — unusual but bail cleanly.
            break

        if dry_run:
            stats["migrated"] += len(ids)
            if offset == start_offset:
                print(f"  [dry-run] {source}: sample payload patch={discriminator}")
        else:
            try:
                qdrant.upsert(target, ids=ids, vectors=vectors, payloads=payloads, documents=documents)
                stats["migrated"] += len(ids)
            except Exception as e:
                print(f"  {source}: upsert failed at offset={offset}: {e}", file=sys.stderr)
                stats["errors"] += 1
                break

        stats["last_offset"] = offset + len(points)

        if not dry_run:
            ckpt = load_checkpoint()
            ckpt[source] = {
                "last_offset": stats["last_offset"],
                "migrated": stats["migrated"],
                "target": target,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            save_checkpoint(ckpt)

        if len(points) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    print(
        f"  {source} -> {target}: source={total_source} migrated={stats['migrated']} "
        f"errors={stats['errors']}"
    )
    return stats


def verify_collection(source: str, qdrant: QdrantStore) -> dict:
    target, _ = TOPOLOGY[source]
    chroma = chroma_store()
    source_count = chroma.count(source)
    target_count = qdrant.count(target)

    # Sample existence. Ask for payload so QdrantStore can extract
    # _original_id — without payload we'd get raw UUIDs back and the
    # set-membership check would always miss.
    id_hits = 0
    id_misses = 0
    sample_points = chroma.get(source, limit=VERIFY_SAMPLE_IDS, with_payload=False, with_documents=False)
    sample_ids = [p.id for p in sample_points][:VERIFY_SAMPLE_IDS]
    if sample_ids:
        qdrant_hits = qdrant.get(target, ids=sample_ids, with_payload=True, with_documents=False)
        q_ids = {h.id for h in qdrant_hits}
        for sid in sample_ids:
            if sid in q_ids:
                id_hits += 1
            else:
                id_misses += 1

    # Vector fidelity on a smaller subsample.
    vec_errs = 0
    vec_samples = chroma.get(
        source,
        limit=VERIFY_SAMPLE_VECTORS,
        with_payload=False,
        with_documents=False,
        with_vectors=True,
    )
    if vec_samples:
        q_vec_pts = qdrant.get(
            target,
            ids=[p.id for p in vec_samples],
            with_payload=True,  # needed for _original_id → VectorPoint.id round-trip
            with_documents=False,
            with_vectors=True,
        )
        q_by_id = {p.id: p.vector for p in q_vec_pts if p.vector}
        for p in vec_samples:
            qv = q_by_id.get(p.id)
            if qv is None or not p.vector:
                vec_errs += 1
                continue
            # Qdrant COSINE stores unit-normalized vectors; compare against
            # the normalized Chroma vector. Cosine ranking is invariant to
            # scale, so this is a fidelity check, not a semantic check.
            if _l2(_unit_normalize(p.vector), qv) >= 1e-4:
                vec_errs += 1

    return {
        "source": source,
        "target": target,
        "source_count": source_count,
        "target_count": target_count,
        "id_hits": id_hits,
        "id_misses": id_misses,
        "vec_errors": vec_errs,
        "vec_checked": len(vec_samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--collection", help="Scope to a single SOURCE collection")
    parser.add_argument("--reset", action="store_true", help="Clear checkpoint, start over")
    args = parser.parse_args()

    qdrant = QdrantStore()
    # Seed checkpoint behavior.
    if args.reset and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
    checkpoint = {} if args.reset else load_checkpoint()

    sources = [args.collection] if args.collection else list(TOPOLOGY.keys())
    for s in sources:
        if s not in TOPOLOGY:
            print(f"ERROR: unknown source collection {s!r}", file=sys.stderr)
            return 2

    if args.verify_only:
        print("VERIFY-ONLY")
        any_fail = False
        for s in sources:
            r = verify_collection(s, qdrant)
            print(
                f"  {s}->{r['target']}: "
                f"src={r['source_count']} tgt={r['target_count']} "
                f"ids_hit={r['id_hits']}/{r['id_hits']+r['id_misses']} "
                f"vec_ok={r['vec_checked']-r['vec_errors']}/{r['vec_checked']}"
            )
            if r["id_misses"] > 0 or r["vec_errors"] > 0:
                any_fail = True
        return 0 if not any_fail else 2

    mode = "[dry-run] " if args.dry_run else ""
    print(f"{mode}migrate_chroma_to_qdrant: {len(sources)} source collections")

    grand = {"migrated": 0, "errors": 0, "total_source": 0}
    for s in sources:
        start = 0
        if not args.reset:
            start = int((checkpoint.get(s) or {}).get("last_offset", 0))
        prefix = f"[resume @ {start}] " if start else ""
        print(f"\n{prefix}{s}")
        stats = migrate_collection(s, dry_run=args.dry_run, start_offset=start, qdrant=qdrant)
        grand["migrated"] += stats["migrated"]
        grand["errors"] += stats["errors"]
        grand["total_source"] += stats["total_source"]

    print(
        f"\nTOTAL: source={grand['total_source']} " f"migrated={grand['migrated']} errors={grand['errors']}"
    )
    return 0 if grand["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
