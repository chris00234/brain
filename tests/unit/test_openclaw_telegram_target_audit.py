from __future__ import annotations

import importlib.util
import json
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_openclaw_telegram_targets", BRAIN_ROOT / "cli" / "audit_openclaw_telegram_targets.py"
)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)


def test_cron_audit_accepts_numeric_target(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "1",
                        "name": "ok",
                        "delivery": {"channel": "telegram", "to": audit.CHRIS_TELEGRAM_ID},
                    }
                ]
            }
        )
    )
    assert audit.audit_cron_jobs(p) == []


def test_cron_audit_rejects_chris_alias(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "1",
                        "name": "bad",
                        "delivery": {"channel": "telegram", "to": "@chris"},
                    }
                ]
            }
        )
    )
    issues = audit.audit_cron_jobs(p)
    assert issues[0]["issue"] == "alias"
    assert issues[0]["expected"] == audit.CHRIS_TELEGRAM_ID


def test_cron_audit_rejects_prompt_alias_without_numeric_target(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "1",
                        "name": "daily",
                        "prompt": "요약해서 Chris한테 텔레그램으로 보내줘",
                        "delivery": {"channel": "telegram", "to": audit.CHRIS_TELEGRAM_ID},
                    }
                ]
            }
        )
    )
    issues = audit.audit_cron_jobs(p)
    assert issues[0]["issue"] == "prompt_alias_without_numeric_target"
    assert issues[0]["expected"] == audit.CHRIS_TELEGRAM_ID


def test_cron_audit_accepts_prompt_with_numeric_target(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "1",
                        "name": "daily",
                        "prompt": f"요약해서 Telegram numeric target {audit.CHRIS_TELEGRAM_ID}로 보내줘",
                        "delivery": {"channel": "telegram", "to": audit.CHRIS_TELEGRAM_ID},
                    }
                ]
            }
        )
    )
    assert audit.audit_cron_jobs(p) == []
