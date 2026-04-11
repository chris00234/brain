from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import dump_json


def normalize_message(payload: dict[str, Any], fallback_index: int) -> dict[str, Any] | None:
    content = str(payload.get("content") or payload.get("text") or payload.get("message") or "").strip()
    if not content:
        return None
    actor = str(payload.get("actor") or payload.get("sender") or payload.get("author") or "unknown")
    message_id = str(payload.get("message_id") or payload.get("id") or fallback_index)
    entities = payload.get("entities") or [actor]
    return {
        "message_id": message_id,
        "actor": actor,
        "content": content,
        "entities": entities,
    }


def bridge_session_export(input_file: Path, output_file: Path) -> dict[str, Any]:
    payload = json.loads(input_file.read_text())
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        raise SystemExit("session export must be a JSON object with 'messages' or a list")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(messages, start=1):
        if not isinstance(item, dict):
            continue
        record = normalize_message(item, index)
        if record:
            normalized.append(record)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in normalized) + ("\n" if normalized else ""))
    manifest = {
        "status": "ok",
        "adapter": "session-export",
        "input_file": str(input_file),
        "output_file": str(output_file),
        "count": len(normalized),
    }
    dump_json(output_file.with_suffix(".manifest.json"), manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge exported session/chat data into adapter-ready JSONL")
    subparsers = parser.add_subparsers(dest="source", required=True)

    session_parser = subparsers.add_parser("session-export", help="convert exported session JSON into chat JSONL")
    session_parser.add_argument("input_file", type=Path)
    session_parser.add_argument("--output-file", type=Path, required=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.source == "session-export":
        manifest = bridge_session_export(args.input_file, args.output_file)
    else:
        raise SystemExit(f"Unsupported source: {args.source}")

    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
