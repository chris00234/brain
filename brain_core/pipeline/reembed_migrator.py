#!/opt/homebrew/bin/python3
"""Shadow-collection re-embedding migrator.

Usage: reembed_migrator.py <collection_name> [--dry-run]
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexer import EMBED_MODEL_VERSION, get_embeddings_batch
from vector_store import get_vector_store


def collection_exists(name: str) -> bool:
    return name in get_vector_store().list_collections()


def create_collection(name: str) -> str:
    get_vector_store().create_collection(name, {"source": "reembed_migrator"})
    return name


def fetch_all_docs(collection: str, batch: int = 500):
    # Single-call full scan — QdrantStore.get walks the native cursor.
    # `batch` kept as a parameter for API compat but is no longer used.
    del batch
    store = get_vector_store()
    points = store.get(
        collection,
        limit=1_000_000,
        with_payload=True,
        with_documents=True,
    )
    for p in points:
        yield p.id, (p.document or ""), (p.payload or {})


def _embed_batch(docs: list, model: str = "") -> list:
    """Embed a batch of docs. Uses LoRA adapter if model starts with 'lora:'."""
    if model.startswith("lora:"):
        from lora_embedder import get_lora_embeddings_batch

        adapter_path = model[5:]
        return get_lora_embeddings_batch(docs, adapter_path, prefix="passage")
    return get_embeddings_batch(docs, prefix="passage")


def _flush_batch(shadow_name: str, ids: list, docs: list, metas: list, model: str = "") -> int:
    """Embed + upsert a batch, filtering out empty embeddings. Returns count written."""
    if not ids:
        return 0
    embs = _embed_batch(docs, model=model)
    valid = [(i, d, m, e) for i, d, m, e in zip(ids, docs, metas, embs, strict=False) if e]
    if not valid:
        print(f"  WARNING: entire batch of {len(ids)} failed embedding, skipped")
        return 0
    if len(valid) < len(ids):
        print(f"  WARNING: {len(ids) - len(valid)} docs in batch failed embedding, skipped")
    v_ids, v_docs, v_metas, v_embs = zip(*valid, strict=False)
    try:
        get_vector_store().upsert(
            shadow_name,
            ids=list(v_ids),
            vectors=list(v_embs),
            documents=list(v_docs),
            payloads=list(v_metas),
        )
        return len(valid)
    except Exception as e:
        print(f"  upsert failed: {e}")
        return 0


def _derive_shadow_name(collection: str, model: str) -> str:
    """Shadow name. Default '{collection}_shadow', LoRA '{collection}_lora_<version>'."""
    if not model.startswith("lora:"):
        return f"{collection}_shadow"
    # Extract the last path component as the version tag, e.g. logs/training/lora_v1/ → lora_v1
    path = model[5:].rstrip("/")
    tag = Path(path).name or "lora"
    if not tag.startswith("lora"):
        tag = f"lora_{tag}"
    return f"{collection}_{tag}"


def main():
    import argparse

    from _watchdog import arm as _arm_watchdog

    # Re-embeds can genuinely take a long time on large collections; 30min cap.
    _arm_watchdog(1800, tag="reembed_migrator")
    parser = argparse.ArgumentParser(description="Re-embed a collection with current model")
    parser.add_argument("collection", help="Source collection name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--model", default="", help="Override embedding model. Use 'lora:<path>' for LoRA adapter"
    )
    args = parser.parse_args()

    if not collection_exists(args.collection):
        print(f"ERROR: collection {args.collection} not found")
        return 2

    shadow_name = _derive_shadow_name(args.collection, args.model)
    if collection_exists(shadow_name):
        print(f"ERROR: shadow collection {shadow_name} already exists — delete it first")
        return 2

    print(f"Migrating {args.collection} → {shadow_name}")
    if args.model.startswith("lora:"):
        print(f"Target embed model: {args.model}")
    else:
        print(f"Target embed model: {EMBED_MODEL_VERSION}")

    if args.dry_run:
        print("[DRY RUN] Would create shadow + re-embed all docs")
        return 0

    # Create shadow
    create_collection(shadow_name)

    def _delete_shadow():
        """Attempt to remove a partial shadow on failure. Best-effort.

        VectorStore doesn't expose collection-level delete in the protocol;
        we rely on the backend-specific CLI for shadow teardown in the
        rollback path. Note this in the log so operators know where to look.
        """
        print(
            f"  rollback: manual shadow cleanup needed — run `qdrant-cli drop {shadow_name}` "
            f"(or ChromaDB equivalent) before retrying."
        )

    # Migrate in batches of 100
    total = 0
    model_version_tag = args.model if args.model.startswith("lora:") else EMBED_MODEL_VERSION
    batch_docs, batch_metas, batch_ids = [], [], []
    try:
        for doc_id, content, meta in fetch_all_docs(args.collection):
            batch_ids.append(doc_id)
            batch_docs.append(content or "")
            meta = dict(meta or {})
            meta["embed_model_version"] = model_version_tag
            meta["reembedded_at"] = datetime.now().isoformat()
            batch_metas.append(meta)

            if len(batch_ids) >= 100:
                total += _flush_batch(shadow_name, batch_ids, batch_docs, batch_metas, model=args.model)
                print(f"  migrated {total} docs...")
                batch_docs, batch_metas, batch_ids = [], [], []

        # Final batch
        if batch_ids:
            total += _flush_batch(shadow_name, batch_ids, batch_docs, batch_metas, model=args.model)
    except KeyboardInterrupt:
        print("\nINTERRUPTED — rolling back partial shadow")
        _delete_shadow()
        return 130
    except Exception as e:
        print(f"\nERROR mid-migration: {e}")
        _delete_shadow()
        return 2

    print(f"\nMigrated {total} documents to {shadow_name}")
    print("\nNext steps (manual):")
    print("  1. Verify shadow with eval set")
    print(
        f"  2. Atomic rename: {args.collection} → {args.collection}_v1_backup, {shadow_name} → {args.collection}"
    )
    print("  3. Keep backup for 30 days before delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
