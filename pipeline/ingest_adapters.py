from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import ROOT, SCHEMA_DIR, dump_json, load_json, sha256_text, slugify, utc_now, validate_schema


def build_raw_record(*, record_id: str, source_type: str, source_ref: str, actor: str, visibility: str, content: str, entities: list[str] | None = None, scrub_status: str = "scrubbed") -> dict[str, Any]:
    return {
        "id": record_id,
        "timestamp": utc_now(),
        "source_type": source_type,
        "source_ref": source_ref,
        "actor": actor,
        "visibility": visibility,
        "scrub_status": scrub_status,
        "content": content,
        "attachments": [],
        "entities": entities or [],
        "hash": sha256_text(content),
    }


def validate_and_write(record: dict[str, Any], output_dir: Path, schema: dict[str, Any]) -> Path:
    validate_schema(schema, record)
    output_path = output_dir / f"{record['id']}.json"
    dump_json(output_path, record)
    return output_path


def ingest_chat_jsonl(args: argparse.Namespace, schema: dict[str, Any]) -> dict[str, Any]:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    for line_number, line in enumerate(args.input_file.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        content = str(payload.get("content", "")).strip()
        if not content:
            continue
        actor = str(payload.get("actor") or payload.get("sender") or "unknown")
        message_id = str(payload.get("message_id") or payload.get("id") or line_number)
        entities = payload.get("entities") or [actor]
        record_id = f"raw_chat_{slugify(args.source_name)}_{message_id}"
        record = build_raw_record(
            record_id=record_id,
            source_type=f"chat:{args.source_name}",
            source_ref=f"{args.source_name}:{message_id}",
            actor=actor,
            visibility=args.visibility,
            content=content,
            entities=[str(entity) for entity in entities],
            scrub_status=args.scrub_status,
        )
        created.append(str(validate_and_write(record, output_dir, schema)))

    manifest = {
        "adapter": "chat-jsonl",
        "source_name": args.source_name,
        "input_file": str(args.input_file),
        "created": created,
        "count": len(created),
    }
    manifest_path = output_dir / f"manifest_chat_{slugify(args.source_name)}.json"
    dump_json(manifest_path, manifest)
    return manifest


def ingest_document_text(args: argparse.Namespace, schema: dict[str, Any]) -> dict[str, Any]:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    content = args.input_file.read_text().strip()
    document_name = args.document_name or args.input_file.stem
    record_id = f"raw_doc_{slugify(document_name)}"
    record = build_raw_record(
        record_id=record_id,
        source_type="document:text",
        source_ref=str(args.input_file),
        actor=args.actor,
        visibility=args.visibility,
        content=content,
        entities=[args.actor, document_name],
        scrub_status=args.scrub_status,
    )
    output_path = validate_and_write(record, output_dir, schema)
    manifest = {
        "adapter": "document-text",
        "document_name": document_name,
        "input_file": str(args.input_file),
        "created": [str(output_path)],
        "count": 1,
    }
    manifest_path = output_dir / f"manifest_doc_{slugify(document_name)}.json"
    dump_json(manifest_path, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingestion adapters for chat and document sources")
    subparsers = parser.add_subparsers(dest="adapter", required=True)

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("input_file", type=Path)
    common_parser.add_argument("--output-dir", type=Path, default=ROOT / "raw" / "inbox")
    common_parser.add_argument("--visibility", default="private", choices=["public", "internal", "private", "secret-ref"])
    common_parser.add_argument("--scrub-status", default="scrubbed", choices=["pending", "scrubbed", "quarantined"])

    chat_parser = subparsers.add_parser("chat-jsonl", parents=[common_parser], help="ingest chat messages from JSONL")
    chat_parser.add_argument("--source-name", default="chat")

    doc_parser = subparsers.add_parser("document-text", parents=[common_parser], help="ingest plain text document")
    doc_parser.add_argument("--document-name")
    doc_parser.add_argument("--actor", default="chris")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    schema = load_json(SCHEMA_DIR / "raw.schema.json")

    if args.adapter == "chat-jsonl":
        manifest = ingest_chat_jsonl(args, schema)
    else:
        manifest = ingest_document_text(args, schema)

    print(json.dumps({"status": "ok", "manifest": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
