"""brain_core/canonical_staleness.py — detect + retire stale canonical claims.

Problem (directly observed 2026-04-23): brain surfaced a "search.py is missing
argparse import" claim with score 10/10 across 10+ canonical chunks. The bug
was fixed weeks ago — `import argparse` is on line 21 of search.py. The
canonical pipeline had no mechanism to notice reality moved on, so every
retrieval pushed a false positive. This module is the mechanism.

Strategy (narrow but directly useful):
  1. Scan distilled canonical .md files for claims that match one of a small
     set of testable patterns (MISSING_IMPORT_CLAIM, NAMEERROR_CLAIM, etc).
  2. For each extracted claim, read the referenced file and check if the
     claim still holds.
  3. If the claim is invalidated → move the distilled file into
     knowledge/obsolete/, stamp an OBSOLETE header with evidence, and delete
     every canonical atom in Qdrant whose `path` points at the old file.
  4. Also scan active canonical notes for explicit current-truth supersession
     drift (for example ChromaDB claims after the Qdrant cutover).
  5. Log every decision to logs/canonical_staleness.jsonl for audit.

Runs daily at 04:30am as a scheduled job; also callable via
POST /brain/canonical_staleness/run (see server.py routes).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_LOGS_DIR, KNOWLEDGE_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")

DISTILLED_DIR = KNOWLEDGE_DIR / "distilled"
OBSOLETE_DIR = KNOWLEDGE_DIR / "obsolete"
AUDIT_LOG = BRAIN_LOGS_DIR / "canonical_staleness.jsonl"

log = logging.getLogger("brain.canonical_staleness")

try:
    from stale_current_truth import build_atoms_report as build_stale_atoms_truth_report
    from stale_current_truth import build_report as build_stale_current_truth_report
    from stale_current_truth import build_vector_report as build_stale_vector_truth_report
except ImportError:  # pragma: no cover - keeps old deployments import-safe
    build_stale_atoms_truth_report = None
    build_stale_current_truth_report = None
    build_stale_vector_truth_report = None


# ── Claim extractors ────────────────────────────────────────────────────
# Each extractor returns (claim_type, target_path, target_symbol) or None.
# target_path is the absolute file path the atom references.
# target_symbol is the module/name the claim says is missing (when applicable).


# Path alternatives we accept in distilled prose:
#   /Users/chrischo/server/brain/<...>.py  (absolute, canonical)
#   brain_core/<...>.py / ingest/<...>.py / cli/<...>.py  (repo-relative)
#   <name>.py — accepted with a limited set of well-known file stems so we
#   don't match every "foo.py" mention.
_BRAIN_ROOT_REL = r"(?:brain_core|ingest|cli|pipeline|tests|server\.py|brain_mcp_server\.py)"
_PATH_ALT = (
    r"(?:"
    r"/Users/chrischo/server/brain/[^\s`'\"]+\.py"
    r"|" + _BRAIN_ROOT_REL + r"/[a-zA-Z0-9_./\-]+\.py"
    r"|[a-zA-Z_][a-zA-Z0-9_\-]*\.py"
    r")"
)


def _normalize_path(raw: str) -> str | None:
    """Resolve a match candidate to an absolute path that exists on disk."""
    from pathlib import Path as _P

    brain_root = _P("/Users/chrischo/server/brain")
    candidates = []
    if raw.startswith("/"):
        candidates.append(_P(raw))
    elif raw.startswith(("brain_core/", "ingest/", "cli/", "pipeline/", "tests/")) or raw in (
        "server.py",
        "brain_mcp_server.py",
    ):
        candidates.append(brain_root / raw)
    else:
        # Bare basename — look in brain_core/ first, then repo root.
        for sub in ("brain_core", "ingest", "cli", "pipeline", ""):
            c = (brain_root / sub / raw) if sub else (brain_root / raw)
            if c.exists():
                candidates.append(c)
                break
    for c in candidates:
        if c.exists() and c.is_file() and c.suffix == ".py":
            return str(c)
    return None


_CLAIM_MISSING_IMPORT = re.compile(
    r"(?P<path>" + _PATH_ALT + r")"
    r"\b[^\n]{0,140}?"
    r"(?:missing|without\s+importing|not\s+imported|is\s+missing)\s+(?:an\s+|a\s+)?"
    r"[`']?(?P<sym>[a-zA-Z_][a-zA-Z0-9_]*)[`']?\s+"
    r"(?:import|module)",
    re.IGNORECASE,
)

_CLAIM_NAMEERROR = re.compile(
    r"(?P<path>" + _PATH_ALT + r")" r"\b[^\n]{0,240}?" r"NameError:?\s+[`']?(?P<sym>[a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)

_CLAIM_CALLS_WITHOUT_IMPORT = re.compile(
    r"(?P<path>" + _PATH_ALT + r")"
    r"\b[^\n]{0,220}?"
    r"(?:calls?|uses|invokes|references)\s+[`']?(?P<sym>[a-zA-Z_][a-zA-Z0-9_.]*)[`']?"
    r"[^\n]{0,140}?without\s+importing",
    re.IGNORECASE,
)


def _extract_claims(text: str) -> list[tuple[str, str, str]]:
    """Return list of (claim_type, abs_path, symbol). Skips claims whose
    path reference cannot be resolved to an on-disk file."""
    out: list[tuple[str, str, str]] = []
    for claim_type, rx in (
        ("calls_without_import", _CLAIM_CALLS_WITHOUT_IMPORT),
        ("missing_import", _CLAIM_MISSING_IMPORT),
        ("nameerror", _CLAIM_NAMEERROR),
    ):
        for m in rx.finditer(text):
            raw = m.group("path")
            abs_path = _normalize_path(raw)
            if not abs_path:
                continue
            sym = m.group("sym").split(".")[0]
            out.append((claim_type, abs_path, sym))
    return list({(c, p, s) for c, p, s in out})


# ── Verifiers ───────────────────────────────────────────────────────────
# Given a claim, return True if the claim is still valid (file really is
# missing the import) OR None if we can't tell.


_IMPORT_RX_CACHE: dict[tuple[str, str, str], bool] = {}


def _file_imports_symbol(path: str, symbol: str) -> bool | None:
    """Check if `path` imports `symbol` at the module level.

    Returns True/False if we can read the file; None if read failed.
    Cached per (path, symbol, mtime) so a staleness run over many atoms
    pointing at the same file is cheap.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    cache_key = (path, symbol, str(stat.st_mtime_ns))
    hit = _IMPORT_RX_CACHE.get(cache_key)
    if hit is not None:
        return hit
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    # Match `import X`, `import X as Y`, `from X import ...`
    import_rx = re.compile(
        rf"^\s*(?:import\s+{re.escape(symbol)}(?:\s+as\s+\w+)?\s*$"
        rf"|from\s+{re.escape(symbol)}(?:\.[a-zA-Z_][a-zA-Z0-9_.]*)?\s+import\s+)",
        re.MULTILINE,
    )
    present = import_rx.search(content) is not None
    _IMPORT_RX_CACHE[cache_key] = present
    return present


