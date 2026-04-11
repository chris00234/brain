from __future__ import annotations

import argparse
from pathlib import Path

from common import ROOT, iter_note_paths, load_json, parse_markdown_frontmatter, validate_required_fields

REQUIRED_BY_TYPE = {
    "distilled": load_json(ROOT / "schemas" / "distilled.schema.json")["required"],
    "canonical": load_json(ROOT / "schemas" / "canonical.schema.json")["required"],
}


def validate_note(path: Path) -> list[str]:
    metadata, body = parse_markdown_frontmatter(path)
    errors: list[str] = []
    note_type = metadata.get("type")
    if note_type not in REQUIRED_BY_TYPE:
        return [f"{path}: unsupported note type '{note_type}'"]
    errors.extend(validate_required_fields(metadata, REQUIRED_BY_TYPE[note_type], path))
    if not body:
        errors.append(f"{path}: empty body")
    if metadata.get("visibility") == "secret":
        errors.append(f"{path}: use 'secret-ref', never 'secret'")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate markdown notes with JSON frontmatter")
    parser.add_argument("paths", nargs="*", help="optional explicit note paths")
    args = parser.parse_args()

    note_paths = [Path(path) for path in args.paths] if args.paths else [
        *iter_note_paths(ROOT / "distilled"),
        *iter_note_paths(ROOT / "canonical"),
    ]
    errors: list[str] = []
    for path in note_paths:
        errors.extend(validate_note(path))

    if errors:
        print("VALIDATION_FAILED")
        for error in errors:
            print(error)
        return 1

    print(f"VALIDATION_OK {len(note_paths)} notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
