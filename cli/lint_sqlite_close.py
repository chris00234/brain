#!/usr/bin/env python3
"""Catch sqlite3.connect() calls that aren't paired with a safe close pattern.

Safe patterns (any one is enough):
  1. `with sqlite3.connect(...) as conn:`                 — context manager
  2. `conn = sqlite3.connect(...)` + `conn.close()`       — explicit close
  3. `conn = sqlite3.connect(...)` + try/finally closes   — explicit close in finally
  4. `conn = sqlite3.connect(...); return conn`           — factory (caller owns)
  5. `conn = sqlite3.connect(...); yield conn`            — contextmanager helper
  6. `_local.conn = sqlite3.connect(...)` or similar      — thread-local cache (bounded)

Exits 1 if any leak is found. Used as a pre-commit hook.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys


def _find_leaks_in_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, file: str
) -> list[tuple[str, int, str, str]]:
    """Return list of (file, line, func, var) for unclosed connects in this function."""
    conns: dict[str, int] = {}  # var_name -> lineno of connect assignment
    closes_var: set[str] = set()
    returns_var: set[str] = set()
    yields_var: set[str] = set()
    with_vars: set[str] = set()

    for sub in ast.walk(node):
        # `with sqlite3.connect(...) as X:`
        if isinstance(sub, ast.With):
            for item in sub.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and getattr(ce.func, "attr", None) == "connect":
                    if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                        with_vars.add(item.optional_vars.id)
                    else:
                        # `with sqlite3.connect(...):` with no var still closes on exit
                        with_vars.add("__anon__")

        # Assignments — only track plain `conn = sqlite3.connect(...)`.
        # `_local.conn = sqlite3.connect(...)` (attribute target) is a
        # thread-local cache and is always safe by design.
        if (
            isinstance(sub, ast.Assign)
            and isinstance(sub.value, ast.Call)
            and getattr(sub.value.func, "attr", None) == "connect"
        ):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    conns[t.id] = sub.lineno

        # `.close()` calls
        if isinstance(sub, ast.Call) and getattr(sub.func, "attr", None) == "close":
            val = getattr(sub.func, "value", None)
            if isinstance(val, ast.Name):
                closes_var.add(val.id)
            elif isinstance(val, ast.Attribute):
                closes_var.add("__attr__")  # self.conn.close(), cached.close()

        # return / yield conn
        if isinstance(sub, ast.Return | ast.Yield):
            v = getattr(sub, "value", None)
            if isinstance(v, ast.Name):
                if isinstance(sub, ast.Return):
                    returns_var.add(v.id)
                else:
                    yields_var.add(v.id)

    leaks = []
    for var, lineno in conns.items():
        if var in closes_var:
            continue
        if var in returns_var:
            continue  # factory pattern
        if var in yields_var:
            continue  # contextmanager helper
        if var in with_vars:
            continue
        leaks.append((file, lineno, node.name, var))
    return leaks


def check_file(path: pathlib.Path) -> list[tuple[str, int, str, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as e:
        print(f"SYNTAX ERROR {path}: {e}", file=sys.stderr)
        return []
    leaks: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            leaks.extend(_find_leaks_in_function(node, str(path)))

    # Module-level `conn = sqlite3.connect(...)` is always suspicious
    # (persistent for process lifetime with no shutdown hook).
    # Exception: `_local.conn = ...` thread-local caches are OK.
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "attr", None) == "connect"
        ):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    leaks.append(
                        (str(path), stmt.lineno, "<module>", f"{t.id} (module-level, no shutdown hook)")
                    )
    return leaks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Files to check (default: scan brain_core/)")
    args = parser.parse_args()

    if args.paths:
        files = [pathlib.Path(p) for p in args.paths if p.endswith(".py")]
    else:
        root = pathlib.Path(__file__).resolve().parent.parent / "brain_core"
        files = [p for p in root.rglob("*.py") if "__pycache__" not in str(p)]

    all_leaks: list[tuple[str, int, str, str]] = []
    for f in files:
        if not f.exists():
            continue
        all_leaks.extend(check_file(f))

    if all_leaks:
        print(f"Found {len(all_leaks)} potential sqlite3 connection leak(s):", file=sys.stderr)
        for file, line, func, var in all_leaks:
            print(f"  {file}:{line}  in {func}()  var={var}", file=sys.stderr)
        print(
            "\nAdd `conn.close()` in a try/finally, use `with sqlite3.connect(...)`, "
            "or return/yield the connection from a factory.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
