#!/usr/bin/env python3
"""Audit Hermes Telegram delivery targets.

Brain SLO/critical alerts and scheduled messages must use Chris's numeric
Telegram chat id, not aliases such as `Chris` or `@chris`. OpenClaw is retired;
this audit inspects Hermes config/profile YAML and never reads ~/.openclaw.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - CI env should have PyYAML via brain venv
    yaml = None  # type: ignore[assignment]

HERMES_HOME = Path.home() / ".hermes"
DEFAULT_CONFIG = HERMES_HOME / "config.yaml"
PROFILES_DIR = HERMES_HOME / "profiles"
CHRIS_TELEGRAM_ID = "8484060831"
ALIAS_RE = re.compile(r"^@?chris(?:\s+cho)?$", re.I)
PROMPT_ALIAS_RE = re.compile(r"(chris|chris cho|크리스|Chris한테|텔레그램으로 보내줘)", re.I)


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def _target_values(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract explicit Telegram target-ish config values."""
    values: list[tuple[str, str]] = []
    telegram = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
    ptelegram = platforms.get("telegram") if isinstance(platforms.get("telegram"), dict) else {}
    gateway = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    gplatforms = gateway.get("platforms") if isinstance(gateway.get("platforms"), dict) else {}
    gtelegram = gplatforms.get("telegram") if isinstance(gplatforms.get("telegram"), dict) else {}

    for prefix, block in (
        ("telegram", telegram),
        ("platforms.telegram", ptelegram),
        ("gateway.platforms.telegram", gtelegram),
    ):
        for key in ("home_channel", "chat_id", "target", "to"):
            raw = block.get(key)
            if raw:
                values.append((f"{prefix}.{key}", str(raw)))
        for key in ("allowed_chats", "group_allowed_chats", "allow_from", "group_allow_from"):
            raw = block.get(key)
            if isinstance(raw, str) and raw:
                values.extend((f"{prefix}.{key}", item.strip()) for item in raw.split(",") if item.strip())
            elif isinstance(raw, list):
                values.extend((f"{prefix}.{key}", str(item)) for item in raw if str(item).strip())
        extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
        for key in ("home_channel", "chat_id", "target", "to", "allowed_chats", "group_allowed_chats"):
            raw = extra.get(key)
            if isinstance(raw, str) and raw:
                values.extend(
                    (f"{prefix}.extra.{key}", item.strip()) for item in raw.split(",") if item.strip()
                )
            elif isinstance(raw, list):
                values.extend((f"{prefix}.extra.{key}", str(item)) for item in raw if str(item).strip())
    return values


def audit_config(path: Path) -> list[dict[str, Any]]:
    cfg = _load_yaml(path)
    issues: list[dict[str, Any]] = []
    if not cfg:
        return issues
    for field, value in _target_values(cfg):
        platform, _, target = value.partition(":")
        check = target or platform
        if ALIAS_RE.match(check):
            issues.append(
                {
                    "file": str(path),
                    "field": field,
                    "target": value,
                    "issue": "alias_target",
                    "expected": CHRIS_TELEGRAM_ID,
                }
            )
        elif check and not re.match(r"^-?\d+$", check) and "telegram" not in check.lower():
            issues.append(
                {
                    "file": str(path),
                    "field": field,
                    "target": value,
                    "issue": "unexpected_non_numeric_target",
                    "expected": CHRIS_TELEGRAM_ID,
                }
            )
    for section in ("cron", "jobs", "reminders"):
        raw = cfg.get(section)
        if (
            raw
            and PROMPT_ALIAS_RE.search(json.dumps(raw, ensure_ascii=False))
            and CHRIS_TELEGRAM_ID not in json.dumps(raw, ensure_ascii=False)
        ):
            issues.append(
                {
                    "file": str(path),
                    "field": section,
                    "issue": "prompt_alias_without_numeric_target",
                    "expected": CHRIS_TELEGRAM_ID,
                }
            )
    return issues


def iter_configs() -> list[Path]:
    paths = [DEFAULT_CONFIG]
    if PROFILES_DIR.exists():
        paths.extend(sorted(PROFILES_DIR.glob("*/config.yaml")))
    return [p for p in paths if p.exists()]


def run() -> dict[str, Any]:
    paths = iter_configs()
    issues: list[dict[str, Any]] = []
    for path in paths:
        issues.extend(audit_config(path))
    return {
        "ok": not issues,
        "files_checked": [str(p) for p in paths],
        "issues": issues,
        "issue_count": len(issues),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result["ok"]:
        print("Hermes Telegram target audit PASSED")
    else:
        print("Hermes Telegram target audit FAILED")
        for issue in result["issues"]:
            print(json.dumps(issue, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