# ── File ops ────────────────────────────────────────────────────────────


def _stamp_obsolete(src_path: Path, reason: str) -> Path:
    """Move src_path into OBSOLETE_DIR with an OBSOLETE header prepended."""
    OBSOLETE_DIR.mkdir(parents=True, exist_ok=True)
    subdir = src_path.parent.name or "misc"
    dest_parent = OBSOLETE_DIR / subdir
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / src_path.name
    # If dest already exists from a prior run, append timestamp
    if dest.exists():
        dest = dest_parent / f"{dest.stem}.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.md"
    original = src_path.read_text(encoding="utf-8", errors="replace")
    header = (
        f"---\n"
        f"OBSOLETE: true\n"
        f"obsoleted_at: {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        f"obsolete_reason: {reason}\n"
        f"original_path: {src_path}\n"
        f"---\n\n"
    )
    dest.write_text(header + original, encoding="utf-8")
    src_path.unlink()
    return dest


def _delete_qdrant_atoms_for_path(orig_path: Path) -> int:
    """Best-effort: delete canonical atoms whose payload.source equals the
    retired file. Qdrant payload uses `source` (absolute distilled path),
    not `path`. Walk the collection and remove matching atoms.
    """
    try:
        from vector_store import get_vector_store
    except ImportError:
        return 0
    try:
        store = get_vector_store()
        points = store.get("canonical", limit=20000, with_payload=True, with_documents=False)
    except Exception as exc:
        log.debug("qdrant scan failed: %s", exc)
        return 0
    target = str(orig_path)
    to_delete: list[str] = []
    for p in points:
        payload = p.payload or {}
        # New convention: payload.source. Older ingest may have used .path.
        if str(payload.get("source", "")) == target or str(payload.get("path", "")) == target:
            to_delete.append(p.id)
    if not to_delete:
        return 0
    try:
        store.delete("canonical", ids=to_delete)
        return len(to_delete)
    except Exception as exc:
        log.debug("qdrant delete failed: %s", exc)
        return 0


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("audit write failed: %s", exc)


# ── Main scan ───────────────────────────────────────────────────────────


