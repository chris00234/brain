#!/opt/homebrew/bin/python3
"""cli/normalize_payloads.py — Phase A4 of the Qdrant migration.

Fixes the three Phase 1 payload-schema drift findings before dual-write
turns on:

1. `confidence` / `trust_score` / `quality_score` are stored as strings
   (e.g., ``str(round(x, 3))``) in ChromaDB but as REAL columns in
   brain.db atoms. Qdrant's payload indexes require typed fields — the
   mismatch would break `confidence > 0.7` range filters after cutover.
   Cast every string value back to float in place.

2. `code` and `personal` collections never wrote `embed_model` or
   `embed_model_version` (see Phase 1 audit — ``ingest/code_repos.py``
   pre-migration had no `embed_model` meta, ``ingest/personal.py`` had
   `embed_model` but no `embed_model_version`). Staleness detection is
   invisible on those rows without the version. Backfill both keys
   using ``EMBED_MODEL`` / ``EMBED_MODEL_VERSION`` from ``config.py``.

3. `line_start` on `code` rows is already int — leave it. Spot-check
   it stays int after the run.

Idempotency: every pass records the last-seen collection/offset in
``~/server/brain/logs/normalize_checkpoint.json`` so a rerun continues
instead of re-writing already-normalized points. Points whose payload
is already typed correctly are skipped (the check is "is confidence a
float?"; if yes, we write nothing).

Usage:
    python cli/normalize_payloads.py --dry-run       # report, no writes
    python cli/normalize_payloads.py                 # real run, all collections
    python cli/normalize_payloads.py --collection code  # scope to one

Plan: ~/.claude/plans/toasty-snacking-shamir.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from config import EMBED_MODEL, EMBED_MODEL_VERSION  # noqa: E402
from vector_store import get_vector_store  # noqa: E402

CHECKPOINT_FILE = BRAIN_ROOT / "logs" / "normalize_checkpoint.json"
PAGE_SIZE = 500

# Fields stored as strings that must become float under Qdrant payload indexes.
# Matches the writers in learn.py, memory_lifecycle.py, entity_graph.py,
# and server.py: confidence/trust_score are round(x, 3) stringified,
# quality_score occasionally stored the same way.
FLOAT_FIELDS = ("confidence", "trust_score", "quality_score")

# Collections missing embed_model / embed_model_version from their metadata
# schema. code was written by ingest/code_repos.py and never carried either
# field; personal was written by ingest/personal.py and carries embed_model
# but not embed_model_version. Backfilling makes staleness detection
# (see memory_lifecycle.recompute_trust_scores and the version-aware
# reembed_migrator) work uniformly.
EMBED_META_BACKFILL_COLLECTIONS = ("code", "personal")


def _is_float(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _needs_float_cast(payload: dict) -> bool:
    for key in FLOAT_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return True
    return False


def _coerce_to_float(value: object, default: float = 0.5) -> float:
    if _is_float(value):
        return float(value)  # type: ignore[arg-type]
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _build_patch(payload: dict, collection: str) -> dict:
    """Compute the minimum payload delta that brings the point up to spec."""
    patch: dict = {}

    # 1. Float coercion — only write the fields that were strings.
    for key in FLOAT_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            try:
                patch[key] = float(value)
            except ValueError:
                patch[key] = 0.5
        elif isinstance(value, str):
            # Empty string → drop, not a useful default.
            patch[key] = 0.5

    # 2. embed_model / embed_model_version backfill on code/personal.
    if collection in EMBED_META_BACKFILL_COLLECTIONS:
        if not payload.get("embed_model"):
            patch["embed_model"] = EMBED_MODEL
        if not payload.get("embed_model_version"):
            patch["embed_model_version"] = EMBED_MODEL_VERSION

    return patch


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


def normalize_collection(collection: str, *, dry_run: bool, start_offset: int = 0) -> dict:
    """Walk a collection and patch payloads where needed.

    Returns a summary dict. Writes checkpoint after every page so the
    process is resumable on crash / ctrl-c.
    """
    store = get_vector_store()
    stats = {
        "collection": collection,
        "scanned": 0,
        "patched": 0,
        "skipped_clean": 0,
        "errors": 0,
        "last_offset": start_offset,
    }

    offset = start_offset
    while True:
        try:
            points = store.get(
                collection,
                limit=PAGE_SIZE,
                offset=offset,
                with_payload=True,
                with_documents=False,
            )
        except Exception as e:
            print(f"  {collection}: fetch failed at offset={offset}: {e}", file=sys.stderr)
            stats["errors"] += 1
            break

        if not points:
            break

        for p in points:
            stats["scanned"] += 1
            patch = _build_patch(p.payload or {}, collection)
            if not patch:
                stats["skipped_clean"] += 1
                continue

            if dry_run:
                stats["patched"] += 1
                if stats["patched"] <= 3:
                    print(f"    [dry-run] {p.id}: patch={patch}")
                continue

            try:
                store.update_payload(collection, ids=[p.id], patch=patch)
                stats["patched"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"  {collection}: patch failed for {p.id}: {e}", file=sys.stderr)

        stats["last_offset"] = offset + len(points)

        if not dry_run:
            checkpoint = load_checkpoint()
            checkpoint[collection] = {
                "last_offset": stats["last_offset"],
                "patched": stats["patched"],
                "scanned": stats["scanned"],
                "updated_at": datetime.now(UTC).isoformat(),
            }
            save_checkpoint(checkpoint)

        if len(points) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    parser.add_argument(
        "--collection",
        help="Normalize only this collection (default: every collection)",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Ignore saved checkpoint; start from offset=0",
    )
    args = parser.parse_args()

    store = get_vector_store()
    names = store.list_collections()
    if args.collection:
        if args.collection not in names:
            print(f"ERROR: collection {args.collection!r} not found", file=sys.stderr)
            return 2
        names = [args.collection]

    checkpoint = {} if args.reset_checkpoint else load_checkpoint()
    mode = "[dry-run] " if args.dry_run else ""
    print(f"{mode}normalize_payloads: {len(names)} collections")

    grand_total = {"scanned": 0, "patched": 0, "skipped_clean": 0, "errors": 0}
    for name in names:
        start = 0
        if not args.reset_checkpoint:
            start = int((checkpoint.get(name) or {}).get("last_offset", 0))
        prefix = f"[resume @ {start}] " if start else ""
        print(f"\n{prefix}{name}")
        stats = normalize_collection(name, dry_run=args.dry_run, start_offset=start)
        print(
            f"  scanned={stats['scanned']} "
            f"patched={stats['patched']} "
            f"clean={stats['skipped_clean']} "
            f"errors={stats['errors']}"
        )
        for key in ("scanned", "patched", "skipped_clean", "errors"):
            grand_total[key] += stats[key]

    print(
        f"\nTOTAL: scanned={grand_total['scanned']} "
        f"patched={grand_total['patched']} "
        f"clean={grand_total['skipped_clean']} "
        f"errors={grand_total['errors']}"
    )
    return 0 if grand_total["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
