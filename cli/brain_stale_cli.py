#!/usr/bin/env python3
"""brain_stale_cli.py — manual review + cleanup of stale atoms.

Mirrors brain_loop._sense_stale_atoms() but in CLI form so Chris can walk
through candidates outside the autonomy gate.

Commands:
  audit [--kind preference|fact|decision|any] [--min-age-days N] [--limit N]
  obsolete <atom_id>               # mark a specific atom as tier='obsolete'
  bulk-obsolete [--kind ...] [--dry] # mark everything audit would flag

Uses brain.db::atoms directly. Does NOT go through /memory endpoint because
the supersession classifier would try to interpret this as new content.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

DECAY_DAYS_BY_KIND = {
    "preference": 90,
    "fact": 180,
    "decision": 365,
    "entity": 180,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _iter_candidates(kind_filter: str, min_age_days: int | None):
    with sqlite3.connect(str(BRAIN_DB), timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, text, kind, tier, created_at, last_reviewed_at, "
            "       reinforcement_count, confidence, provenance_json "
            "FROM atoms "
            "WHERE tier IN ('semantic','episodic') "
            "  AND (superseded_by IS NULL OR superseded_by = '') "
            "ORDER BY COALESCE(last_reviewed_at, created_at) ASC"
        ).fetchall()
    now = _now()
    for r in rows:
        atom_kind = (r["kind"] or "fact").lower()
        if kind_filter != "any" and atom_kind != kind_filter:
            continue
        threshold = DECAY_DAYS_BY_KIND.get(atom_kind, 180)
        anchor = _parse_iso(r["last_reviewed_at"] or r["created_at"] or "")
        if not anchor:
            continue
        age_days = (now - anchor).total_seconds() / 86400
        min_cutoff = min_age_days if min_age_days is not None else threshold
        if age_days < min_cutoff:
            continue
        reinf = r["reinforcement_count"] or 0
        if reinf >= 2:
            continue
        yield {
            "id": r["id"],
            "kind": atom_kind,
            "tier": r["tier"],
            "age_days": round(age_days, 1),
            "decay_days": threshold,
            "reinf": reinf,
            "text": r["text"] or "",
            "confidence": r["confidence"] or 0,
        }


def cmd_audit(args: argparse.Namespace) -> int:
    candidates = list(_iter_candidates(args.kind, args.min_age_days))
    candidates.sort(key=lambda c: c["age_days"], reverse=True)
    if not candidates:
        print("No stale candidates")
        return 0
    print(f"{len(candidates)} stale candidate(s) (kind={args.kind})")
    print("=" * 80)
    for c in candidates[: args.limit]:
        preview = c["text"][:90].replace("\n", " ")
        print(f"[{c['age_days']:6.1f}d / {c['decay_days']}d] {c['kind']:10s} {c['id']}")
        print(f"    tier={c['tier']} reinf={c['reinf']} conf={c['confidence']:.2f}")
        print(f"    {preview}")
    if len(candidates) > args.limit:
        print(f"... +{len(candidates) - args.limit} more")
    return 0


def cmd_obsolete(args: argparse.Namespace) -> int:
    with sqlite3.connect(str(BRAIN_DB), timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT id, tier, text FROM atoms WHERE id = ?", (args.atom_id,)).fetchone()
        if not row:
            print(f"atom {args.atom_id} not found", file=sys.stderr)
            return 1
        conn.execute(
            "UPDATE atoms SET tier='obsolete', updated_at=? WHERE id = ?",
            (_now().isoformat(timespec="seconds"), args.atom_id),
        )
        conn.commit()
    print(f"marked {args.atom_id} as obsolete")
    print(f"  was tier={row['tier']} text={(row['text'] or '')[:120]}")
    return 0


def cmd_bulk_obsolete(args: argparse.Namespace) -> int:
    candidates = list(_iter_candidates(args.kind, args.min_age_days))
    if not candidates:
        print("Nothing to obsolete")
        return 0
    print(f"Would obsolete {len(candidates)} atom(s) (kind={args.kind})")
    if args.dry:
        for c in candidates[:10]:
            preview = c["text"][:90].replace("\n", " ")
            print(f"  [{c['age_days']:6.1f}d] {c['id']} {c['kind']:10s} {preview}")
        if len(candidates) > 10:
            print(f"  ... +{len(candidates) - 10} more")
        print("\nDry run. Re-run without --dry to commit.")
        return 0
    # Apply
    with sqlite3.connect(str(BRAIN_DB), timeout=5) as conn:
        now = _now().isoformat(timespec="seconds")
        ids = [c["id"] for c in candidates]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE atoms SET tier='obsolete', updated_at=? WHERE id IN ({placeholders})",
            [now] + ids,
        )
        conn.commit()
    print(f"Marked {len(candidates)} atom(s) as obsolete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stale atom audit + cleanup CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="List stale atom candidates")
    p_audit.add_argument("--kind", default="any", choices=["any", "preference", "fact", "decision", "entity"])
    p_audit.add_argument("--min-age-days", type=int, default=None, help="Override per-kind decay threshold")
    p_audit.add_argument("--limit", type=int, default=30)
    p_audit.set_defaults(func=cmd_audit)

    p_obs = sub.add_parser("obsolete", help="Mark a specific atom obsolete")
    p_obs.add_argument("atom_id")
    p_obs.set_defaults(func=cmd_obsolete)

    p_bulk = sub.add_parser("bulk-obsolete", help="Mark all audit candidates obsolete")
    p_bulk.add_argument("--kind", default="any", choices=["any", "preference", "fact", "decision", "entity"])
    p_bulk.add_argument("--min-age-days", type=int, default=None)
    p_bulk.add_argument("--dry", action="store_true", help="Preview only")
    p_bulk.set_defaults(func=cmd_bulk_obsolete)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
