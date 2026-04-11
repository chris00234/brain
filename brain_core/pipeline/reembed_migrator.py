#!/opt/homebrew/bin/python3
"""Shadow-collection re-embedding migrator.

Usage: reembed_migrator.py <collection_name> [--dry-run]
"""
import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from http_pool import http_json
from indexer import get_embeddings_batch, EMBED_MODEL, EMBED_MODEL_VERSION

CHROMA_URL = "http://127.0.0.1:8000"
CHROMA_API = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections"

def get_collection_id(name: str) -> str | None:
    cols = http_json("GET", CHROMA_API)
    if isinstance(cols, list):
        for c in cols:
            if c.get("name") == name:
                return c.get("id")
    return None

def create_collection(name: str) -> str | None:
    resp = http_json("POST", CHROMA_API, payload={"name": name, "metadata": {"source": "reembed_migrator"}})
    return resp.get("id") if isinstance(resp, dict) else None

def fetch_all_docs(col_id: str, batch: int = 500):
    offset = 0
    while True:
        resp = http_json("POST", f"{CHROMA_API}/{col_id}/get",
            {"limit": batch, "offset": offset, "include": ["documents", "metadatas"]})
        ids = resp.get("ids", [])
        if not ids:
            break
        docs = resp.get("documents", []) or []
        metas = resp.get("metadatas", []) or []
        for i, d, m in zip(ids, docs, metas):
            yield i, d, m
        if len(ids) < batch:
            break
        offset += batch

def _embed_batch(docs: list, model: str = "") -> list:
    """Embed a batch of docs. Uses LoRA adapter if model starts with 'lora:'."""
    if model.startswith("lora:"):
        from lora_embedder import get_lora_embeddings_batch
        adapter_path = model[5:]
        return get_lora_embeddings_batch(docs, adapter_path, prefix="passage")
    return get_embeddings_batch(docs, prefix="passage")


def _flush_batch(shadow_id: str, ids: list, docs: list, metas: list, model: str = "") -> int:
    """Embed + upsert a batch, filtering out empty embeddings. Returns count written."""
    if not ids:
        return 0
    embs = _embed_batch(docs, model=model)
    valid = [(i, d, m, e) for i, d, m, e in zip(ids, docs, metas, embs) if e]
    if not valid:
        print(f"  WARNING: entire batch of {len(ids)} failed embedding, skipped")
        return 0
    if len(valid) < len(ids):
        print(f"  WARNING: {len(ids) - len(valid)} docs in batch failed embedding, skipped")
    v_ids, v_docs, v_metas, v_embs = zip(*valid)
    try:
        http_json("POST", f"{CHROMA_API}/{shadow_id}/upsert", {
            "ids": list(v_ids),
            "embeddings": list(v_embs),
            "documents": list(v_docs),
            "metadatas": list(v_metas),
        })
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
    parser = argparse.ArgumentParser(description="Re-embed a collection with current model")
    parser.add_argument("collection", help="Source collection name")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="", help="Override embedding model. Use 'lora:<path>' for LoRA adapter")
    args = parser.parse_args()

    source_id = get_collection_id(args.collection)
    if not source_id:
        print(f"ERROR: collection {args.collection} not found")
        return 2

    shadow_name = _derive_shadow_name(args.collection, args.model)
    existing_shadow = get_collection_id(shadow_name)
    if existing_shadow:
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
    shadow_id = create_collection(shadow_name)
    if not shadow_id:
        print(f"ERROR: failed to create shadow collection")
        return 2

    def _delete_shadow():
        """Attempt to remove a partial shadow on failure. Best-effort.

        ChromaDB v2 native mode DELETE uses the collection NAME, not the UUID
        (verified against 0.5.x native server — DELETE by UUID returns 404).
        """
        try:
            http_json("DELETE", f"{CHROMA_API}/{shadow_name}")
            print(f"  rolled back: deleted partial shadow {shadow_name}")
        except Exception as e:
            print(f"  WARNING: could not delete partial shadow {shadow_name}: {e}")

    # Migrate in batches of 100
    total = 0
    model_version_tag = args.model if args.model.startswith("lora:") else EMBED_MODEL_VERSION
    batch_docs, batch_metas, batch_ids = [], [], []
    try:
        for doc_id, content, meta in fetch_all_docs(source_id):
            batch_ids.append(doc_id)
            batch_docs.append(content or "")
            meta = dict(meta or {})
            meta["embed_model_version"] = model_version_tag
            meta["reembedded_at"] = datetime.now().isoformat()
            batch_metas.append(meta)

            if len(batch_ids) >= 100:
                total += _flush_batch(shadow_id, batch_ids, batch_docs, batch_metas, model=args.model)
                print(f"  migrated {total} docs...")
                batch_docs, batch_metas, batch_ids = [], [], []

        # Final batch
        if batch_ids:
            total += _flush_batch(shadow_id, batch_ids, batch_docs, batch_metas, model=args.model)
    except KeyboardInterrupt:
        print("\nINTERRUPTED — rolling back partial shadow")
        _delete_shadow()
        return 130
    except Exception as e:
        print(f"\nERROR mid-migration: {e}")
        _delete_shadow()
        return 2

    print(f"\nMigrated {total} documents to {shadow_name}")
    print(f"\nNext steps (manual):")
    print(f"  1. Verify shadow with eval set")
    print(f"  2. Atomic rename: {args.collection} → {args.collection}_v1_backup, {shadow_name} → {args.collection}")
    print(f"  3. Keep backup for 30 days before delete")
    return 0

if __name__ == "__main__":
    sys.exit(main())