def scan_distilled(*, dry_run: bool = False, max_files: int = 20000) -> dict:
    """Walk every distilled .md file, extract testable claims, verify, and
    retire the stale ones. Returns a summary dict."""
    scanned = 0
    claim_total = 0
    retired = 0
    retired_files: list[str] = []

    if not DISTILLED_DIR.exists():
        return {"status": "error", "reason": f"{DISTILLED_DIR} does not exist"}

    for md_path in sorted(DISTILLED_DIR.rglob("*.md")):
        if scanned >= max_files:
            break
        scanned += 1
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Skip files already marked obsolete
        if text.lstrip().startswith("---") and "OBSOLETE: true" in text[:400]:
            continue
        claims = _extract_claims(text)
        if not claims:
            continue
        claim_total += len(claims)
        invalidated_claims: list[dict] = []
        for claim_type, target_path, symbol in claims:
            if claim_type in ("missing_import", "calls_without_import", "nameerror"):
                imports_it = _file_imports_symbol(target_path, symbol)
                if imports_it is True:
                    invalidated_claims.append(
                        {
                            "claim_type": claim_type,
                            "target_path": target_path,
                            "symbol": symbol,
                            "why_invalid": f"file imports `{symbol}` at module level",
                        }
                    )
        if not invalidated_claims:
            continue
        reason = "; ".join(
            f"{c['claim_type']} on {Path(c['target_path']).name} " f"(`{c['symbol']}`): {c['why_invalid']}"
            for c in invalidated_claims
        )[:400]
        audit_entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "distilled_file": str(md_path),
            "invalidated_claims": invalidated_claims,
            "action": "dry_run" if dry_run else "retired",
            "reason": reason,
        }
        if dry_run:
            _audit(audit_entry)
            retired += 1
            retired_files.append(str(md_path))
            continue
        try:
            new_path = _stamp_obsolete(md_path, reason)
            audit_entry["moved_to"] = str(new_path)
        except OSError as exc:
            log.warning("obsolete move failed for %s: %s", md_path, exc)
            audit_entry["error"] = str(exc)
            _audit(audit_entry)
            continue
        qdrant_deleted = _delete_qdrant_atoms_for_path(md_path)
        audit_entry["qdrant_atoms_deleted"] = qdrant_deleted
        _audit(audit_entry)
        retired += 1
        retired_files.append(str(md_path))

    return {
        "status": "ok",
        "dry_run": dry_run,
        "files_scanned": scanned,
        "claims_extracted": claim_total,
        "files_retired": retired,
        "retired_files": retired_files[:20],  # cap output
    }


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="scan + log without moving files")
    p.add_argument("--max-files", type=int, default=2000)
    p.add_argument("--current-truth", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--vector-current-truth", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--atoms-current-truth", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--fail-on-current-truth-blockers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="exit non-zero when active canonical notes contain stale current-truth claims",
    )
    args = p.parse_args()
    result = scan_distilled(dry_run=args.dry_run, max_files=args.max_files)
    if args.current_truth and build_stale_current_truth_report is not None:
        current_truth = build_stale_current_truth_report(KNOWLEDGE_DIR)
        result["current_truth"] = current_truth
        if current_truth.get("blockers"):
            result["status"] = "blocked"
            result["reason"] = "stale current-truth blockers found"
            _audit(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "current_truth_blocked",
                    "blocker_count": current_truth.get("blocker_count"),
                    "blockers": current_truth.get("blockers", [])[:20],
                }
            )
    if args.vector_current_truth and build_stale_vector_truth_report is not None:
        vector_truth = build_stale_vector_truth_report(apply=not args.dry_run)
        result["vector_current_truth"] = vector_truth
        if vector_truth.get("blockers"):
            _audit(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "vector_current_truth_marked"
                    if not args.dry_run
                    else "vector_current_truth_dry_run",
                    "blocker_count": vector_truth.get("blocker_count"),
                    "marked": vector_truth.get("marked"),
                    "blockers": vector_truth.get("blockers", [])[:20],
                }
            )
    if args.atoms_current_truth and build_stale_atoms_truth_report is not None:
        atoms_truth = build_stale_atoms_truth_report(apply=not args.dry_run)
        result["atoms_current_truth"] = atoms_truth
        if atoms_truth.get("blockers"):
            _audit(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "action": "atoms_current_truth_marked"
                    if not args.dry_run
                    else "atoms_current_truth_dry_run",
                    "blocker_count": atoms_truth.get("blocker_count"),
                    "marked_atoms": atoms_truth.get("marked_atoms"),
                    "marked_vectors": atoms_truth.get("marked_vectors"),
                    "blockers": atoms_truth.get("blockers", [])[:20],
                }
            )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if (
        args.current_truth
        and args.fail_on_current_truth_blockers
        and isinstance(result.get("current_truth"), dict)
        and result["current_truth"].get("blockers")
    ):
        raise SystemExit(1)
