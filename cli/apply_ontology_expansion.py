#!/usr/bin/env python3
"""Apply ontology expansion config only after rollout gates pass.

This is the operator path for production changes. It updates the repo launchd
plist and the installed LaunchAgent plist, restarts Brain, and runs the rollout
gate again. Unsafe relation sets fail before any plist is changed.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_PLIST = ROOT / "launchd" / "ai.openclaw.brain-server.plist"
INSTALLED_PLIST = Path.home() / "Library" / "LaunchAgents" / "ai.openclaw.brain-server.plist"
BACKUP_DIR = ROOT / ".omx" / "plans"
LABEL = "ai.openclaw.brain-server"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _run(cmd: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)


def _load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return plistlib.load(fh)


def _write_plist(path: Path, payload: dict[str, Any]) -> None:
    with path.open("wb") as fh:
        plistlib.dump(payload, fh, sort_keys=False)


def _backup(path: Path, stamp: str) -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    scope = "installed" if path == INSTALLED_PLIST else "repo"
    backup = BACKUP_DIR / f"{path.name}.{scope}.pre-ontology-apply-{stamp}.bak"
    shutil.copy2(path, backup)
    return str(backup)


def _patch_plist(path: Path, args: argparse.Namespace, stamp: str) -> str:
    backup = _backup(path, stamp)
    payload = _load_plist(path)
    env = dict(payload.get("EnvironmentVariables") or {})
    env["BRAIN_ONTOLOGY_EXPANSION_ENABLED"] = _bool_str(args.enabled)
    env["BRAIN_ONTOLOGY_EXPANSION_SOURCE"] = args.source
    env["BRAIN_ONTOLOGY_EXPANSION_RELATIONS"] = args.relations
    env["BRAIN_ONTOLOGY_EXPANSION_MODE"] = args.mode
    env["BRAIN_ONTOLOGY_SIDECAR_LIMIT"] = str(args.sidecar_limit)
    env["BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS"] = str(args.max_terms)
    env["BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED"] = _bool_str(args.conditional)
    payload["EnvironmentVariables"] = env
    _write_plist(path, payload)
    return backup


def _restore_backups(backups: dict[str, str]) -> None:
    if backups.get("repo"):
        shutil.copy2(backups["repo"], REPO_PLIST)
    if backups.get("installed"):
        shutil.copy2(backups["installed"], INSTALLED_PLIST)


def _healthz() -> dict[str, Any]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8791/healthz", timeout=10) as resp:
            return json.load(resp)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _run_gate(args: argparse.Namespace) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(ROOT / "cli" / "ontology_rollout_gate.py"),
        "--source",
        args.source,
        "--relations",
        args.relations,
        "--mode",
        args.mode,
        "--sidecar-limit",
        str(args.sidecar_limit),
        "--max-p95-regression-pct",
        str(args.max_p95_regression_pct),
        "--max-mean-regression-ms",
        str(args.max_mean_regression_ms),
        "--max-ontology-p95-ms",
        str(args.max_ontology_p95_ms),
        "--json",
    ]
    cmd.append("--conditional" if args.conditional else "--no-conditional")
    return _run(cmd, timeout=420)


def _reload_launchagent() -> dict[str, Any]:
    domain = f"gui/{os.getuid()}"
    bootout = _run(["launchctl", "bootout", domain, str(INSTALLED_PLIST)], timeout=30)
    bootstrap = _run(["launchctl", "bootstrap", domain, str(INSTALLED_PLIST)], timeout=30)
    if bootstrap.returncode != 0 and "Bootstrap failed: 5" in bootstrap.stderr:
        # If launchd still considers it loaded, fall back to kickstart. This is
        # not the preferred path for env changes but avoids leaving the service
        # down on a transient bootstrap race.
        kick = _run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], timeout=30)
    else:
        kick = _run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], timeout=30)
    return {
        "bootout": {"exit_code": bootout.returncode, "stderr": bootout.stderr.strip()},
        "bootstrap": {"exit_code": bootstrap.returncode, "stderr": bootstrap.stderr.strip()},
        "kickstart": {"exit_code": kick.returncode, "stderr": kick.stderr.strip()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely apply ontology expansion launchd config")
    parser.add_argument("--relations", default="has_agent,owned_by,owns")
    parser.add_argument("--source", choices=["neo4j", "file"], default="neo4j")
    parser.add_argument("--mode", choices=["rewrite", "sidecar"], default="rewrite")
    parser.add_argument("--sidecar-limit", type=int, default=5)
    parser.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--conditional", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-terms", type=int, default=5)
    parser.add_argument("--max-p95-regression-pct", type=float, default=10.0)
    parser.add_argument("--max-mean-regression-ms", type=float, default=25.0)
    parser.add_argument("--max-ontology-p95-ms", type=int, default=75)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    stamp = _timestamp()
    pre_gate = _run_gate(args)
    if pre_gate.returncode != 0:
        report = {
            "applied": False,
            "reason": "pre-apply rollout gate failed",
            "exit_code": pre_gate.returncode,
            "stdout": pre_gate.stdout[-4000:],
            "stderr": pre_gate.stderr[-2000:],
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1

    backups = {
        "repo": _patch_plist(REPO_PLIST, args, stamp),
        "installed": _patch_plist(INSTALLED_PLIST, args, stamp),
    }
    lint_repo = _run(["plutil", "-lint", str(REPO_PLIST)], timeout=30)
    lint_installed = _run(["plutil", "-lint", str(INSTALLED_PLIST)], timeout=30)
    if lint_repo.returncode != 0 or lint_installed.returncode != 0:
        shutil.copy2(backups["repo"], REPO_PLIST)
        shutil.copy2(backups["installed"], INSTALLED_PLIST)
        report = {
            "applied": False,
            "reason": "plist lint failed; restored backups",
            "lint_repo": lint_repo.stderr or lint_repo.stdout,
            "lint_installed": lint_installed.stderr or lint_installed.stdout,
            "backups": backups,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1

    reload_result = _reload_launchagent()
    time.sleep(2)
    health = _healthz()
    post_gate = _run_gate(args)
    applied = post_gate.returncode == 0 and health.get("status") == "ok"
    rollback: dict[str, Any] = {"performed": False}
    if not applied:
        _restore_backups(backups)
        rollback_reload = _reload_launchagent()
        time.sleep(2)
        rollback = {
            "performed": True,
            "launchd_reload": rollback_reload,
            "health": _healthz(),
        }
    report = {
        "applied": applied,
        "timestamp": stamp,
        "config": {
            "enabled": args.enabled,
            "source": args.source,
            "mode": args.mode,
            "sidecar_limit": args.sidecar_limit,
            "relations": [rel.strip() for rel in args.relations.split(",") if rel.strip()],
            "conditional": args.conditional,
            "max_terms": args.max_terms,
        },
        "backups": backups,
        "lint": {
            "repo": lint_repo.stdout.strip(),
            "installed": lint_installed.stdout.strip(),
        },
        "launchd_reload": reload_result,
        "health": health,
        "post_gate_exit_code": post_gate.returncode,
        "post_gate_summary": _safe_gate_summary(post_gate.stdout),
        "rollback": rollback,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["applied"] else 1


def _safe_gate_summary(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {"parse_error": stdout[-1000:]}
    return {
        "passed": payload.get("passed"),
        "failures": payload.get("failures"),
        "summary": payload.get("summary"),
        "artifacts": payload.get("artifacts"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
