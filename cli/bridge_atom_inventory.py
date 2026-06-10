#!/Users/chrischo/server/brain/.venv/bin/python
"""Read-only inventory of query-keyed bridge atoms (Contract 7).

Lists atoms whose text leads with query-keyed bridge framing ("For the exact
query X: ...", "Knowledge-gap bridge for query Y: ...") — data-level retrieval
hacks that recall governance demotes via query_keyed_bridge_penalty. The
report reuses recall_governance.is_query_keyed_bridge_result as the single
source of truth, so it can never drift from what governance penalizes.

READ-ONLY by contract: opens the DB with mode=ro and never mutates rows.
Deleting/superseding the flagged atoms is a separate, approval-gated cleanup.

Usage:
  bridge_atom_inventory.py            # human-readable list
  bridge_atom_inventory.py --json     # JSON report for dashboards/review tasks
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from recall_governance.source_authority import is_query_keyed_bridge_result  # noqa: E402

DEFAULT_DB = BRAIN_ROOT / "logs" / "brain.db"


def inventory_bridge_atoms(db_path: Path | str = DEFAULT_DB) -> list[dict]:
    """Return non-obsolete atoms flagged by the governance bridge classifier."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, text, kind, tier, canonical, confidence, created_at "
            "FROM atoms WHERE tier != 'obsolete' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows if is_query_keyed_bridge_result({"content": row["text"]})]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to brain.db (opened read-only)")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    flagged = inventory_bridge_atoms(args.db)
    if args.json:
        print(json.dumps({"count": len(flagged), "atoms": flagged}, ensure_ascii=False, indent=2))
        return 0
    for row in flagged:
        print(f"{row['id']}  tier={row['tier']}  conf={row['confidence']:.2f}  {row['text'][:100]!r}")
    print(f"\n{len(flagged)} query-keyed bridge atoms (read-only report; no rows were modified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
