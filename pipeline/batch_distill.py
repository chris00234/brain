from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import ROOT, SCHEMA_DIR, iter_note_paths, load_json, parse_markdown_frontmatter, slugify, utc_now, validate_schema, write_markdown_frontmatter

DOMAIN_KEYWORDS = {
    "infra": ["deploy", "server", "docker", "nginx", "cloudflare", "chromadb", "ollama", "infra"],
    "decisions": ["decide", "decision", "deprecated", "policy", "workflow", "architecture"],
    "incidents": ["incident", "error", "failed", "failure", "outage", "stale", "bug"],
    "projects": ["project", "build", "feature", "implementation", "system", "memory"],
}


def infer_domain(raw_record: dict[str, Any]) -> str:
    content = str(raw_record.get("content", "")).lower()
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(keyword in content for keyword in keywords):
            return domain
    return "chris" if "Chris" in raw_record.get("entities", []) else "projects"


def infer_subtype(domain: str, raw_record: dict[str, Any]) -> str:
    content = str(raw_record.get("content", "")).lower()
    if domain == "chris":
        return "preference" if "prefer" in content or "좋" in content else "observation"
    if domain == "decisions":
        return "workflow" if "workflow" in content or "deprecated" in content else "proposal"
    if domain == "incidents":
        return "incident"
    if domain == "infra":
        return "stack"
    return "project-memory"


def infer_title(raw_record: dict[str, Any], domain: str) -> str:
    content = str(raw_record.get("content", "")).strip()
    first = content.splitlines()[0].strip() if content else raw_record["id"]
    compact = " ".join(first.split())[:72].strip(" .")
    if compact:
        return compact[0].upper() + compact[1:]
    return f"{domain.title()} memory"


def infer_entities(raw_record: dict[str, Any]) -> list[str]:
    entities = [str(entity) for entity in raw_record.get("entities", [])]
    if "Chris" not in entities:
        entities.insert(0, "Chris")
    return entities


def existing_source_map(distilled_root: Path) -> set[str]:
    seen: set[str] = set()
    for path in iter_note_paths(distilled_root):
        metadata, _body = parse_markdown_frontmatter(path)
        for source in metadata.get("sources", []):
            seen.add(source)
    return seen


def build_distilled(raw_record: dict[str, Any], domain: str, subtype: str, title: str) -> tuple[dict[str, Any], str]:
    note_id = f"dist_{slugify(title).replace('-', '_')}"
    metadata = {
        "id": note_id,
        "type": "distilled",
        "domain": domain,
        "subtype": subtype,
        "title": title,
        "status": "active",
        "visibility": raw_record["visibility"],
        "confidence": 0.75,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "sources": [raw_record["id"]],
        "provenance_summary": f"Distilled from {raw_record['source_type']} evidence {raw_record['source_ref']}.",
        "entities": infer_entities(raw_record),
        "relations": [],
        "review_state": "proposed",
    }
    body = (
        "# Summary\n\n"
        f"{raw_record['content']}\n\n"
        "## Observations\n"
        "- Derived from raw evidence.\n"
        f"- Candidate domain: {domain}.\n"
    )
    return metadata, body


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-distill raw inbox records into distilled notes")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "raw" / "inbox")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "distilled")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    raw_schema = load_json(SCHEMA_DIR / "raw.schema.json")
    distilled_schema = load_json(SCHEMA_DIR / "distilled.schema.json")
    seen_sources = existing_source_map(args.output_dir)
    created: list[str] = []
    skipped: list[str] = []

    for raw_path in sorted(args.input_dir.glob("raw_*.json")):
        raw_record = load_json(raw_path)
        validate_schema(raw_schema, raw_record)
        raw_id = raw_record["id"]
        if raw_id in seen_sources:
            skipped.append(raw_id)
            continue

        domain = infer_domain(raw_record)
        subtype = infer_subtype(domain, raw_record)
        title = infer_title(raw_record, domain)
        metadata, body = build_distilled(raw_record, domain, subtype, title)
        validate_schema(distilled_schema, metadata)

        target_dir = args.output_dir / domain
        target_path = target_dir / f"{metadata['id']}.md"
        suffix = 1
        while target_path.exists():
            if suffix > 100:
                print(f"    WARN: too many collisions for {title!r}, skipping {raw_id}")
                skipped.append(raw_id)
                break
            try:
                existing_meta, _ = parse_markdown_frontmatter(target_path)
            except Exception:
                # Corrupted frontmatter — treat as collision, increment suffix
                metadata["id"] = f"dist_{slugify(title).replace('-', '_')}_{suffix}"
                target_path = target_dir / f"{metadata['id']}.md"
                suffix += 1
                continue
            if raw_id in existing_meta.get("sources", []):
                skipped.append(raw_id)
                seen_sources.add(raw_id)
                break
            metadata["id"] = f"dist_{slugify(title).replace('-', '_')}_{suffix}"
            target_path = target_dir / f"{metadata['id']}.md"
            suffix += 1
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            write_markdown_frontmatter(target_path, metadata, body)
            created.append(str(target_path))
            seen_sources.add(raw_id)
            continue
        # only reached when skipped via matching existing source
        continue

    manifest = {
        "status": "ok",
        "input_dir": str(args.input_dir),
        "output_dir": str(args.output_dir),
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }
    manifest_path = args.manifest or (ROOT / "reports" / "review-queue" / "batch_distill_manifest.json")
    write_target = manifest_path if manifest_path.is_absolute() else manifest_path
    write_target.parent.mkdir(parents=True, exist_ok=True)
    write_target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
