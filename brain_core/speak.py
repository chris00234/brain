"""brain_core/speak.py — brain's outbound voice (shim).

Split into speak_schema + speak_drives + speak_synthesis + speak_composer +
speak_urgent on 2026-04-23 to meet the <300-line file bar. This file now
re-exports the public API and hosts the CLI so existing call sites keep
working without changes.

Public API:
  - Observation (dataclass)
  - collect_observations() → list[Observation]
  - compose_digest / format_telegram / run_digest
  - recent_history / ack
  - urgent_scan
  - ensure_schema

CLI:
  python speak.py run [--dry-run] [--bypass-dedup]
  python speak.py urgent_scan
  python speak.py history [--limit N]
  python speak.py ack <entry_id> <useful|noise|ignore>
  python speak.py drives        # inspect current drive outputs
  python speak.py migrate       # just ensure the DDL
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from speak_composer import (
    DRIVES,
    ack,
    collect_observations,
    compose_digest,
    format_telegram,
    recent_history,
    run_digest,
)
from speak_schema import (
    DEDUP_WINDOW_H,
    DIGEST_MAX_BULLETS,
    Observation,
    ensure_schema,
    log_emit,
    now_iso,
    was_sent_recently,
)
from speak_urgent import URGENT_SEVERITY_THRESHOLD, urgent_scan

__all__ = [
    "DEDUP_WINDOW_H",
    "DIGEST_MAX_BULLETS",
    "DRIVES",
    "URGENT_SEVERITY_THRESHOLD",
    "Observation",
    "ack",
    "collect_observations",
    "compose_digest",
    "ensure_schema",
    "format_telegram",
    "log_emit",
    "now_iso",
    "recent_history",
    "run_digest",
    "urgent_scan",
    "was_sent_recently",
]


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--bypass-dedup", action="store_true")

    sub.add_parser("urgent_scan")

    p_hist = sub.add_parser("history")
    p_hist.add_argument("--limit", type=int, default=20)

    p_ack = sub.add_parser("ack")
    p_ack.add_argument("entry_id")
    p_ack.add_argument("verdict", choices=["useful", "noise", "ignore"])

    sub.add_parser("migrate")
    sub.add_parser("drives")

    args = p.parse_args()
    if args.cmd == "run":
        print(
            json.dumps(
                run_digest(dry_run=args.dry_run, bypass_dedup=args.bypass_dedup), indent=2, ensure_ascii=False
            )
        )
    elif args.cmd == "urgent_scan":
        print(json.dumps(urgent_scan(), indent=2, ensure_ascii=False))
    elif args.cmd == "history":
        print(json.dumps(recent_history(limit=args.limit), indent=2, ensure_ascii=False))
    elif args.cmd == "ack":
        print(json.dumps({"ok": ack(args.entry_id, args.verdict)}))
    elif args.cmd == "migrate":
        ensure_schema()
        print("schema ensured")
    elif args.cmd == "drives":
        obs = collect_observations()
        print(
            json.dumps(
                [
                    {
                        "drive": o.drive,
                        "category": o.category,
                        "severity": o.severity,
                        "message": o.message,
                        "dedup_key": o.dedup_key,
                    }
                    for o in obs
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
