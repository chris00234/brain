#!/Users/chrischo/server/brain/.venv/bin/python3
"""Code intelligence indexer.

For every Python/TypeScript/JS function in Chris's tracked repos, store one
ChromaDB doc in a `code` collection so semantic search over code becomes
possible. Re-runs are incremental — only files whose mtime exceeds the
recorded watermark get re-indexed.

Run via:
  /Users/chrischo/server/brain/.venv/bin/python3 ingest/code_repos.py
or via the scheduler `code_index_refresh` job.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from http_pool import http_json
from indexer import ensure_collection, get_embeddings_batch

CODE_COLLECTION = "code"
STATE_FILE = Path("/Users/chrischo/server/brain/logs/code-index-state.json")
CHROMA_API = "http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"

# Same repo list as ingest/git_activity.py — keep in sync.
REPOS: list[Path] = [
    Path.home() / "server/brain",
    Path.home() / "server/brain-ui",
    Path.home() / "server/chrischodev",
    Path.home() / "server/claw3d",
    Path.home() / "server/knowledge",
    Path.home() / "LibreUIUX-Claude-Code",
    Path.home() / "jenna_teacher",
    Path.home() / "oc-lifehub",
    Path.home() / "ui-ux-pro-max-skill",
]

# Skip dirs that have no value or break parsing.
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".cache",
    "coverage",
    "logs",
    "chroma-data",
    "ollama-data",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# Source extensions we know how to parse.
EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

# Heuristic JS/TS function patterns. Cheap, no tree-sitter dep.
JS_FUNC_PATTERNS = [
    re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)", re.MULTILINE),
    re.compile(
        r"^(?:export\s+)?const\s+(\w+)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?\([^)]*\)\s*(?::\s*[^=]+)?=>\s*\{",
        re.MULTILINE,
    ),
    re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE),
]


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"file_mtimes": {}, "last_run_at": ""}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"file_mtimes": {}, "last_run_at": ""}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run_at"] = datetime.now(UTC).isoformat()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def walk_repo(repo: Path):
    """Yield all source files in a repo, skipping noise."""
    if not repo.exists() or not repo.is_dir():
        return
    for path in repo.rglob("*"):
        # Skip everything inside excluded directories
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix not in EXTENSIONS:
            continue
        # Skip files >500KB — almost certainly generated/minified.
        try:
            if path.stat().st_size > 500_000:
                continue
        except Exception:
            continue
        yield path


def parse_python(path: Path, source: str) -> list[dict]:
    """Extract function-level chunks from a Python file via stdlib ast."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
        )
        name = node.name
        # Build a signature string. ast.unparse exists in Py 3.9+.
        try:
            if isinstance(node, ast.ClassDef):
                signature = f"class {name}"
            else:
                args = ast.unparse(node.args) if hasattr(ast, "unparse") else "..."
                signature = f"{'async ' if kind == 'async_function' else ''}def {name}({args})"
        except Exception:
            signature = f"def {name}(...)"
        docstring = ast.get_docstring(node) or ""
        # Extract source segment if available
        body_text = ""
        try:
            body_text = ast.get_source_segment(source, node) or ""
        except Exception:
            body_text = ""
        if not body_text:
            continue
        out.append(
            {
                "kind": kind,
                "name": name,
                "signature": signature,
                "docstring": docstring[:500],
                "body": body_text[:1500],
                "line_start": node.lineno,
            }
        )
    return out


def parse_js_ts(path: Path, source: str) -> list[dict]:
    """Heuristic JS/TS function extractor — no tree-sitter dependency."""
    out: list[dict] = []
    seen_names: set[str] = set()
    lines = source.splitlines()
    for pat in JS_FUNC_PATTERNS:
        for m in pat.finditer(source):
            name = m.group(1)
            if name in seen_names:
                continue
            seen_names.add(name)
            # Approximate line number from match offset
            line_no = source[: m.start()].count("\n") + 1
            kind = "class" if pat is JS_FUNC_PATTERNS[2] else "function"
            # Grab ~30 lines around the match for body
            start = max(0, line_no - 1)
            end = min(len(lines), line_no + 30)
            body_text = "\n".join(lines[start:end])
            out.append(
                {
                    "kind": kind,
                    "name": name,
                    "signature": m.group(0).strip()[:300],
                    "docstring": "",
                    "body": body_text[:1500],
                    "line_start": line_no,
                }
            )
    return out


def doc_id_for(path: Path, fn_name: str, line: int) -> str:
    """Stable doc id so re-indexing the same function overwrites the prior row."""
    h = hashlib.md5(f"{path}:{fn_name}:{line}".encode()).hexdigest()[:16]
    return f"code_{h}"


