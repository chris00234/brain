#!/usr/bin/env python3
"""Audit OpenClaw Telegram delivery targets.

Brain SLO/critical alerts should use direct Telegram Bot API. OpenClaw cron
jobs that still deliver via Telegram must use Chris's numeric chat id, not
aliases such as `Chris` or `@chris`, because alias resolution has failed in
production.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

OPENCLAW_DIR = Path.home() / ".openclaw"
DEFAULT_CRON_JOBS = OPENCLAW_DIR / "cron" / "jobs.json"
DEFAULT_CONFIG = OPENCLAW_DIR / "openclaw.json"
CHRIS_TELEGRAM_ID = "8484060831"
ALIAS_RE = re.compile(r"^@?chris(?:\s+cho)?$", re.I)
PROMPT_ALIAS_RE = re.compile(r"(chris|chris cho|크리스|Chris한테|텔레그램으로 보내줘)", re.I)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def audit_cron_jobs(path: Path = DEFAULT_CRON_JOBS) -> list[dict]:
    if not path.exists():
        return []
    data = _load_json(path)
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    issues: list[dict] = []
    for job in jobs:
        delivery = job.get("delivery") or {}
        if str(delivery.get("channel") or "").lower() != "telegram":
            continue
        target = str(delivery.get("to") or "")
        if target != CHRIS_TELEGRAM_ID:
            severity = "alias" if ALIAS_RE.match(target) else "unexpected_target"
            issues.append(
                {
                    "file": str(path),
                    "job_id": job.get("id"),
                    "job_name": job.get("name"),
                    "target": target,
                    "issue": severity,
                    "expected": CHRIS_TELEGRAM_ID,
                }
            )
        promptish = "\n".join(
            str(job.get(k) or "") for k in ("prompt", "message", "instruction", "description", "name")
        )
        if promptish and CHRIS_TELEGRAM_ID not in promptish and PROMPT_ALIAS_RE.search(promptish):
            issues.append(
                {
                    "file": str(path),
                    "job_id": job.get("id"),
                    "job_name": job.get("name"),
                    "issue": "prompt_alias_without_numeric_target",
                    "expected": CHRIS_TELEGRAM_ID,
                }
            )
    return issues


def audit_openclaw_config(path: Path = DEFAULT_CONFIG) -> list[dict]:
    if not path.exists():
        return []
    cfg = _load_json(path)
    channels = cfg.get("channels") or {}
    telegram = channels.get("telegram") or {}
    allowed = {str(v) for v in telegram.get("groupAllowFrom") or []}
    issues: list[dict] = []
    if telegram.get("enabled") and CHRIS_TELEGRAM_ID not in allowed:
        issues.append(
            {
                "file": str(path),
                "issue": "missing_numeric_group_allow_from",
                "expected": CHRIS_TELEGRAM_ID,
            }
        )
    return issues


def run() -> dict:
    issues = audit_cron_jobs() + audit_openclaw_config()
    return {"ok": not issues, "issues": issues, "issue_count": len(issues)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result["ok"]:
        print("OpenClaw Telegram target audit PASSED")
    else:
        print("OpenClaw Telegram target audit FAILED")
        for issue in result["issues"]:
            print(json.dumps(issue, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
