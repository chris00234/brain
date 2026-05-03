#!/usr/bin/env python3
"""Generate a release-readiness snapshot for the current Brain worktree.

This is intentionally non-mutating: it records diff scope, required evidence,
and commit-splitting hints without staging or committing anything.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[1]
REPORT_FILE = BRAIN_ROOT / "logs" / "release_readiness.json"


def _git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=BRAIN_ROOT, text=True, stderr=subprocess.DEVNULL)


def _category(path: str) -> str:
    if path.startswith("tests/"):
        return "tests"
    if path.startswith("brain_core/routes/"):
        return "api-routes"
    if path.startswith("brain_core/"):
        return "brain-core"
    if path.startswith("cli/"):
        return "cli"
    if path.startswith("docs/") or path.endswith(".md"):
        return "docs"
    if path.startswith("launchd/"):
        return "ops"
    if path.startswith("ingest/"):
        return "ingest"
    return "other"


def run() -> dict:
    status = _git(["status", "--porcelain=v1"])
    entries = []
    for line in status.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append({"status": code.strip() or "modified", "path": path, "category": _category(path)})
    counts = Counter(e["category"] for e in entries)
    commit_lanes = [
        {"lane": cat, "files": [e["path"] for e in entries if e["category"] == cat]} for cat in sorted(counts)
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "dirty" if entries else "clean",
        "changed_files": len(entries),
        "category_counts": dict(sorted(counts.items())),
        "commit_lanes": commit_lanes,
        "required_evidence": [
            "uv run ruff check",
            "uv run pytest -q",
            "uv run python cli/backup_restore_drill.py",
            "uv run python cli/retrieval_regression.py --limit 20 --json",
            "npm run build (brain-ui when UI changed)",
        ],
        "lore_commit_template": {
            "Constraint": "Brain entry contract and SLO remediation must remain observable and reversible.",
            "Confidence": "high after listed evidence passes",
            "Scope-risk": "moderate",
            "Directive": "Keep ingestion writes behind QdrantStore and keep dangerous remediation manual-gated.",
        },
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
