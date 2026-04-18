from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT, SCHEMA_DIR, ValidationError, dump_json, load_json, validate_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or validate raw inbox records.")
    parser.add_argument("--content", help="Raw content to ingest.")
    parser.add_argument("--input-file", type=Path, help="Existing raw JSON file to validate and copy.")
    parser.add_argument("--source-type", default="manual")
    parser.add_argument("--source-ref", default="manual:unknown")
    parser.add_argument("--actor", default="chris")
    parser.add_argument("--visibility", default="private")
    parser.add_argument("--scrub-status", default="scrubbed")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "raw" / "inbox")
    return parser.parse_args()


def build_record(args: argparse.Namespace) -> dict[str, object]:
    if args.input_file:
        return load_json(args.input_file)
    if not args.content:
        raise ValidationError("either --content or --input-file is required")

    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha256(args.content.encode()).hexdigest()
    return {
        "id": f"raw_{timestamp[:10].replace('-', '_')}_{digest[:8]}",
        "timestamp": timestamp,
        "source_type": args.source_type,
        "source_ref": args.source_ref,
        "actor": args.actor,
        "visibility": args.visibility,
        "scrub_status": args.scrub_status,
        "content": args.content,
        "attachments": [],
        "entities": ["Chris"],
        "hash": f"sha256:{digest}",
    }


def main() -> int:
    args = parse_args()
    schema = load_json(SCHEMA_DIR / "raw.schema.json")
    record = build_record(args)
    validate_schema(schema, record)
    output_path = args.output_dir / f"{record['id']}.json"
    dump_json(output_path, record)
    print(json.dumps({"status": "ok", "output": str(output_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
