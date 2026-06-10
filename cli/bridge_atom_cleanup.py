#!/Users/chrischo/server/brain/.venv/bin/python
"""Quarantine query-keyed bridge atoms (Contract 8 — approval-gated cleanup).

Companion to bridge_atom_inventory.py. Where the inventory only reports,
this tool quarantines: classifier-positive bridge atoms move to
tier='obsolete' — the established auditable expiry tier (conjecture_validator
uses the same one) — never hard delete. Production runs BRAIN_ATOMS_READ=true,
so search_unified drops tier='obsolete' rows from the atoms truth layer at
query time; the ranking-time query_keyed_bridge_penalty stays as defense in
depth for any fallback path that reads Qdrant payload metadata instead.

Safety contract:
  - Default is DRY-RUN: prints the plan, mutates nothing.
  - --apply takes a SQLite online backup to logs/backups/ first, then writes
    a full-row JSON export (the revert source) before any UPDATE.
  - Only rows the governance classifier flags AT APPLY TIME are touched —
    re-checked inside the write transaction, so the plan can't go stale.
  - --revert <export.json> restores prior tier for rows this tool
    quarantined, matched by id + the bridge_cleanup provenance marker.

Usage:
  bridge_atom_cleanup.py                       # dry-run plan
  bridge_atom_cleanup.py --apply               # backup + export + quarantine
  bridge_atom_cleanup.py --revert EXPORT.json  # restore from an apply export
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from recall_governance.source_authority import is_query_keyed_bridge_result  # noqa: E402

DEFAULT_DB = BRAIN_ROOT / "logs" / "brain.db"
BACKUP_DIR = BRAIN_ROOT / "logs" / "backups"
CLEANUP_REASON = "query_keyed_bridge_quarantine"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_bridge(text: str) -> bool:
    return is_query_keyed_bridge_result({"content": text})


def plan_cleanup(db_path: Path | str = DEFAULT_DB) -> list[dict]:
    """Full rows for non-obsolete atoms the governance classifier flags."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM atoms WHERE tier != 'obsolete' ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if _is_bridge(r["text"])]


def backup_db(db_path: Path | str = DEFAULT_DB, backup_dir: Path = BACKUP_DIR) -> Path:
    """SQLite online .backup (lock-free, consistent) before any mutation."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"brain-pre-bridge-cleanup-{stamp}.db"
    src = sqlite3.connect(str(db_path), timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def apply_cleanup(db_path: Path | str = DEFAULT_DB, *, export_path: Path) -> dict:
    """Quarantine classifier-positive bridge atoms; export pre-mutation rows."""
    plan = plan_cleanup(db_path)
    export_path = Path(export_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(
        json.dumps(
            {"exported_at": _now(), "reason": CLEANUP_REASON, "atoms": plan}, ensure_ascii=False, indent=2
        )
    )

    summary: dict = {"quarantined": [], "skipped": [], "export": str(export_path)}
    now_iso = _now()
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        for planned in plan:
            row = conn.execute(
                "SELECT id, text, tier, provenance_json FROM atoms WHERE id = ?", (planned["id"],)
            ).fetchone()
            # Re-check inside the transaction: the plan must not go stale.
            if not row or row["tier"] == "obsolete" or not _is_bridge(row["text"]):
                summary["skipped"].append({"id": planned["id"], "reason": "stale_plan"})
                continue
            try:
                prov = json.loads(row["provenance_json"] or "{}")
            except json.JSONDecodeError:
                prov = {}
            prov["bridge_cleanup"] = {"at": now_iso, "reason": CLEANUP_REASON, "prior_tier": row["tier"]}
            conn.execute(
                "UPDATE atoms SET tier = 'obsolete', provenance_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(prov, ensure_ascii=False), now_iso, row["id"]),
            )
            summary["quarantined"].append(row["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return summary


def revert_cleanup(db_path: Path | str, *, export_path: Path) -> dict:
    """Restore prior tier for rows this tool quarantined (marker-matched)."""
    exported = json.loads(Path(export_path).read_text())["atoms"]
    summary: dict = {"restored": [], "skipped": []}
    now_iso = _now()
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        for old in exported:
            row = conn.execute(
                "SELECT id, tier, provenance_json FROM atoms WHERE id = ?", (old["id"],)
            ).fetchone()
            if not row or row["tier"] != "obsolete":
                summary["skipped"].append({"id": old["id"], "reason": "not_quarantined"})
                continue
            try:
                prov = json.loads(row["provenance_json"] or "{}")
            except json.JSONDecodeError:
                prov = {}
            marker = prov.get("bridge_cleanup") or {}
            if marker.get("reason") != CLEANUP_REASON:
                summary["skipped"].append({"id": old["id"], "reason": "no_cleanup_marker"})
                continue
            prov.pop("bridge_cleanup", None)
            conn.execute(
                "UPDATE atoms SET tier = ?, provenance_json = ?, updated_at = ? WHERE id = ?",
                (
                    marker.get("prior_tier") or old["tier"],
                    json.dumps(prov, ensure_ascii=False),
                    now_iso,
                    row["id"],
                ),
            )
            summary["restored"].append(row["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to brain.db")
    parser.add_argument("--apply", action="store_true", help="Backup, export, then quarantine")
    parser.add_argument("--revert", metavar="EXPORT", help="Restore from an apply export file")
    parser.add_argument("--export", help="Export path for --apply (default: logs/backups/...)")
    args = parser.parse_args()

    if args.revert:
        summary = revert_cleanup(args.db, export_path=Path(args.revert))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not args.apply:
        plan = plan_cleanup(args.db)
        for row in plan:
            print(f"{row['id']}  tier={row['tier']}  {row['text'][:90]!r}")
        print(f"\nDRY-RUN: {len(plan)} atoms would be quarantined (tier→obsolete). No rows modified.")
        print("Run with --apply to backup + export + quarantine.")
        return 0

    backup = backup_db(args.db)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    export_path = Path(args.export) if args.export else BACKUP_DIR / f"bridge-atoms-export-{stamp}.json"
    summary = apply_cleanup(args.db, export_path=export_path)
    summary["backup"] = str(backup)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
