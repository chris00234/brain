from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Data directory — the knowledge/ tree (canonical, distilled, raw, reports, schemas).
# Code lives at /server/brain/pipeline/; data lives at /server/knowledge/.
try:
    from brain_core.config import KNOWLEDGE_DIR as ROOT
except ImportError:
    ROOT = Path("/Users/chrischo/server/knowledge")
SCHEMA_DIR = ROOT / "schemas"
SCHEMAS_DIR = SCHEMA_DIR
FRONTMATTER_PREFIXES = ("---json", "---")
TOKEN_RE = re.compile(r"[a-z0-9_\-]{2,}")
_KOREAN_RE = re.compile(r"[가-힣]{2,}")


class ValidationError(Exception):
    pass


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def parse_note(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text()
    lines = text.splitlines()
    if len(lines) < 3 or not lines[0].startswith(FRONTMATTER_PREFIXES):
        raise ValidationError(f"{path} is missing JSON frontmatter")

    end_index = None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            end_index = index
            break

    if end_index is None:
        raise ValidationError(f"{path} frontmatter is not terminated")

    metadata = json.loads("\n".join(lines[1:end_index]))
    body = "\n".join(lines[end_index + 1 :]).strip()
    return metadata, body


def render_note(metadata: dict[str, Any], body: str) -> str:
    return "---json\n" + json.dumps(metadata, indent=2, ensure_ascii=False) + f"\n---\n{body.rstrip()}\n"


def parse_markdown_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    return parse_note(path)


def write_markdown_frontmatter(path: Path, metadata: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(metadata, body))


def validate_schema(schema: dict[str, Any], payload: Any, path: str = "root") -> None:
    expected_type = schema.get("type")
    if expected_type is not None:
        validate_type(expected_type, payload, path)

    if "const" in schema and payload != schema["const"]:
        raise ValidationError(f"{path} must equal {schema['const']!r}")

    if "enum" in schema and payload not in schema["enum"]:
        raise ValidationError(f"{path} must be one of {schema['enum']!r}")

    if isinstance(payload, str):
        if schema.get("minLength") and len(payload) < schema["minLength"]:
            raise ValidationError(f"{path} must be at least {schema['minLength']} chars")
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, payload):
            raise ValidationError(f"{path} does not match pattern {pattern}")
        if schema.get("format") == "date-time":
            validate_datetime(payload, path)

    if isinstance(payload, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(payload) < min_items:
            raise ValidationError(f"{path} must contain at least {min_items} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(payload):
                validate_schema(item_schema, item, f"{path}[{index}]")

    if isinstance(payload, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in payload:
                raise ValidationError(f"{path}.{key} is required")

        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            extra = set(payload) - allowed
            if extra:
                raise ValidationError(f"{path} has unexpected fields: {sorted(extra)}")

        properties = schema.get("properties", {})
        for key, value in payload.items():
            if key in properties:
                validate_schema(properties[key], value, f"{path}.{key}")


def validate_type(expected: Any, payload: Any, path: str) -> None:
    options = expected if isinstance(expected, list) else [expected]
    for option in options:
        if option == "object" and isinstance(payload, dict):
            return
        if option == "array" and isinstance(payload, list):
            return
        if option == "string" and isinstance(payload, str):
            return
        if option == "number" and isinstance(payload, (int, float)) and not isinstance(payload, bool):
            return
        if option == "null" and payload is None:
            return
        if option == "boolean" and isinstance(payload, bool):
            return
    raise ValidationError(f"{path} must be of type {expected!r}")


def validate_datetime(value: str, path: str) -> None:
    candidate = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValidationError(f"{path} must be ISO-8601 date-time") from error


def iter_note_files(root: Path, folder: str) -> list[Path]:
    base = root / folder
    return sorted(path for path in base.rglob("*.md") if path.is_file())


def iter_note_paths(root: Path) -> list[Path]:
    """Iterate schema-compliant notes under root, skipping index / README files."""
    _SKIP = {"index.md", "readme.md", "README.md"}
    return sorted(path for path in root.rglob("*.md") if path.is_file() and path.name not in _SKIP)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return slug.strip("-")


def validate_required_fields(metadata: dict[str, Any], required: list[str], path: Path) -> list[str]:
    return [f"{path}: missing required field '{field}'" for field in required if field not in metadata]


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower()) + _KOREAN_RE.findall(text or "")


def read_all_notes(root: Path) -> list[dict[str, Any]]:
    """Walk distilled + canonical, skip notes with bad/missing frontmatter.

    A single malformed file must not crash the entire walk — memory_observability
    and lint_memory jobs depend on this being resilient.
    """
    import logging

    log = logging.getLogger("brain.pipeline.read_all_notes")
    notes = []
    for folder in ("distilled", "canonical"):
        for path in iter_note_files(root, folder):
            try:
                metadata, body = parse_note(path)
            except Exception as e:
                log.debug("skipping malformed note %s: %s", path, e)
                continue
            notes.append({"path": str(path.relative_to(root)), "metadata": metadata, "body": body})
    return notes


def find_similar_canonical(
    title: str, body: str, canonical_dir: Path | None = None, threshold: float = 0.7
) -> Path | None:
    """Check if a canonical note with similar content already exists.
    Returns the path of the match, or None."""
    if canonical_dir is None:
        canonical_dir = ROOT / "canonical"
    content_tokens = set(TOKEN_RE.findall((title + " " + body).lower()[:1000]))
    if len(content_tokens) < 5:
        return None
    for md_file in canonical_dir.rglob("*.md"):
        try:
            text = md_file.read_text(errors="replace")
            existing_tokens = set(TOKEN_RE.findall(text.lower()[:1000]))
            if not existing_tokens:
                continue
            jaccard = len(content_tokens & existing_tokens) / max(len(content_tokens | existing_tokens), 1)
            if jaccard > threshold:
                return md_file
        except Exception:
            continue
    return None


def build_keyword_index(notes: list[dict[str, Any]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for note in notes:
        note_id = note["metadata"]["id"]
        tokens = set(tokenize(note["metadata"]["title"] + " " + note["body"]))
        for token in sorted(tokens):
            index[token].append(note_id)
    return dict(sorted(index.items()))
