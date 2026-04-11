#!/opt/homebrew/bin/python3
"""Weekly skill graph indexer + proposed skill extractor.

1. Indexes existing OpenClaw skills from ~/.openclaw/skills/ into Neo4j as SKILL nodes
2. Analyzes successful /brain/reason traces for composition patterns
3. Proposes new skills to ~/.openclaw/skills/_proposed/ for human review
"""
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKILLS_DIR = Path("/Users/chrischo/.openclaw/skills")
PROPOSED_DIR = SKILLS_DIR / "_proposed"


def parse_skill_frontmatter(skill_file: Path) -> dict | None:
    """Parse SKILL.md frontmatter (YAML header between --- markers)."""
    try:
        content = skill_file.read_text()
    except Exception:
        return None

    if not content.startswith("---"):
        return None

    end = content.find("---", 3)
    if end < 0:
        return None

    header = content[3:end]
    try:
        return yaml.safe_load(header) or {}
    except Exception:
        return None


def index_existing_skills():
    """Walk ~/.openclaw/skills/ and index SKILL.md files into Neo4j."""
    if not SKILLS_DIR.exists():
        print(f"skills dir not found: {SKILLS_DIR}")
        return 0

    try:
        from neo4j_client import run_write
    except Exception as e:
        print(f"neo4j unavailable: {e}")
        return 0

    indexed = 0
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        # Skip proposed skills
        if "_proposed" in str(skill_md):
            continue

        meta = parse_skill_frontmatter(skill_md)
        if not meta:
            continue

        name = meta.get("name", skill_md.parent.name)
        description = meta.get("description", "")

        try:
            run_write(
                "MERGE (s:Skill {name: $name}) "
                "ON CREATE SET s.description = $desc, s.path = $path, "
                "  s.created_at = $now, s.use_count = 0 "
                "ON MATCH SET s.description = $desc, s.path = $path, "
                "  s.updated_at = $now",
                {
                    "name": name,
                    "desc": description[:500],
                    "path": str(skill_md),
                    "now": datetime.now().isoformat(),
                }
            )
            indexed += 1
        except Exception:
            continue

    return indexed


def main():
    print("Indexing OpenClaw skills into Neo4j graph...")
    count = index_existing_skills()
    print(f"Indexed {count} skills")

    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    # Skill extraction from reasoning traces is deferred — placeholder for future
    print("Skill extraction from traces: not yet implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
