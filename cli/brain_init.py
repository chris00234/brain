"""brain init — idempotent bootstrap for a fresh Mac.

Usage:
    brain-init check                  # report gaps, exit 0 (ready) or 2 (gaps)
    brain-init                        # full setup (idempotent, refuses if already installed)
    brain-init --yes                  # force re-run even if already installed
    brain-init plists [--dry-run]     # (re)bootstrap launchd entries
    brain-init secrets [--force]      # seed .personal_webhook_secret
    brain-init migrate                # run schema_versions.check_and_migrate()

This script never restarts brain-server unless explicitly requested via `--restart-brain`.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
VENV_PY = BRAIN_ROOT / ".venv/bin/python"
LAUNCH_SRC = BRAIN_ROOT / "launchd"
LAUNCH_DST = Path("~/Library/LaunchAgents").expanduser()

SECRET_F = Path("~/.openclaw/credentials/.personal_webhook_secret").expanduser()
OUTBOX_ROOT = Path("~/.openclaw/outbox/brain-learn").expanduser()

PLISTS = [
    "ai.openclaw.brain-server.plist",
    "ai.openclaw.ollama-native.plist",
    "ai.openclaw.neo4j-native.plist",
    "ai.openclaw.qdrant-native.plist",
    "ai.openclaw.qdrant-backup.plist",
    "ai.openclaw.brain-ci.plist",
    "ai.openclaw.log-rotation.plist",
    "ai.openclaw.gateway.plist",
    "ai.openclaw.watchdog.plist",
    "ai.openclaw.orbstack-watchdog.plist",
]

REQ_DIRS = [
    BRAIN_ROOT / "logs",
    BRAIN_ROOT / "logs/training",
    BRAIN_ROOT / "logs/backups",
    BRAIN_ROOT / "qdrant-backups",
    Path("~/.openclaw/logs").expanduser(),
    Path("~/.openclaw/credentials").expanduser(),
    OUTBOX_ROOT / "pending",
    OUTBOX_ROOT / "inflight",
    OUTBOX_ROOT / "done",
    OUTBOX_ROOT / "quarantine",
]


def _check_service(name: str, port: int) -> bool:
    try:
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def cmd_check() -> int:
    """Report current install state. Exit 0 if ready, 2 if gaps."""
    gaps: list[str] = []

    if not VENV_PY.exists():
        gaps.append(f"venv missing at {VENV_PY} — run `uv sync --dev`")

    if not SECRET_F.exists():
        gaps.append(f"webhook secret missing at {SECRET_F}")
    elif oct(SECRET_F.stat().st_mode)[-3:] != "600":
        gaps.append(f"webhook secret permissions not 600 at {SECRET_F}")

    for d in REQ_DIRS:
        if not d.exists():
            gaps.append(f"directory missing: {d}")

    for name in PLISTS:
        dst = LAUNCH_DST / name
        if not dst.exists():
            gaps.append(f"plist not installed: {name}")

    services = [
        ("qdrant", 6333),
        ("ollama", 11434),
        ("neo4j (bolt)", 7687),
        ("brain-server", 8791),
    ]
    for svc, port in services:
        if not _check_service(svc, port):
            gaps.append(f"service unreachable: {svc} on 127.0.0.1:{port}")

    if not gaps:
        print("[OK] brain install is complete and reachable")
        return 0

    print(f"[GAPS] {len(gaps)} issue(s) found:")
    for g in gaps:
        print(f"  - {g}")
    return 2


def cmd_secrets(force: bool = False) -> int:
    """Seed .personal_webhook_secret (chmod 600). Idempotent unless --force."""
    if SECRET_F.exists() and not force:
        print(f"[skip] secret exists at {SECRET_F} (use --force to rewrite)")
        return 0
    SECRET_F.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(48)
    SECRET_F.write_text(token)
    SECRET_F.chmod(0o600)
    print(f"[ok] wrote {SECRET_F} (chmod 600)")
    return 0


def cmd_plists(dry_run: bool = False) -> int:
    """Copy canonical plists from brain/launchd/ into ~/Library/LaunchAgents/.

    Uses launchctl bootout + bootstrap. We CANNOT use kickstart -k here because
    the plist file may be new — kickstart only restarts an already-loaded service.
    """
    if not LAUNCH_SRC.exists():
        print(f"[error] canonical plist source missing: {LAUNCH_SRC}")
        return 1

    LAUNCH_DST.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    changed = unchanged = missing = 0

    for name in PLISTS:
        src = LAUNCH_SRC / name
        dst = LAUNCH_DST / name
        if not src.exists():
            print(f"[warn] missing canonical source: {src}")
            missing += 1
            continue

        if dst.exists() and dst.read_bytes() == src.read_bytes():
            print(f"[same] {name}")
            unchanged += 1
            continue

        if dry_run:
            print(f"[would copy] {src} -> {dst}")
            continue

        shutil.copyfile(src, dst)
        # bootout is best-effort (may not be loaded yet); bootstrap is required
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(dst)],
            check=False,
            capture_output=True,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(dst)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"[ok] (re)bootstrapped {name}")
            changed += 1
        else:
            print(f"[error] bootstrap failed for {name}: {result.stderr.strip()}")
            return 1

    print(f"\nplists: {changed} changed, {unchanged} unchanged, " f"{missing} missing source")
    return 0


def cmd_migrate() -> int:
    """Run schema_versions.check_and_migrate() against the brain DBs."""
    sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
    try:
        from schema_versions import check_and_migrate  # type: ignore
    except ImportError as e:
        print(f"[error] cannot import schema_versions: {e}")
        return 1

    result = check_and_migrate()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") != "error" else 1


def cmd_full(yes: bool = False) -> int:
    """Idempotent end-to-end bootstrap.

    Refuses to run if already installed unless --yes is passed.
    Order: dirs → secrets → uv sync → plists → migrate.
    Does NOT restart brain-server (use `launchctl kickstart -k` after).
    """
    state = cmd_check()
    if state == 0 and not yes:
        print("\nBrain appears fully installed. Use --yes to force re-run.")
        return 0

    print("\n=== Phase 1: directories ===")
    for d in REQ_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        print(f"[ok] {d}")

    print("\n=== Phase 2: secrets ===")
    cmd_secrets()

    print("\n=== Phase 3: uv sync ===")
    if shutil.which("uv") is None:
        print("[warn] uv not on PATH. Install with: " "curl -LsSf https://astral.sh/uv/install.sh | sh")
    else:
        result = subprocess.run(
            ["uv", "sync", "--dev"],
            cwd=str(BRAIN_ROOT),
            check=False,
        )
        if result.returncode != 0:
            print("[error] uv sync failed")
            return 1

    print("\n=== Phase 4: launchd plists ===")
    if cmd_plists() != 0:
        return 1

    print("\n=== Phase 5: schema migrations ===")
    if cmd_migrate() != 0:
        print("[warn] schema migration reported issues — check logs")

    print("\n=== Bootstrap complete ===")
    print("Next steps:")
    print(f"  launchctl kickstart -k gui/{os.getuid()}/ai.openclaw.brain-server")
    print("  curl -fs http://127.0.0.1:8791/healthz")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="brain-init")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("check", help="report install gaps")
    sub.add_parser("migrate", help="run schema migrations")

    p_plists = sub.add_parser("plists", help="(re)bootstrap launchd plists")
    p_plists.add_argument("--dry-run", action="store_true")

    p_secrets = sub.add_parser("secrets", help="seed webhook secret")
    p_secrets.add_argument("--force", action="store_true")

    p_init = sub.add_parser("init", help="full bootstrap (idempotent)")
    p_init.add_argument("--yes", action="store_true")

    parser.add_argument("--yes", action="store_true", help="force-run default init")
    args = parser.parse_args()

    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "migrate":
        return cmd_migrate()
    if args.cmd == "plists":
        return cmd_plists(dry_run=args.dry_run)
    if args.cmd == "secrets":
        return cmd_secrets(force=args.force)
    if args.cmd == "init":
        return cmd_full(yes=args.yes)
    return cmd_full(yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
