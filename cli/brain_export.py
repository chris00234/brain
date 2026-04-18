#!/opt/homebrew/bin/python3
"""Portable brain export — dumps all memories to JSONL for cross-machine sync.

Exports semantic_memory, canonical (markdown files), Neo4j entities/lessons.
Format: self-describing JSONL that can be imported into any compatible system.

Usage:
  brain_export.py --output ~/brain-export.tar.gz
  brain_export.py --output ~/brain-export/ --format dir
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from http_pool import http_json
from search import get_collections

CHROMA_URL = "http://127.0.0.1:8000"
CHROMA_API = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections"
KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")


def export_collection(col_name: str, col_id: str, out_file: Path, batch: int = 500) -> int:
    """Export all docs from a ChromaDB collection to JSONL."""
    count = 0
    offset = 0
    with out_file.open("w") as f:
        while True:
            try:
                resp = http_json(
                    "POST",
                    f"{CHROMA_API}/{col_id}/get",
                    {"limit": batch, "offset": offset, "include": ["documents", "metadatas", "embeddings"]},
                )
            except Exception as e:
                print(f"  {col_name}: fetch failed at offset {offset}: {e}", file=sys.stderr)
                break

            ids = resp.get("ids", [])
            if not ids:
                break
            docs = resp.get("documents", []) or []
            metas = resp.get("metadatas", []) or []
            embs = resp.get("embeddings", []) or []

            for i, (doc_id, doc, meta) in enumerate(zip(ids, docs, metas, strict=False)):
                record = {
                    "id": doc_id,
                    "content": doc,
                    "metadata": meta or {},
                }
                # Include embedding if small enough
                if i < len(embs) and embs[i]:
                    record["embedding"] = embs[i]
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1

            if len(ids) < batch:
                break
            offset += batch
    return count


def export_neo4j() -> dict:
    """Export Neo4j entities, lessons, and skill graph."""
    try:
        from neo4j_client import run_query

        entities = run_query(
            "MATCH (e:Entity) RETURN e.name AS name, e.entity_type AS type, "
            "e.mention_count AS mentions, properties(e) AS props LIMIT 10000"
        )
        lessons = run_query(
            "MATCH (l:Lesson) RETURN l.id AS id, l.task AS task, "
            "l.reflection AS reflection, l.agent_id AS agent, "
            "l.created_at AS created_at, l.failure_count AS failure_count LIMIT 5000"
        )
        skills = run_query(
            "MATCH (s:Skill) RETURN s.name AS name, s.description AS description, "
            "s.path AS path LIMIT 1000"
        )
        relations = run_query(
            "MATCH (a)-[r:RELATES_TO]->(b) RETURN a.name AS from_name, "
            "b.name AS to_name, r.weight AS weight, r.co_occurrence_count AS count "
            "LIMIT 50000"
        )
        return {
            "entities": entities,
            "lessons": lessons,
            "skills": skills,
            "relations": relations,
        }
    except Exception as e:
        print(f"Neo4j export failed: {e}", file=sys.stderr)
        return {"entities": [], "lessons": [], "skills": [], "relations": []}


def export_canonical(out_dir: Path) -> int:
    """Copy canonical + distilled markdown files."""
    src = KNOWLEDGE_DIR / "canonical"
    dst = out_dir / "canonical"
    count = 0
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        count = sum(1 for _ in dst.rglob("*.md"))

    src2 = KNOWLEDGE_DIR / "distilled"
    dst2 = out_dir / "distilled"
    if src2.exists():
        shutil.copytree(src2, dst2, dirs_exist_ok=True)
        count += sum(1 for _ in dst2.rglob("*.md"))
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="Output .tar.gz or directory")
    parser.add_argument("--format", choices=["tar", "dir"], default="tar")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip embeddings to reduce size")
    args = parser.parse_args()

    # Build export in temp dir
    tmpdir = Path(tempfile.mkdtemp(prefix="brain_export_"))
    try:
        manifest = {
            "exported_at": datetime.now(UTC).isoformat(),
            "format_version": "1.0",
            "source": "brain",
            "collections": {},
            "neo4j": {"entities": 0, "lessons": 0, "skills": 0, "relations": 0},
        }

        # ChromaDB collections
        print("Exporting ChromaDB collections...")
        cols = get_collections()
        for col_name, col_id in cols.items():
            out_file = tmpdir / f"{col_name}.jsonl"
            count = export_collection(col_name, col_id, out_file)
            manifest["collections"][col_name] = count
            print(f"  {col_name}: {count} docs")

        # Neo4j graph
        print("Exporting Neo4j graph...")
        neo4j_data = export_neo4j()
        (tmpdir / "neo4j.json").write_text(json.dumps(neo4j_data, default=str, indent=2))
        for key in ("entities", "lessons", "skills", "relations"):
            manifest["neo4j"][key] = len(neo4j_data.get(key, []))
            print(f"  {key}: {manifest['neo4j'][key]}")

        # Canonical markdown
        print("Copying canonical + distilled markdown...")
        md_count = export_canonical(tmpdir)
        manifest["markdown_files"] = md_count
        print(f"  {md_count} markdown files")

        # Manifest
        (tmpdir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Package
        if args.format == "tar":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(args.output, "w:gz") as tar:
                tar.add(tmpdir, arcname="brain-export")
            size_mb = args.output.stat().st_size / 1024 / 1024
            print(f"\nExported to {args.output} ({size_mb:.1f} MB)")
        else:
            shutil.copytree(tmpdir, args.output, dirs_exist_ok=True)
            print(f"\nExported to {args.output}/")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
