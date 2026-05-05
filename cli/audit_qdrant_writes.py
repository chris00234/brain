#!/usr/bin/env python3
"""Audit Qdrant write boundaries.

New data must enter Qdrant through brain_core.qdrant_store.QdrantStore so the
source-aware entry contract (schema/chunk/tag/content_hash/provenance) is
stamped consistently. This audit fails CI when production code introduces a
raw qdrant_client write outside approved maintenance/boundary files.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRITE_METHODS = {
    "upsert",
    "upload_points",
    "upload_collection",
    "set_payload",
    "overwrite_payload",
    "delete_payload",
    "update_vectors",
    "delete_vectors",
    "create_collection",
    "recreate_collection",
    "delete_collection",
    "create_payload_index",
    "delete",
}
ALLOWLIST = {
    "brain_core/qdrant_store.py": "single vector-store boundary; stamps entry contract",
    "brain_core/contextual_embed.py": "maintenance-only contextual vector/payload patch for existing canonical rows",
    "cli/qdrant_bootstrap.py": "schema/bootstrap/probe maintenance",
    "cli/source_aware_shadow_reindex.py": "controlled shadow migration; writes through QdrantStore for points",
    "cli/populate_canonical_contextual.py": "maintenance-only contextual vector backfill",
}
SKIP_DIR_PARTS = {".git", ".venv", "__pycache__", "node_modules", "_archive"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    method: str
    receiver: str


def _rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _iter_py_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        p = path if path.is_absolute() else ROOT / path
        if not p.exists():
            continue
        if p.is_file() and p.suffix == ".py":
            files.append(p)
            continue
        for child in p.rglob("*.py"):
            rel_parts = set(_rel(child).split("/"))
            if rel_parts & SKIP_DIR_PARTS:
                continue
            files.append(child)
    return sorted(set(files))


def _qdrant_client_names(tree: ast.AST) -> set[str]:
    names = {"QdrantClient"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "qdrant_client":
            for alias in node.names:
                if alias.name == "QdrantClient":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "qdrant_client":
                    names.add(alias.asname or "qdrant_client")
    return names


def _attr_chain(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attr_chain(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _attr_chain(node.func)
    return ""


def _is_qdrant_constructor(node: ast.AST, qdrant_names: set[str]) -> bool:
    name = _attr_chain(node)
    return name in qdrant_names or name.endswith(".QdrantClient")


def audit_file(path: Path) -> list[Violation]:
    rel = _rel(path)
    if rel in ALLOWLIST or rel.startswith("tests/"):
        return []
    try:
        tree = ast.parse(path.read_text(errors="ignore"), filename=str(path))
    except SyntaxError:
        return []
    qdrant_names = _qdrant_client_names(tree)
    client_vars: set[str] = set()
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _is_qdrant_constructor(node.value.func, qdrant_names)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    client_vars.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Call)
            and _is_qdrant_constructor(node.value.func, qdrant_names)
            and isinstance(node.target, ast.Name)
        ):
            client_vars.add(node.target.id)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method not in WRITE_METHODS:
            continue
        receiver = _attr_chain(node.func.value)
        if receiver in client_vars:
            violations.append(Violation(rel, node.lineno, method, receiver))
    return violations


def run(paths: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for file in _iter_py_files(paths):
        violations.extend(audit_file(file))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path, default=[Path("brain_core"), Path("cli"), Path("ingest")]
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    violations = run(args.paths)
    if args.json:
        print(json.dumps([asdict(v) for v in violations], indent=2))
    elif violations:
        print("Raw Qdrant writes outside the approved boundary:")
        for v in violations:
            print(f"  {v.path}:{v.line}: {v.receiver}.{v.method}()")
    else:
        print("OK: no unapproved raw Qdrant writes found")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