def build_chunk_text(fn: dict, path: Path) -> str:
    """The text we embed. E5 models like a query/passage prefix; this gets
    a passage prefix at embed time via indexer.get_embeddings_batch."""
    parts = [
        f"file: {path.name}",
        f"path: {path}",
        f"name: {fn['name']}",
        f"signature: {fn['signature']}",
    ]
    if fn["docstring"]:
        parts.append(f"docstring: {fn['docstring']}")
    parts.append(f"body: {fn['body'][:800]}")
    return "\n".join(parts)


def index_repo(repo: Path, state: dict, col_id: str) -> dict:
    """Walk the repo, parse changed files, embed + upsert chunks."""
    file_mtimes = state.get("file_mtimes", {})
    pending_docs: list[dict] = []
    pending_ids: list[str] = []
    pending_texts: list[str] = []
    pending_metas: list[dict] = []
    indexed_files = 0
    skipped_files = 0
    extracted_fns = 0

    for path in walk_repo(repo):
        path_str = str(path)
        try:
            mtime = path.stat().st_mtime
        except Exception:
            continue
        prev_mtime = file_mtimes.get(path_str, 0)
        if mtime <= prev_mtime:
            skipped_files += 1
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if path.suffix == ".py":
            functions = parse_python(path, source)
        else:
            functions = parse_js_ts(path, source)
        if not functions:
            file_mtimes[path_str] = mtime
            continue
        indexed_files += 1
        extracted_fns += len(functions)
        for fn in functions:
            doc_text = build_chunk_text(fn, path)
            doc_id = doc_id_for(path, fn["name"], fn["line_start"])
            meta = {
                "file_path": path_str,
                "file_name": path.name,
                "function_name": fn["name"],
                "kind": fn["kind"],
                "signature": fn["signature"][:300],
                "line_start": fn["line_start"],
                "language": "python"
                if path.suffix == ".py"
                else "typescript"
                if path.suffix in (".ts", ".tsx")
                else "javascript",
                "repo": repo.name,
                "indexed_at": datetime.now(UTC).isoformat(),
            }
            pending_ids.append(doc_id)
            pending_texts.append(doc_text)
            pending_metas.append(meta)
            pending_docs.append(doc_text)
        file_mtimes[path_str] = mtime
        # Flush in batches of 50 to keep memory bounded.
        if len(pending_ids) >= 50:
            _flush_batch(col_id, pending_ids, pending_texts, pending_metas)
            pending_ids.clear()
            pending_texts.clear()
            pending_metas.clear()
            pending_docs.clear()

    if pending_ids:
        _flush_batch(col_id, pending_ids, pending_texts, pending_metas)

    state["file_mtimes"] = file_mtimes
    return {
        "repo": str(repo),
        "indexed_files": indexed_files,
        "skipped_unchanged": skipped_files,
        "extracted_functions": extracted_fns,
    }


def _flush_batch(col_id: str, ids: list, texts: list, metas: list) -> None:
    """Embed + upsert one batch."""
    if not ids:
        return
    try:
        embs = get_embeddings_batch(texts, prefix="passage", batch_size=50)
    except Exception as e:
        print(f"  embed batch failed: {e}", flush=True)
        return
    valid = [(i, t, m, e) for i, t, m, e in zip(ids, texts, metas, embs, strict=False) if e]
    if not valid:
        return
    v_ids, v_texts, v_metas, v_embs = zip(*valid, strict=False)
    try:
        http_json(
            "POST",
            f"{CHROMA_API}/{col_id}/upsert",
            {
                "ids": list(v_ids),
                "embeddings": list(v_embs),
                "documents": list(v_texts),
                "metadatas": list(v_metas),
            },
        )
    except Exception as e:
        print(f"  upsert failed: {e}", flush=True)


def main() -> int:
    print(f"[code_index] starting at {datetime.now(UTC).isoformat()}", flush=True)
    state = load_state()
    print(f"[code_index] state: {len(state.get('file_mtimes', {}))} files tracked", flush=True)

    col_id = ensure_collection(CODE_COLLECTION)
    if not col_id:
        print("[code_index] FATAL: could not get/create code collection", file=sys.stderr)
        return 2

    t_start = time.time()
    results: list[dict] = []
    for repo in REPOS:
        if not repo.exists():
            continue
        print(f"[code_index] {repo}", flush=True)
        try:
            r = index_repo(repo, state, col_id)
            results.append(r)
            print(
                f"  indexed_files={r['indexed_files']} extracted_functions={r['extracted_functions']} skipped={r['skipped_unchanged']}",
                flush=True,
            )
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr, flush=True)

    save_state(state)
    duration = time.time() - t_start
    total_funcs = sum(r["extracted_functions"] for r in results)
    total_files = sum(r["indexed_files"] for r in results)
    print(f"[code_index] done in {duration:.1f}s — {total_files} files, {total_funcs} functions", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
