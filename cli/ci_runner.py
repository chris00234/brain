"""brain CI runner — ruff + bandit + pytest in sequence.

Triggered by:
  - launchd plist `ai.openclaw.brain-ci.plist` watching `.git/refs/heads`
  - pre-commit hook (manual)
  - any caller running `python cli/ci_runner.py`

Exit codes:
  0 — all green
  1 — ruff failed
  2 — bandit failed
  3 — pytest failed
  4 — config/setup error

Failures emit a Telegram alert via the existing OpenClaw gateway so Chris sees
the regression on his phone without polling logs.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
VENV_BIN = BRAIN_ROOT / ".venv/bin"
LOG_FILE = BRAIN_ROOT / "logs/ci.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"

# ruff respects `extend-exclude` in ruff.toml when invoked without explicit targets.
# Bandit needs explicit targets — point at the new modules only (legacy is excluded
# via bandit.yaml).  As Phase 2-9 add files (autonomy.py, breakers.py, sm2.py, ...),
# add them to BANDIT_TARGETS here.
BANDIT_TARGETS = [
    BRAIN_ROOT / "cli/brain_init.py",
    BRAIN_ROOT / "cli/ci_runner.py",
]


def _log(line: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] {line}"
    print(msg)
    with LOG_FILE.open("a") as f:
        f.write(msg + "\n")


def _alert(title: str, body: str) -> None:
    """Send a Telegram alert via OpenClaw gateway. Best-effort."""
    if not Path(OPENCLAW_BIN).exists():
        _log(f"[skip alert] openclaw not at {OPENCLAW_BIN}")
        return
    try:
        subprocess.run(
            [
                OPENCLAW_BIN,
                "message",
                "send",
                "--channel",
                "telegram",
                "--title",
                title,
                "--body",
                body,
            ],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except Exception as e:
        _log(f"[alert failed] {e}")


def run_step(name: str, cmd: list[str]) -> tuple[int, str]:
    """Run one CI step and capture output."""
    _log(f"=== {name} ===")
    _log("$ " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=str(BRAIN_ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        _log(f"[FAIL] {name} exit={result.returncode}")
        _log(out[-2000:])
    else:
        _log(f"[OK] {name}")
    return result.returncode, out


def main() -> int:
    # ruff: walks "." and respects ruff.toml extend-exclude. New files in scope;
    # legacy modules excluded.
    rc, out = run_step("ruff check", [str(VENV_BIN / "ruff"), "check", "."])
    if rc != 0:
        _alert("brain CI: ruff failed", out[-800:])
        return 1

    rc, out = run_step(
        "ruff format --check",
        [str(VENV_BIN / "ruff"), "format", "--check", "."],
    )
    if rc != 0:
        _alert("brain CI: ruff format drift", out[-800:])
        return 1

    bandit_targets = [str(p) for p in BANDIT_TARGETS if p.exists()]
    if bandit_targets:
        rc, out = run_step(
            "bandit",
            [
                str(VENV_BIN / "bandit"),
                "-c",
                str(BRAIN_ROOT / "bandit.yaml"),
                "-q",
                "-r",
                *bandit_targets,
            ],
        )
        if rc != 0:
            _alert("brain CI: bandit security issue", out[-800:])
            return 2

    rc, out = run_step(
        "pytest",
        [str(VENV_BIN / "python"), "-m", "pytest", "-q", "--tb=short"],
    )
    if rc != 0:
        _alert("brain CI: pytest failed", out[-1200:])
        return 3

    _log("=== ALL GREEN ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
