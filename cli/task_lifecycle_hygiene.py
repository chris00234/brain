#!/usr/bin/env python3
"""Audit and safely repair Brain task lifecycle hygiene."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = ROOT / "brain_core"
if str(BRAIN_CORE) not in sys.path:
    sys.path.insert(0, str(BRAIN_CORE))

from task_lifecycle_hygiene import (  # noqa: E402
    REPORT_FILE,
    apply_safe_repairs,
    audit_task_lifecycle,
    write_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="Path to autonomy.db")
    parser.add_argument(
        "--apply-safe", action="store_true", help="Apply non-destructive safe repairs before reporting"
    )
    parser.add_argument("--write-report", action="store_true", help=f"Write report to {REPORT_FILE}")
    args = parser.parse_args(argv)

    repair = None
    if args.apply_safe:
        repair = apply_safe_repairs(args.db) if args.db else apply_safe_repairs()
    report = audit_task_lifecycle(args.db) if args.db else audit_task_lifecycle()
    if repair is not None:
        report["safe_repair"] = repair
    if args.write_report:
        report["report_path"] = str(write_report(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") != "missing_db" else 1


if __name__ == "__main__":
    raise SystemExit(main())
